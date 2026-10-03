"""V2-004C: provider nonzero exits keep bounded, sanitized failure reasons offline."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from runtime import WorkUnit, run, ExecutionError, MAX_DIAGNOSTIC_BYTES
from switchyard.context_transport import (StageInvocation, workspace_resolver, StageOutputContract,
                                          StageHandoffContract)
from switchyard.work_contract import WorkWorkspace
from switchyard.workflow_bundle import (load_workflow_bundle, ExecutionProfile, CommandSpec,
                                        CommandOutcomes, CommandCapture)
from switchyard.workflow_compiler import compile_workflow, ExecutionBinding
from switchyard.provider_stages import ProviderRuntimeConfig, ProviderStageExecutorResolver
from switchyard.provider_process import ProviderExecutionError
from test_workflow_compiler import EXAMPLE

VALID = {'outcome':'ready','produced_artifacts':[],'handoff':None,'diagnostics':None}


def envelope(**fields):
    """Claude --output-format json result envelope with fields that must never leak."""
    value = {'type':'result','subtype':'success','is_error':True,'duration_ms':12,'num_turns':1,
             'session_id':'session-identifier-LEAK','uuid':'uuid-LEAK','total_cost_usd':0.5,
             'usage':{'input_tokens':123456789},'modelUsage':{'haiku':{'LEAK':1}},
             'structured_output':{**VALID,'diagnostics':'structured-output-LEAK'},
             'permission_denials':[], 'result':''}
    value.update(fields)
    return json.dumps(value)


@unittest.skipIf(os.name=='nt','executable fixture scripts require POSIX shebang execution')
class ProviderFailureEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        script=(Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        for name in ('claude','codex','python3'):
            path=self.bin/name; path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        self.log=self.root/'calls.jsonl'; self.plan=self.root/'plan.json'
        self.plan.write_text(json.dumps({'artifacts':{}}))
        self.env={'PATH':str(self.bin),'FAKE_LOG':str(self.log),'FAKE_PLAN':str(self.plan)}

    def scripted(self, stdout=b'', stderr=b'', code=1, result=None):
        files={}
        for key,data in (('FAKE_STDOUT_FILE',stdout),('FAKE_STDERR_FILE',stderr),('FAKE_RESULT_FILE',result)):
            if data is not None:
                path=self.root/key.lower(); path.write_bytes(data.encode() if type(data) is str else data)
                files[key]=str(path)
        return {**self.env,'FAKE_MODE':'scripted','FAKE_EXIT':str(code),**files}

    def adapter(self, provider='claude', model='haiku', environment=None, command=None):
        config=ProviderRuntimeConfig(str(self.root),str(self.bin/'claude'),str(self.bin/'codex'),
                                     environment or self.env, timeout_seconds=5)
        binding=ExecutionBinding('test','TRIAGE','worker',0,'profile',provider,model,
                                 None if command else 'medium',None,None if command else ('repo_read',))
        resolver=ProviderStageExecutorResolver({'profile':ExecutionProfile(provider,model,command)},config)
        contract=StageOutputContract(('ready',),(),StageHandoffContract(True,()))
        return resolver(binding),StageInvocation(binding,(),output_contract=contract)

    def failure(self, provider='claude', **channels):
        adapter,invocation=self.adapter(provider,'haiku' if provider=='claude' else 'codex',self.scripted(**channels))
        before=len(self.calls())
        with self.assertRaises(ProviderExecutionError) as caught:
            adapter.run(invocation)
        self.assertEqual(len(self.calls()),before+1)  # One process, never retried.
        error=caught.exception
        self.assertEqual(str(error),f'provider exited with status {error.returncode}')
        self.assertLessEqual(len(error.diagnostics.utf8),MAX_DIAGNOSTIC_BYTES)
        return error,error.diagnostics.utf8.decode('utf-8')

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assertNoLeak(self, text):
        for secret in ('LEAK','123456789','"type"','structured_output','{'):
            self.assertNotIn(secret,text)

    def test_live_failure_shape_retains_structured_reason_with_empty_stderr(self):
        # Exact V2-006 TRIAGE shape: reason only on stdout, empty stderr, exit 1.
        # Before V2-004C the stdout envelope was never decoded and diagnostics were ''.
        stdout=envelope(result='API Error: 400 model unavailable for this account',api_error_status=400)
        error,text=self.failure(stdout=stdout)
        self.assertEqual(error.returncode,1)
        self.assertEqual(text,'claude result: subtype=success, is_error=true, api_error_status=400\n'
                              'error: API Error: 400 model unavailable for this account')
        self.assertNoLeak(text)

    def test_error_subtype_errors_and_stderr_are_deterministic_and_bounded(self):
        stdout=envelope(subtype='error_during_execution',terminal_reason='error',result='',
                        errors=['first reason','first reason',{'message':'object reason','secret':'LEAK'},7],
                        permission_denials=[{'tool_name':'Bash','tool_use_id':'LEAK',
                                             'tool_input':{'command':'cat LEAK'}}]*2)
        first=self.failure(stdout=stdout,stderr='operational stderr\n')[1]
        second=self.failure(stdout=stdout,stderr='operational stderr\n')[1]
        self.assertEqual(first,second)
        self.assertEqual(first,'claude result: subtype=error_during_execution, terminal_reason=error, is_error=true\n'
                               'error: first reason\nerror: object reason\n'
                               'permission_denials: Bash\nstderr: operational stderr')
        self.assertNoLeak(first)

    def test_identical_stderr_is_not_duplicated(self):
        text=self.failure(stdout=envelope(result='Not logged in'),stderr='Not logged in\n')[1]
        self.assertEqual(text.count('Not logged in'),1)

    def test_malformed_stdout_prefers_stderr(self):
        text=self.failure(stdout='partial stdout {',stderr='useful stderr reason')[1]
        self.assertEqual(text,'stderr: useful stderr reason')

    def test_malformed_stdout_without_stderr_uses_bounded_raw_fallback(self):
        text=self.failure(stdout='Error: \x1b[31mbad flag\x1b[0m\x00 '+'x'*5000)[1]
        self.assertTrue(text.startswith('stdout: Error: bad flag'))
        self.assertTrue(text.endswith('…[truncated]'))
        self.assertLessEqual(len(text.encode()),len('stdout: ')+1024)
        self.assertNotIn('\x1b',text); self.assertNotIn('\x00',text)

    def test_no_reason_falls_back_to_generic_status(self):
        for stdout in ('', json.dumps({'arbitrary':'LEAK'}), json.dumps(['LEAK'])):
            with self.subTest(stdout=stdout):
                error,text=self.failure(stdout=stdout,code=3)
                self.assertEqual((error.returncode,text),(3,'provider exited with status 3'))

    def test_oversized_reason_and_stderr_stay_within_diagnostic_bound(self):
        stdout=envelope(errors=['e'*200000],result='r'*200000)
        error,text=self.failure(stdout=stdout,stderr='s'*100000)
        self.assertLessEqual(len(error.diagnostics.utf8),MAX_DIAGNOSTIC_BYTES)
        self.assertIn('error: eeee',text); self.assertIn('…[truncated]',text)
        self.assertIn('\nstderr: ssss',text)  # Both sections survive the shared bound.

    def test_success_prose_and_structured_output_are_not_failure_reasons(self):
        # Even a schema-valid StageResult on a failed exit is not semantic output.
        stdout=envelope(is_error=False,result='model prose LEAK')
        text=self.failure(stdout=stdout)[1]
        self.assertEqual(text,'claude result: subtype=success, is_error=false')

    def test_credentials_and_control_characters_are_sanitized(self):
        stdout=envelope(result='Invalid key sk-ant-api03-abcdefghijklmnopqrstuvwx; '
                               'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123\x07 api_key=supersecretvalue',
                        errors=['\x1b]0;title\x07token: abcdefgh12345678'])
        text=self.failure(stdout=stdout)[1]
        for secret in ('abcdefghijklmnopqrstuvwx','abcdefghijklmnopqrstuvwxyz0123','supersecretvalue','abcdefgh12345678'):
            self.assertNotIn(secret,text)
        self.assertIn('sk-[REDACTED]',text); self.assertIn('Bearer [REDACTED]',text)
        self.assertFalse(any(ord(c)<32 and c!='\n' for c in text))

    def test_codex_failure_keeps_stderr_and_never_parses_result_file(self):
        error,text=self.failure('codex',stdout='final agent message LEAK',stderr='ERROR: model rejected',
                                result=json.dumps(VALID))
        self.assertEqual((error.returncode,text),(1,'stderr: ERROR: model rejected'))
        error,text=self.failure('codex',stdout='LEAK',code=2,result=json.dumps(VALID))
        self.assertEqual(text,'provider exited with status 2')

    def test_success_paths_unchanged(self):
        claude,invocation=self.adapter('claude','haiku',self.scripted(
            stdout=json.dumps({'type':'result','subtype':'success','is_error':False,'result':'prose',
                               'structured_output':VALID}),stderr='chatter',code=0))
        self.assertEqual(claude.run(invocation).outcome,'ready')
        codex,invocation=self.adapter('codex','codex',self.scripted(stderr='chatter',code=0,result=json.dumps(VALID)))
        self.assertEqual(codex.run(invocation).outcome,'ready')
        # Exit 0 with an error envelope remains the existing strict success-path rejection.
        claude,invocation=self.adapter('claude','haiku',self.scripted(stdout=envelope(result='boom'),code=0))
        with self.assertRaisesRegex(ProviderExecutionError,'successful result envelope') as caught:
            claude.run(invocation)
        self.assertIsNone(caught.exception.returncode)

    def test_command_nonzero_exit_remains_semantic_outcome(self):
        command=CommandSpec(('python3','-m','unittest'),CommandOutcomes({0:'pass'},'fail'),CommandCapture('evidence','stderr',100))
        adapter,invocation=self.adapter('command',None,{**self.env,'FAKE_EXIT':'9'},command)
        result=adapter.run(invocation)
        self.assertEqual((result.outcome,result.produced_artifacts[0].content),('fail','=stderr evidence\n'))

    def test_runtime_records_unchanged_execution_failure(self):
        bundle=load_workflow_bundle(EXAMPLE)
        config=ProviderRuntimeConfig(str(self.root),str(self.bin/'claude'),str(self.bin/'codex'),
                                     self.scripted(stdout=envelope(result='provider reason')),timeout_seconds=5)
        workspace=WorkWorkspace.open(self.root/'work',bundle.artifacts)
        workspace.initialize_request('request')
        resolver=ProviderStageExecutorResolver(bundle.profiles,config)
        compiled=compile_workflow(bundle,'bugfix',workspace_resolver(bundle,'bugfix',workspace,resolver))
        with self.assertRaises(ExecutionError) as caught:
            run(WorkUnit('request'),compiled.flow,compiled.executors)
        self.assertEqual([e.kind for e in caught.exception.events],
                         ['run_started','state_entered','invocation_started','run_error'])
        cause=caught.exception.__cause__
        self.assertIsInstance(cause,ProviderExecutionError)
        self.assertIn('error: provider reason',cause.diagnostics.utf8.decode())
        self.assertEqual(len(self.calls()),1)
        self.assertEqual([a for a in bundle.artifacts if workspace.versions(a)],['request'])
        self.assertEqual(sum(len(workspace.handoffs(s)) for s in bundle.flow('bugfix').states),0)

    def test_error_type_remains_backward_compatible(self):
        error=ProviderExecutionError('process timeout')
        self.assertIsNone(error.diagnostics); self.assertIsNone(error.returncode)


if __name__ == '__main__':
    unittest.main()

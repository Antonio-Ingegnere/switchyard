"""Actual subprocess tests use only repository-owned fake executable scripts."""
from dataclasses import replace, FrozenInstanceError
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import WorkUnit, run, DiagnosticPayload
from switchyard.context_transport import StageInvocation, workspace_resolver
from switchyard.work_contract import WorkWorkspace, HandoffRecord
from switchyard.workflow_bundle import (load_workflow_bundle, ExecutionProfile, CommandSpec,
                                        CommandOutcomes, CommandCapture)
from switchyard.workflow_compiler import compile_workflow, ExecutionBinding, WorkflowCompileError
from switchyard.provider_stages import (ProviderRuntimeConfig, ProviderStageExecutorResolver,
                                        ProviderResolutionError, render_invocation,
                                        parse_stage_result, result_schema, _decode)
from switchyard.provider_process import ProviderExecutionError
from test_workflow_compiler import EXAMPLE
from test_context_transport import SPEC


@unittest.skipIf(os.name=='nt','executable fixture scripts require POSIX shebang execution')
class ProviderStageTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        script=(Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        for name in ('claude','codex','python3'):
            path=self.bin/name; path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        self.bundle=load_workflow_bundle(EXAMPLE)
        self.log=self.root/'calls.jsonl'; self.plan=self.root/'plan.json'
        self.plan.write_text(json.dumps({'artifacts':{'A':[]}}))
        self.env={'PATH':str(self.bin),'FAKE_LOG':str(self.log),'FAKE_PLAN':str(self.plan)}
        self.config=ProviderRuntimeConfig(str(self.root),str(self.bin/'claude'),str(self.bin/'codex'),
                                           self.env, timeout_seconds=3,codex_models=('codex',))

    def binding(self,provider='claude',model='sonnet',effort='medium'):
        return ExecutionBinding('test','A','worker',0,'profile',provider,model,effort,None,None)

    def adapter(self,provider='claude',model='sonnet',effort='medium',command=None,config=None):
        binding=self.binding(provider,model,effort)
        resolver=ProviderStageExecutorResolver({'profile':ExecutionProfile(provider,model,command)},config or self.config)
        return resolver(binding),StageInvocation(binding,())

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_provider_arguments_and_result_channels(self):
        for provider,model in [('claude','sonnet'),('codex','codex'),('claude','haiku')]:
            with self.subTest(provider=provider,model=model):
                adapter,inv=self.adapter(provider,model)
                result=adapter.run(inv)
                self.assertEqual(result.outcome,'ready')
                call=self.calls()[-1]
                self.assertEqual(call['provider'],provider)
                self.assertEqual(Path(call['cwd']).resolve(),self.root.resolve())
                self.assertEqual(call['argv'][call['argv'].index('--model')+1],model)
                self.assertEqual(call['input'],render_invocation(inv))
                if provider=='codex':
                    self.assertIn('model_reasoning_effort="medium"',call['argv'])
                    self.assertEqual(call['schema'],result_schema())
                elif model=='sonnet':
                    self.assertIn('--effort',call['argv']); self.assertIn('medium',call['argv'])
                else:
                    self.assertEqual(call['thinking'],'8192'); self.assertNotIn('--effort',call['argv'])
                if provider=='claude': self.assertEqual(call['retries'],'1')

    def test_all_effort_levels(self):
        for level,tokens in [('low','1024'),('medium','8192'),('high','16384')]:
            for provider,model in [('claude','sonnet'),('claude','haiku'),('codex','codex')]:
                adapter,inv=self.adapter(provider,model,level); adapter.run(inv)
                call=self.calls()[-1]
                if model=='haiku': self.assertEqual(call['thinking'],tokens)
                elif provider=='codex': self.assertIn('model_reasoning_effort='+json.dumps(level),call['argv'])
                else: self.assertEqual(call['argv'][call['argv'].index('--effort')+1],level)

    def test_transport_failures_and_no_retries(self):
        for provider,model in [('claude','sonnet'),('codex','codex')]:
            for mode in ('exit','timeout','malformed','missing_outcome','oversized'):
                with self.subTest(provider=provider,mode=mode):
                    cfg=replace(self.config,environment={**self.env,'FAKE_MODE':mode},
                                timeout_seconds=.15 if mode=='timeout' else 3)
                    adapter,inv=self.adapter(provider,model,config=cfg)
                    before=len(self.calls()) if self.log.exists() else 0
                    with self.assertRaises(ProviderExecutionError): adapter.run(inv)
                    self.assertEqual(len(self.calls()),before+1)
        adapter,inv=self.adapter('codex','codex',config=replace(self.config,environment={**self.env,'FAKE_MODE':'missing_file'}))
        with self.assertRaisesRegex(ProviderExecutionError,'missing'): adapter.run(inv)

    def test_no_execution_when_compilation_cannot_resolve_provider(self):
        workspace=WorkWorkspace.open(self.root/'work',self.bundle.artifacts)
        workspace.initialize_request('request')
        config=replace(self.config,codex_executable='/missing/codex')
        resolver=ProviderStageExecutorResolver(self.bundle.profiles,config)
        with self.assertRaises(WorkflowCompileError):
            compile_workflow(self.bundle,'bugfix',workspace_resolver(self.bundle,'bugfix',workspace,resolver))
        self.assertFalse(self.log.exists())

    def test_cancellation_cleanup(self):
        from switchyard import provider_process
        adapter,inv=self.adapter()
        launched=[]
        original=provider_process.subprocess.Popen
        def track(*args,**kwargs):
            process=original(*args,**kwargs); launched.append(process); return process
        with patch.object(provider_process.subprocess,'Popen',side_effect=track), \
             patch.object(provider_process,'_read_chunk',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): adapter.run(inv)
        self.assertEqual(len(launched),1)
        self.assertIsNotNone(launched[0].poll())

    def test_resolution_and_identity(self):
        with self.assertRaises(ProviderResolutionError): self.adapter(config=replace(self.config,claude_executable='/missing'))
        with self.assertRaises(ProviderResolutionError): self.adapter(model='invented')
        with self.assertRaises(ProviderResolutionError): self.adapter('codex','invented')
        with self.assertRaises(ProviderResolutionError): self.adapter(effort='ultra')
        adapter,inv=self.adapter()
        with self.assertRaises(ProviderExecutionError): adapter.run(replace(inv,binding=replace(inv.binding,model='haiku')))
        with self.assertRaises(TypeError): self.config.environment['PATH']='other'
        with self.assertRaises(FrozenInstanceError): self.config.timeout_seconds=0
        self.assertFalse(self.log.exists())

    def command(self,stream='combined',limit=1000,code=0,mode='valid',capture=True):
        command=CommandSpec(('python3','-m','unittest','--literal','a;b'),CommandOutcomes({0:'pass',2:'blocked'},'fail'),
                            CommandCapture('evidence',stream,limit) if capture else None)
        return self.adapter('command',None,None,command,
                            replace(self.config,environment={**self.env,'FAKE_EXIT':str(code),'FAKE_MODE':mode}))

    def test_command_exit_mapping_and_capture(self):
        for code,expected in [(0,'pass'),(2,'blocked'),(9,'fail')]:
            for stream in ('stdout','stderr','combined'):
                adapter,inv=self.command(stream,code=code); result=adapter.run(inv)
                self.assertEqual(result.outcome,expected); self.assertIsNone(result.handoff)
                text=result.produced_artifacts[0].content
                self.assertEqual(result.produced_artifacts[0].name,'evidence')
                self.assertTrue(text.startswith('='))
                self.assertEqual('stdout' in text,stream!='stderr')
                self.assertEqual('stderr' in text,stream!='stdout')
                if stream=='combined': self.assertEqual(text,'=stdout evidence\nstderr evidence\n')
                self.assertEqual(self.calls()[-1]['argv'],['-m','unittest','--literal','a;b'])
                self.assertEqual(self.calls()[-1]['input'],'')
        adapter,inv=self.command(capture=False)
        self.assertEqual(adapter.run(inv).produced_artifacts,())

    def test_command_truncation_utf8_and_transport(self):
        for limit in (1,2,8,40):
            adapter,inv=self.command('stdout',limit,mode='large')
            content=adapter.run(inv).produced_artifacts[0].content
            self.assertTrue(content.startswith('!')); self.assertLessEqual(len(content.encode()),limit)
            if limit>=8: self.assertTrue(content.endswith('わり'))
        adapter,inv=self.command('stdout',100,mode='invalid_utf8')
        self.assertEqual(adapter.run(inv).produced_artifacts[0].content,'=good\ufffdend')
        adapter,inv=self.command(mode='signal')
        with self.assertRaisesRegex(ProviderExecutionError,'signal'): adapter.run(inv)
        adapter,inv=self.command(mode='timeout'); adapter=replace(adapter,config=replace(adapter.config,timeout_seconds=.15))
        with self.assertRaisesRegex(ProviderExecutionError,'timeout'): adapter.run(inv)
        adapter,inv=self.command(); (self.bin/'python3').unlink()
        with self.assertRaisesRegex(ProviderExecutionError,'cannot start'): adapter.run(inv)

    def integration(self,flow,outcomes=None,fail=False):
        source=self.bundle.flow(flow)
        artifacts={name:[{'name':n,'content':SPEC if self.bundle.artifacts[n].schema else name+' evidence'}
                         for n in state.produces] for name,state in source.states.items()}
        self.plan.write_text(json.dumps({'artifacts':artifacts,'outcomes':outcomes or {}}))
        config=replace(self.config,environment={**self.env,'FAKE_VERIFY_FAIL':'1' if fail else '0'})
        workspace=WorkWorkspace.open(self.root/'work',self.bundle.artifacts)
        workspace.initialize_request('A natural keyboard navigation request.')
        resolver=ProviderStageExecutorResolver(self.bundle.profiles,config)
        compiled=compile_workflow(self.bundle,flow,workspace_resolver(self.bundle,flow,workspace,resolver))
        result=run(WorkUnit('A natural keyboard navigation request.'),compiled.flow,compiled.executors)
        self.assertEqual(result.terminal_state,'READY_FOR_MANUAL_QA')
        return workspace

    def test_bugfix_vertical_horizontal_pipeline(self):
        workspace=self.integration('bugfix',{'DIAGNOSE:0':'inconclusive','DIAGNOSE:1':'diagnosis_disproved'})
        calls=[c for c in self.calls() if c['binding'] and c['binding']['logical_state']=='DIAGNOSE']
        self.assertEqual([c['provider'] for c in calls],['claude','claude','codex'])
        self.assertEqual([c['binding']['model'] for c in calls],['haiku','sonnet','codex'])
        self.assertEqual(calls[0]['thinking'],'8192')
        self.assertIn('high',calls[1]['argv']); self.assertIn('model_reasoning_effort="high"',calls[2]['argv'])
        self.assertNotIn('PRIOR REASONING HANDOFF',calls[0]['input'])
        self.assertIn('PRIOR REASONING HANDOFF',calls[1]['input'])
        self.assertIn('DIAGNOSE reasoning 0',calls[1]['input'])
        self.assertNotIn('PRIOR REASONING HANDOFF',calls[2]['input'])
        for artifact in ('request','bug_spec','reproduction_evidence'):
            self.assertIn('"name":"'+artifact+'"',calls[2]['input'])
        self.assertEqual(len(workspace.handoffs('DIAGNOSE')),3)
        self.assertEqual(len(workspace.versions('verifier_evidence')),1)

    def test_feature_provider_command_evidence_pipeline(self):
        workspace=self.integration('feature',fail=True)
        calls=self.calls()
        implement=next(c for c in calls if c['binding'] and c['binding']['logical_state']=='IMPLEMENT')
        self.assertIn('"name":"microspec"',implement['input'])
        diagnose=next(c for c in calls if c['binding'] and c['binding']['logical_state']=='DIAGNOSE_FAILURE')
        self.assertIn('stdout evidence',diagnose['input'])
        self.assertIn('stderr evidence',diagnose['input'])
        self.assertEqual(len(workspace.versions('verifier_evidence')),2)
        self.assertEqual(workspace.handoffs('VERIFY'),())


class ResultProtocolTests(unittest.TestCase):
    def value(self):
        return {'outcome':'ready','produced_artifacts':[],'handoff':None,'diagnostics':None}

    def test_strict_result(self):
        invalid=[None,[],{},dict(self.value(),extra=1),dict(self.value(),outcome=1),
                 dict(self.value(),produced_artifacts=[{'name':'x','content':'a'},{'name':'x','content':'b'}]),
                 dict(self.value(),handoff={'summary':'only'}),dict(self.value(),diagnostics='a'*4097),
                 dict(self.value(),outcome='budget_exceeded'),dict(self.value(),diagnostics=2)]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ProviderExecutionError): parse_stage_result(value)
        for raw in ('prose','{"a":1,"a":2}','{"a":NaN}'):
            with self.assertRaises(ProviderExecutionError): _decode(raw)
        self.assertEqual(parse_stage_result(dict(self.value(),diagnostics='diagnostic')).diagnostics,DiagnosticPayload(b'diagnostic'))

    def test_render_advisory_only_when_present(self):
        binding=ExecutionBinding('flow','A','role',0,'profile','claude','sonnet','high',None,None)
        inv=StageInvocation(binding,())
        self.assertNotIn('PRIOR REASONING HANDOFF',render_invocation(inv))
        h=HandoffRecord('flow','A','role',0,'ready','hypothesis')
        rendered=render_invocation(replace(inv,previous_handoff=h))
        self.assertIn('PRIOR REASONING HANDOFF — advisory',rendered)
        self.assertIn('hypothesis',rendered)
        self.assertEqual(rendered,render_invocation(replace(inv,previous_handoff=h)))

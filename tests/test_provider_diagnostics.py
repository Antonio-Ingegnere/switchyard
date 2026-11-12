"""OBSERVABILITY (#1) slice 3A: durable, bounded provider diagnostics per invocation. Real deliveries with the offline
provider fixtures; diagnostics are checked against the authoritative history (events.jsonl, operation records)."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard import diagnostics
from switchyard.cli import main
from switchyard.diagnostics import Diagnostics, read_diagnostics, sanitize
from switchyard.provider_process import execute, observe_invocation
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.recovery import (execute_recoverable_v2_work, qa_fix_v2_work, recovery_status, resume_v2_work,
                                 _operations, _store)
from switchyard.trace import trace_events

import test_human_input
from test_trace import TraceBase

SECRET = 'ghp_DIAGsecretTOKEN123456'
# Fixture hook (Claude executable only): emit exact, test-owned channel bytes.
HOOK = '''if os.environ.get('DIAG_STDOUT') is not None:
    sys.stdin.read()
    with open(os.environ['FAKE_LOG'], 'a') as stream: stream.write(json.dumps({'provider': 'claude', 'hook': True})+'\\n')
    os.write(1, Path(os.environ['DIAG_STDOUT']).read_bytes() if os.environ['DIAG_STDOUT'] else b'')
    if os.environ.get('DIAG_STDERR'): os.write(2, Path(os.environ['DIAG_STDERR']).read_bytes())
    sys.exit(int(os.environ.get('DIAG_EXIT', '0')))
'''


class DiagnosticsBase(TraceBase):
    def setUp(self):
        super().setUp()
        claude = self.root/'claude'
        claude.write_text(claude.read_text().replace('argv=sys.argv[1:]', HOOK+'argv=sys.argv[1:]', 1))

    def diags(self, work=None, invocation=None):
        return read_diagnostics(work or self.work, invocation)

    def of(self, state, work=None):
        return [d for d in self.diags(work) if d['state'] == state]

    def diag_bytes(self, work=None):
        d = (work or self.work)/'observability/diagnostics'
        return self.files(d) if d.exists() else {}

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['work', 'diagnostics', str(self.work), *args])
        return code, out.getvalue()

    def claude_emits(self, stdout=b'', stderr=None, exit_code=0):
        paths = {}
        for name, data in (('stdout', stdout), ('stderr', stderr)):
            if data is not None:
                paths[name] = self.root/('diag-'+name); paths[name].write_bytes(data)
        env = dict(DIAG_STDOUT=str(paths['stdout']), DIAG_EXIT=str(exit_code))
        if 'stderr' in paths:
            env['DIAG_STDERR'] = str(paths['stderr'])
        return replace(self.config, environment={**self.config.environment, **env})

    def deliver(self, config=None, fails=False):
        if fails:
            with self.assertRaises(ExecutionError):
                execute_recoverable_v2_work(self.text, 'feature', 'Change source.py', self.work, config or self.config)
            return None
        return execute_recoverable_v2_work(self.text, 'feature', 'Change source.py', self.work, config or self.config)

    def configure(self, **agents):
        for name, fields in agents.items():
            self.data['agents'][name].update(fields)
        self.text = json.dumps(self.data); (self.target/'.switchyard/config.yaml').write_text(self.text)
        self.git('commit', '-qam', 'diagnostics config'); self.base = self.git('rev-parse', 'HEAD')


class ProcessMetadataTests(DiagnosticsBase):
    # 1, 11, 18, 20
    def test_a_successful_delivery_records_one_complete_diagnostic_per_invocation(self):
        self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
        records = self.diags()
        canonical = [e for e in self.canonical() if e.kind == 'invocation_started']
        self.assertEqual([d['invocation_id'] for d in records], [e.invocation_id for e in canonical])
        self.assertEqual([(d['state'], d['provider']) for d in records], [('SPECIFY', 'codex'), ('IMPLEMENT', 'claude'), ('VERIFY', 'command')])
        for d in records:
            p = d['process']
            self.assertEqual((d['completeness'], d['observed_by'], d['recorded']), ('complete', 'runtime', True))
            self.assertEqual((p['launched'], p['exit_code'], p['timed_out'], p['termination_signal'], p['launches']), (True, 0, False, None, 1))
            self.assertEqual((d['timeout_seconds'], d['duration_source']), (5, 'process'))
            self.assertIsInstance(d['duration_ms'], int)
            self.assertEqual((d['result']['produced_stage_result'], d['result']['outcome_category'], d['result']['reason']),
                             (True, 'semantic_outcome', None))
        self.assertEqual([d['result']['outcome'] for d in records], ['ready', 'ready', 'pass'])
        # 18: identities agree with the trace; the trace links each finished invocation to its diagnostic.
        events = self.assert_consistent()
        for d in records:
            started = [e for e in events if e['event_type'] == 'invocation_started' and e['invocation_id'] == d['invocation_id']][0]
            self.assertEqual({k: started[k] for k in ('work_id', 'run_id', 'state_visit_id', 'state', 'rung', 'agent', 'provider', 'model')},
                             {k: d[k] for k in ('work_id', 'run_id', 'state_visit_id', 'state', 'rung', 'agent', 'provider', 'model')})
            finished = [e for e in events if e['event_type'] == 'invocation_finished' and e['invocation_id'] == d['invocation_id']][0]
            self.assertEqual(finished['payload']['diagnostic_id'], d['diagnostic_id'])
        # Claude: documented envelope facts only. No LLM-provider stderr is retained, only its size.
        claude, codex = records[1], records[0]
        self.assertEqual(claude['provider_channel'], dict(decodable=True, result_envelope=True, subtype='success', is_error=False))
        self.assertEqual(claude['structured_output']['valid'], True)
        for d in (claude, codex):
            self.assertEqual(d['stderr_excerpt'], dict(stream='stderr', available=False, reason='unstructured_output_not_retained',
                                                       bytes_seen=len(b'operational stderr chatter\n')))
        self.assertNotIn(b'operational stderr chatter', b''.join(self.diag_bytes().values()))
        self.assertEqual(codex['structured_output']['repair'], dict(mechanism='none', max_attempts=1, attempted=False, succeeded=None))
        # 11: no provider stdout, result payload, prompt or request text is persisted.
        data = b''.join(self.diag_bytes().values())
        for leaked in (b'operational prose must not be parsed', b'progress: operational stdout', b'reasoning 0',
                       b'STAGE OUTPUT CONTRACT', b'EXECUTION BINDING', b'Human task', b'bounded feature'):
            self.assertNotIn(leaked, data)
        # 20: zero additional provider processes.
        self.assertEqual(len(self.calls()), 2)

    # 2, 13, 14, 18
    def test_a_nonzero_exit_and_its_retry_get_one_diagnostic_each(self):
        self.interrupted()
        failed = self.of('IMPLEMENT')
        self.assertEqual(len(failed), 1)
        d = failed[0]
        self.assertEqual((d['process']['exit_code'], d['process']['launched'], d['result']['classification'], d['result']['reason'],
                          d['result']['semantic_invocation'], d['result']['produced_stage_result']),
                         (7, True, 'provider_exit', 'provider_failure', False, False))
        self.assertEqual(d['summary'], 'Provider exited with status 7 before producing a valid stage result.')
        op = [o for o in _operations(_store(self.work)) if o['phase'] == 'interrupted'][-1]
        self.assertEqual((op['provider_failure']['classification'], op['returncode']), (d['result']['classification'], 7))  # Same taxonomy.
        stop = trace_events(self.work)[-1]
        self.assertEqual((stop['event_type'], stop['payload']['diagnostic_id']), ('run_interrupted', d['diagnostic_id']))
        before = self.diag_bytes()
        resume_v2_work(self.work, self.text, self.config)
        after = self.diag_bytes()
        self.assertTrue(all(after[k] == v for k, v in before.items()))  # 14: finalized records never rewritten.
        implement = self.of('IMPLEMENT')
        self.assertEqual(len(implement), 2)  # 13: the retry is a separate invocation with its own record.
        self.assertEqual(len({x['invocation_id'] for x in implement} | {x['diagnostic_id'] for x in implement}), 4)
        self.assertEqual({x['state_visit_id'] for x in implement}, {d['state_visit_id']})
        self.assertEqual([x['result']['outcome'] or x['result']['classification'] for x in implement], ['provider_exit', 'ready'])
        self.assertEqual(len(self.diags()), len([e for e in self.canonical() if e.kind == 'invocation_started']))
        self.assert_consistent()

    # 3
    def test_a_timeout_records_the_configured_timeout_and_the_kill(self):
        self.configure(spec=dict(timeout=1))
        self.deliver(replace(self.config, environment={**self.config.environment, 'FAKE_MODE': 'timeout'}), fails=True)
        d = self.of('SPECIFY')[0]
        p = d['process']
        self.assertEqual((p['timed_out'], p['terminated_by'], p['termination_signal'], p['exit_code'], d['timeout_seconds']),
                         (True, 'switchyard_timeout', 'SIGKILL', None, 1))
        self.assertGreaterEqual(d['duration_ms'], 1000)
        self.assertEqual((d['result']['classification'], d['result']['reason']), ('timeout', 'provider_timeout'))
        self.assertIn('timed out after 1s', d['summary'])

    # 4
    def test_a_launch_failure_is_distinct_from_a_failed_execution(self):
        (self.root/'claude').write_text('#!/nonexistent/interpreter\n'); (self.root/'claude').chmod(0o755)
        self.deliver(fails=True)
        d = self.of('IMPLEMENT')[0]
        self.assertEqual((d['process']['launched'], d['process']['exit_code'], d['process']['duration_ms']), (False, None, None))
        self.assertEqual((d['result']['classification'], d['duration_source']), ('provider_unavailable', 'interrupted_operation'))
        self.assertIn('could not be started', d['summary'])


class ResultFailureTests(DiagnosticsBase):
    # 5, 12
    def test_a_semantic_verifier_failure_is_not_an_infrastructure_failure_and_links_its_evidence(self):
        self.configure(tests=dict(command=[sys.executable, '-c', 'import sys; print("1 test failed"); sys.exit(1)']))
        self.assertEqual(self.start().result.terminal_state, 'NEEDS_HUMAN')
        v = self.of('VERIFY')[0]
        self.assertEqual((v['result']['produced_stage_result'], v['result']['outcome'], v['result']['outcome_category'],
                          v['result']['semantic_invocation'], v['result']['classification']), (True, 'fail', 'semantic_outcome', True, None))
        ver = v['verification']
        self.assertEqual((ver['verifier'], ver['exit_code'], ver['outcome'], ver['timed_out'], ver['evidence_artifact']),
                         ('tests', 1, 'fail', False, 'verifier_evidence'))
        self.assertEqual(ver['executable'], os.path.basename(sys.executable))
        finished = trace_events(self.work, invocation_id=v['invocation_id'], event_type='invocation_finished')[0]
        self.assertEqual(ver['evidence_refs'], [r for r in finished['payload']['artifact_refs'] if r['kind'] == 'artifact'])
        self.assertTrue((self.work/ver['evidence_refs'][0]['path']).is_file())  # Navigable, never copied:
        self.assertNotIn(b'1 test failed', b''.join(self.diag_bytes().values()))
        self.assertEqual(self.of('IMPLEMENT')[0]['followed_by_verification'],
                         dict(invocation_id=v['invocation_id'], verifier='tests', outcome='fail', exit_code=1, basis='outcome_transition'))

    # 6, 8
    def test_an_invalid_structured_result_is_bounded_useful_and_never_retained(self):
        result = dict(outcome='ready', produced_artifacts=[dict(name='implementation_handoff', content='RAW_SOURCE_PAYLOAD def f(): pass',
                                                                **{'bogus_%d' % i: 'RAW_VALUE' for i in range(30)})],
                      handoff=None, diagnostics=None, extra_top='RAW_TOP')
        envelope = json.dumps(dict(type='result', subtype='success', is_error=False, structured_output=result)).encode()
        self.deliver(self.claude_emits(envelope), fails=True)
        d = self.of('IMPLEMENT')[0]
        s = d['structured_output']
        self.assertEqual((d['result']['classification'], d['result']['produced_stage_result'], s['valid'], s['raw_result_retained']),
                         ('invalid_stage_result', False, False, False))
        self.assertEqual(d['process']['exit_code'], 0)
        self.assertLessEqual(len(s['errors']), diagnostics.MAX_ERRORS)
        self.assertIn(dict(path='produced_artifacts[0].bogus_0', error='additional property is not allowed',
                           allowed_fields=['name', 'content']), s['errors'])
        self.assertIn('extra_top', s['error'])
        self.assertIn('exited normally but its structured result was invalid', d['summary'])
        data = b''.join(self.diag_bytes().values())
        for leaked in (b'RAW_SOURCE_PAYLOAD', b'RAW_VALUE', b'RAW_TOP'):
            self.assertNotIn(leaked, data)

    # 7
    def test_structured_output_repair_stays_with_its_one_invocation(self):
        self.deliver(replace(self.config, environment={**self.config.environment, 'FAKE_SCHEMA_INVALID_ATTEMPTS': '1'}))
        implement = self.of('IMPLEMENT')
        self.assertEqual((len(implement), sum(c['provider'] == 'claude' for c in self.calls())), (1, 1))
        self.assertEqual((implement[0]['structured_output']['valid'], implement[0]['structured_output']['repair']['attempted']), (True, None))

    def test_exhausted_repair_is_one_invocation_with_the_validation_path(self):
        self.deliver(replace(self.config, environment={**self.config.environment, 'FAKE_SCHEMA_INVALID_ATTEMPTS': '5'}), fails=True)
        implement = self.of('IMPLEMENT')
        self.assertEqual((len(implement), sum(c['provider'] == 'claude' for c in self.calls())), (1, 1))
        d = implement[0]
        self.assertEqual((d['result']['classification'], d['process']['exit_code']), ('invalid_stage_result', 1))
        self.assertEqual(d['structured_output']['repair'], dict(mechanism='provider_native_structured_output', max_attempts=3,
                                                                attempted=True, succeeded=False))
        self.assertEqual([e['path'] for e in d['structured_output']['errors']], ['produced_artifacts[0].content_name'])
        self.assertEqual(d['provider_channel']['subtype'], 'error_max_structured_output_retries')


class StreamTests(DiagnosticsBase):
    # 9
    def test_stderr_content_is_never_held_and_pipes_still_drain(self):
        config = ProviderRuntimeConfig(str(self.root))
        with observe_invocation() as facts:
            out = execute(sys.executable, ['-c', "import os\nfor _ in range(256): os.write(2, b'x'*4096)\nprint('done')"], config,
                          result_stdout=True)
        self.assertEqual((out.returncode, out.stdout), (0, b'done\n'))  # Pipes drained: no deadlock.
        self.assertEqual(facts.stderr_bytes, 256*4096)
        self.assertFalse(hasattr(facts, 'stderr'))  # Only a count; no stream bytes are kept in memory.

    # Regression (finalization fix 1): Claude stderr the heuristic gate would accept is still never persisted.
    def test_plausible_claude_stderr_with_source_is_not_persisted_but_structured_facts_are(self):
        source = 'Traceback (most recent call last):\n  File "billing.py", line 7\n    def proprietary_rate(customer): return 0.42\n'
        self.assertEqual(sanitize(source)[1], None)  # The heuristic alone would have kept it: not proof of safety.
        envelope = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True, api_error_status=529,
                                   errors=['Overloaded'])).encode()
        self.deliver(self.claude_emits(envelope, source.encode(), exit_code=1), fails=True)
        d = self.of('IMPLEMENT')[0]
        self.assertEqual(d['stderr_excerpt'], dict(stream='stderr', available=False, reason='unstructured_output_not_retained',
                                                   bytes_seen=len(source.encode())))
        self.assertEqual((d['provider_channel']['subtype'], d['provider_channel']['api_error_status'], d['provider_channel']['errors']),
                         ('error_during_execution', 529, ['Overloaded']))
        self.assertEqual((d['process']['exit_code'], d['process']['launched'], d['result']['classification']),
                         (1, True, 'provider_unavailable'))
        data = b''.join(self.diag_bytes().values())
        self.assertNotIn(b'proprietary_rate', data); self.assertNotIn(b'billing.py', data)
        self.assertIn('stderr: not retained (unstructured_output_not_retained, %d bytes seen)' % len(source.encode()),
                      self.cli('--invocation', 'I2')[1])
        path = self.work/'observability/diagnostics'/diagnostics.file_name(d['invocation_id'])
        self.assertLessEqual(path.stat().st_size, diagnostics.MAX_RECORD)
        self.assertLessEqual(len(d['summary'].encode()), diagnostics.MAX_SUMMARY)

    # 10
    def test_synthetic_credentials_never_persist(self):
        leaks = ['Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123', 'fatal: https://user:hunter2pass@example.com/repo.git',
                 'export ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstu', 'token=' + SECRET, 'using '+SECRET,
                 'aws AKIAABCDEFGHIJKLMNOP', 'key Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm9', '-----BEGIN RSA PRIVATE KEY-----']
        for leak in leaks:
            self.assertEqual(sanitize('error: provider failed\n'+leak), (None, 'unsafe_unstructured_output'), leak)
        self.assertEqual(sanitize('A=1\nB=2\nCCC=3\nDDD=4\nEEE=5'), (None, 'unsafe_unstructured_output'))  # Environment dump.
        self.assertEqual(sanitize('\x1b[31merror:\x1b[0m rate limited'), ('error: rate limited', None))
        stderr = ('error: provider failed\n'+'\n'.join(leaks)).encode()
        envelope = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True,
                                   errors=['auth failed for token '+SECRET, 'plain provider error'])).encode()
        config = self.claude_emits(envelope, stderr, exit_code=1)
        config = replace(config, environment={**config.environment, 'GH_TOKEN': SECRET, 'SECRET_ENV': SECRET})
        self.deliver(config, fails=True)
        d = self.of('IMPLEMENT')[0]
        self.assertEqual((d['stderr_excerpt']['available'], d['stderr_excerpt']['reason']), (False, 'unstructured_output_not_retained'))
        self.assertEqual(d['provider_channel']['errors'], ['plain provider error'])
        self.assertEqual(d['provider_channel']['errors_omitted'], dict(count=1, reason='unsafe_unstructured_output'))
        data = b''.join(self.diag_bytes().values())
        for secret in (SECRET, 'hunter2pass', 'abcdefghijklmnopqrstuvwxyz0123', 'sk-ant-', 'AKIAABCDEFGHIJKLMNOP', 'PRIVATE KEY'):
            self.assertNotIn(secret.encode(), data)


class RecoveryTests(DiagnosticsBase):
    # 14, 15, 16
    def test_a_diagnostic_write_failure_changes_nothing_and_proven_facts_are_backfilled(self):
        with patch('switchyard.diagnostics._publish', side_effect=OSError('disk full')):
            execution = self.start()
        self.assertEqual((execution.result.terminal_state, len(self.calls())), ('READY_FOR_MANUAL_QA', 2))
        self.assertEqual(recovery_status(self.work)['terminal'], 'READY_FOR_MANUAL_QA')
        events = self.assert_consistent()
        self.assertEqual(len(events), 22)  # The trace is unaffected and links nothing that does not exist.
        self.assertEqual({e['payload']['diagnostic_id'] for e in events if e['event_type'] == 'invocation_finished'}, {None})
        missing = self.diags()  # 15: the read API reports explicitly unknown, persists nothing.
        self.assertEqual({(d['completeness'], d['recorded'], d['result']) for d in missing}, {('unavailable', False, None)})
        self.assertEqual(self.diag_bytes(), {})
        first = len(missing)
        self.candidate(execution)
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.config)  # Next drive: backfill, then live.
        records = self.diags()
        backfilled, live = records[:first], records[first:]
        self.assertEqual({(d['observed_by'], d['completeness']) for d in backfilled}, {('recovery_backfill', 'partial')})
        self.assertEqual({(d['observed_by'], d['completeness']) for d in live}, {('runtime', 'complete')})
        for d in backfilled:  # Only what events.jsonl proves; streams and process metadata stay unknown.
            self.assertEqual((d['process'], d['stderr_excerpt']['reason'], d['provider_channel'], d['artifact_refs'], d['duration_source']),
                             (None, 'not_captured', None, None, 'canonical_event'))
            self.assertTrue(d['result']['produced_stage_result'])
        self.assertEqual([d['result']['outcome'] for d in backfilled], ['ready', 'ready', 'pass'])
        before = self.diag_bytes()
        writer = Diagnostics(self.work, records[0]['work_id'])
        writer.bind(self.compiled())
        writer.backfill(self.canonical(), _operations(_store(self.work)))  # A retried recovery: nothing new, nothing rewritten.
        self.assertEqual(self.diag_bytes(), before)

    def test_an_unwritten_interruption_diagnostic_is_backfilled_from_its_operation_record(self):
        with patch('switchyard.diagnostics._publish', side_effect=OSError('disk full')):
            self.interrupted()
        self.assertEqual(self.of('IMPLEMENT')[0]['completeness'], 'unavailable')
        resume_v2_work(self.work, self.text, self.config)
        failed, retry = self.of('IMPLEMENT')
        self.assertEqual((failed['observed_by'], failed['completeness'], failed['process']['source'], failed['process']['exit_code'],
                          failed['process']['launched'], failed['result']['classification'], failed['result']['semantic_invocation']),
                         ('recovery_backfill', 'partial', 'interrupted_operation', 7, None, 'provider_exit', False))
        self.assertEqual(failed['stderr_excerpt']['reason'], 'not_captured')
        self.assertEqual((retry['observed_by'], retry['result']['outcome']), ('runtime', 'ready'))
        stop = [e for e in trace_events(self.work) if e['event_type'] == 'run_interrupted'][0]
        self.assertIsNone(stop['payload']['diagnostic_id'])  # Never rewritten after the fact; the read API joins.
        self.assertEqual(self.diags(invocation=stop['invocation_id'])[0]['diagnostic_id'], failed['diagnostic_id'])

    # 15 (controller killed during an invocation: only invocation_started is durable)
    def test_an_invocation_without_a_proven_end_gets_no_invented_diagnostic(self):
        self.start()
        history = self.canonical()
        cut = max(i for i, e in enumerate(history) if e.kind == 'invocation_started')
        shutil.rmtree(self.work/'observability/diagnostics')
        writer = Diagnostics(self.work, 'work'); writer.bind(self.compiled())
        writer.backfill(history[:cut+1], [])
        proven = [d for d in self.diags() if d['recorded']]
        self.assertEqual([d['state'] for d in proven], ['SPECIFY', 'IMPLEMENT'])  # Finished ones only.
        pending = self.diags(invocation=history[cut].invocation_id)[0]
        self.assertEqual((pending['completeness'], pending['result'], pending['recorded']), ('unavailable', None, False))

    # 16
    def test_diagnostic_and_capture_bugs_never_change_execution(self):
        with patch('switchyard.diagnostics.Diagnostics._build', side_effect=RuntimeError('bug')), \
                patch('switchyard.provider_process.InvocationFacts.note', side_effect=RuntimeError('bug')):
            self.interrupted()
        op = [o for o in _operations(_store(self.work)) if o['phase'] == 'interrupted'][-1]
        self.assertEqual((op['reason'], op['semantic_invocation'], op['resume_safe'], op['provider_failure']['classification']),
                         ('provider_failure', False, True, 'provider_exit'))
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(len(self.calls()), 3)

    def compiled(self):
        from switchyard.recovery import _Projection
        from switchyard.simple_config import simple_config_from_text
        from switchyard.workflow_compiler import compile_workflow
        return compile_workflow(simple_config_from_text(self.text).bundle, 'feature', lambda b: _Projection())


class VerificationLinkTests(unittest.TestCase):
    # Regression (finalization fix 2): a verifier is linked only through the invocation's own outcome transition.
    def setUp(self):
        from tempfile import TemporaryDirectory
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def history(self, *events):
        from runtime import Event
        from runtime.recording import JsonlRecorder
        recorder = JsonlRecorder(self.work/'events.jsonl')
        for kind, fields in events:
            recorder.record(Event(kind, 'run:r', **fields))
        directory = self.work/'observability/diagnostics'; directory.mkdir(parents=True)
        for number, provider, outcome in ((1, 'claude', 'ready'), (2, 'command', 'fail')):
            invocation = 'run:r:invocation:%d' % number
            (directory/diagnostics.file_name(invocation)).write_text(json.dumps(dict(
                schema_version=1, invocation_id=invocation, run_id='run:r', provider=provider, agent=provider,
                result=dict(outcome=outcome), verification=dict(exit_code=1) if provider == 'command' else None)))
        return {d['invocation_id']: d for d in read_diagnostics(self.work)}

    def test_the_next_verifier_in_the_run_is_not_linked_without_the_transition_that_entered_it(self):
        records = self.history(
            ('run_started', {}),
            ('state_entered', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1')),
            ('invocation_started', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1', invocation_id='run:r:invocation:1')),
            ('invocation_finished', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1', invocation_id='run:r:invocation:1', outcome='ready')),
            ('transition_selected', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1', invocation_id='run:r:invocation:1',
                                         outcome='ready', target='DECISION')),
            ('state_exited', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1')),
            ('state_entered', dict(state='DECISION', state_visit_id='run:r:visit:2')),
            ('human_input_requested', dict(state='DECISION', state_visit_id='run:r:visit:2', target='VERIFY')),
            ('human_input_received', dict(state='DECISION', state_visit_id='run:r:visit:2', target='VERIFY')),
            ('human_input_resumed', dict(state='DECISION', state_visit_id='run:r:visit:2', target='VERIFY')),
            ('state_exited', dict(state='DECISION', state_visit_id='run:r:visit:2')),
            ('state_entered', dict(state='VERIFY', state_visit_id='run:r:visit:3')),
            ('invocation_started', dict(state='VERIFY', state_visit_id='run:r:visit:3', invocation_id='run:r:invocation:2')))
        self.assertEqual(list(records), ['run:r:invocation:1', 'run:r:invocation:2'])  # The verifier IS the next invocation,
        self.assertIsNone(records['run:r:invocation:1']['followed_by_verification'])  # but a human, not I1, routed to it.

    def test_the_verifier_entered_by_the_outcome_transition_is_linked(self):
        records = self.history(
            ('run_started', {}),
            ('state_entered', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1')),
            ('invocation_started', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1', invocation_id='run:r:invocation:1')),
            ('invocation_finished', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1', invocation_id='run:r:invocation:1', outcome='ready')),
            ('transition_selected', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1', invocation_id='run:r:invocation:1',
                                         outcome='ready', target='VERIFY')),
            ('state_exited', dict(state='IMPLEMENT', state_visit_id='run:r:visit:1')),
            ('state_entered', dict(state='VERIFY', state_visit_id='run:r:visit:2')),
            ('invocation_started', dict(state='VERIFY', state_visit_id='run:r:visit:2', invocation_id='run:r:invocation:2')))
        self.assertEqual(records['run:r:invocation:1']['followed_by_verification'],
                         dict(invocation_id='run:r:invocation:2', verifier='command', outcome='fail', exit_code=1, basis='outcome_transition'))


class ReadTests(DiagnosticsBase):
    # 17
    def test_the_read_api_and_cli_are_read_only_and_prioritize_failures(self):
        self.interrupted()
        resume_v2_work(self.work, self.text, self.config)
        before = self.files(self.work)
        code, text = self.cli()
        lines = text.splitlines()
        self.assertEqual((code, lines[0]), (0, '4 invocation(s), 1 failed'))
        self.assertEqual(lines[1].split(), ['INVOCATION', 'STATE', 'AGENT', 'PROVIDER', 'RESULT', 'DURATION', 'DIAGNOSTIC'])
        self.assertEqual(lines[2].split()[:5], ['I2', 'IMPLEMENT', 'code', 'claude', 'provider_exit'])  # Failed first.
        self.assertEqual([l.split()[0] for l in lines[3:]], ['I1', 'I3', 'I4'])
        code, detail = self.cli('--invocation', 'I2')
        for expected in ('State: IMPLEMENT', 'Agent: code', 'Provider: claude', 'Model: sonnet', 'Exit code: 7',
                         'Timeout configured: 5s', 'Provider exited with status 7 before producing a valid stage result.',
                         'Charged as attempt: no', 'Verification:\n  Not executed', 'Completeness: complete'):
            self.assertIn(expected, detail)
        full = self.diags()[1]['invocation_id']
        self.assertEqual(self.cli('--invocation', full)[1], detail)
        code, out = self.cli('--json')
        self.assertEqual(json.loads(out), self.diags())
        self.assertEqual(self.cli('--invocation', 'I99'), (1, 'no such invocation\n'))
        self.assertEqual(self.files(self.work), before)  # Reading never writes, backfills or repairs.
        shutil.rmtree(self.work/'observability')  # Legacy work: canonical invocations, explicitly without diagnostics.
        self.assertEqual({d['completeness'] for d in self.diags()}, {'unavailable'})
        self.assertIn('Diagnostic: unavailable', self.cli('--invocation', 'I1')[1])
        os.unlink(self.work/'events.jsonl')
        self.assertEqual(self.cli(), (0, 'no diagnostics available\n'))


class FlowTests(DiagnosticsBase):
    # 19
    def test_qa_reopen_adds_diagnostics_without_touching_earlier_ones(self):
        execution = self.start(); self.candidate(execution)
        before = self.diag_bytes()
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.config)
        after = self.diag_bytes()
        self.assertTrue(all(after[k] == v for k, v in before.items()))
        self.assertEqual(len(self.diags()), len([e for e in self.canonical() if e.kind == 'invocation_started']))
        self.assertEqual({d['observed_by'] for d in self.diags()}, {'runtime'})


class HumanInputDiagnosticsTests(unittest.TestCase):
    _base = test_human_input.HumanInputTests
    setUp, setUp_base, git, save_config, start, answer = (_base.setUp, _base.setUp_base, _base.git, _base.save_config,
                                                          _base.start, _base.answer)

    # 19
    def test_a_human_wait_and_answer_keep_one_diagnostic_per_invocation(self):
        self.start()
        waiting = read_diagnostics(self.work)
        # Stub stages launch no process: live records, honestly partial (no process metadata exists).
        self.assertTrue(waiting and all((d['observed_by'], d['completeness'], d['process']) == ('runtime', 'partial', None) for d in waiting))
        self.assertEqual(waiting[-1]['result']['outcome'], 'needs_clarification')
        self.answer()
        records = read_diagnostics(self.work)
        self.assertEqual(records[:len(waiting)], waiting)
        self.assertEqual(len({d['invocation_id'] for d in records}), len(records))
        self.assertEqual(len(records), len([e for e in trace_events(self.work) if e['event_type'] == 'invocation_started']))


if __name__ == '__main__':
    unittest.main()

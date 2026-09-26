import contextlib
import io
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from examples.delivery.__main__ import main
from examples.delivery.presets import build_change
from runtime import ExecutorResult, InvocationResponse, RunContext, WorkUnit, run
from runtime.codex import CodexExecutor
from switchyard.observability import project_runs, read_events, summarize_runs
from runtime.recording import JsonlRecorder


VERIFY = "from pathlib import Path; import sys; ok=Path('result.txt').read_text()=='ok'; print('deterministic failure evidence' if not ok else 'passed'); sys.exit(0 if ok else 1)"


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.calls = []
        self.initial_good = False
        self.repair_good = False
        self.refuse = False
        self.omit_analysis = False

    def build(self, kind='deliver'):
        return build_change(kind, workspace=str(self.root), codex_executable='/fake/codex',
                            verifier_executable=sys.executable, verifier_argv=('-c', VERIFY))

    def agent(self, request):
        prompt = request.work_unit.task
        path = Path(re.search(r'Analysis artifact: (.*)', prompt).group(1))
        stage = ('diagnosis' if prompt.startswith('Perform FRESH_DIAGNOSIS') else
                 'analysis' if prompt.startswith('Inspect the requested') else
                 'repair' if prompt.startswith('Make ONE') else 'implementation')
        self.calls.append((stage, request, path))
        if stage in ('analysis', 'diagnosis'):
            if not self.omit_analysis:
                path.write_text('Observed behavior\nConstraints\nProposed change\nAffected areas\nRisks\nVerification approach\n')
        elif stage == 'implementation':
            self.assertTrue(path.is_file())
            (self.root / 'result.txt').write_text('ok' if self.initial_good else 'bad')
            if self.refuse:
                (path.parent / 'blocked.txt').write_text('repository contradicts analysis')
        else:
            self.assertIsNotNone(request.diagnostics)
            self.assertEqual(request.diagnostics.origin.state, 'VERIFY')
            self.assertIn(b'deterministic failure evidence', request.diagnostics.payload.utf8)
            self.assertLessEqual(len(request.diagnostics.payload.utf8), 4096)
            (self.root / 'result.txt').write_text('ok' if self.repair_good else 'bad')
        return InvocationResponse(ExecutorResult('success'))

    def execute(self, loaded, recorder=None):
        with patch.object(CodexExecutor, 'run', side_effect=self.agent):
            return run(WorkUnit('ORIGINAL TASK'), loaded.flow, loaded.executors,
                       context=RunContext('change-123', 'external-parent'), recorder=recorder)

    def test_deliver_three_required_itineraries(self):
        for first, repaired, terminal in ((True, False, 'READY_FOR_MANUAL_QA'),
                                         (False, True, 'READY_FOR_MANUAL_QA'),
                                         (False, False, 'NEEDS_REDIAGNOSIS')):
            with self.subTest(first=first, repaired=repaired):
                self.calls = []
                self.initial_good, self.repair_good = first, repaired
                result = self.execute(self.build())
                expected = ['ANALYZE', 'IMPLEMENT', 'VERIFY']
                if not first:
                    expected += ['REPAIR_ONCE', 'VERIFY_AGAIN']
                self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'], expected)
                self.assertEqual(result.terminal_state, terminal)
                self.assertEqual([call[0] for call in self.calls], ['analysis', 'implementation'] + ([] if first else ['repair']))
                self.assertTrue(all('ORIGINAL TASK' in call[1].work_unit.task for call in self.calls))
                self.assertLessEqual(sum(e.state == 'REPAIR_ONCE' and e.kind == 'invocation_started' for e in result.events), 1)

    def test_repair_always_starts_with_fresh_diagnosis(self):
        paths = []
        for good, repaired, terminal in ((True, False, 'READY_FOR_MANUAL_QA'),
                                         (False, True, 'READY_FOR_MANUAL_QA'),
                                         (False, False, 'NEEDS_HUMAN')):
            with self.subTest(good=good, repaired=repaired):
                self.calls = []
                self.initial_good, self.repair_good = good, repaired
                result = self.execute(self.build('repair'))
                expected = ['FRESH_DIAGNOSIS', 'FIX', 'VERIFY'] + ([] if good else ['REPAIR_ONCE', 'VERIFY_AGAIN'])
                self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'], expected)
                self.assertEqual(result.terminal_state, terminal)
                prompt = self.calls[0][1].work_unit.task
                self.assertIn('advisory, not authoritative', prompt)
                self.assertIn('actual execution', prompt)
                self.assertIn('Do not modify production code', prompt)
                self.assertEqual(self.calls[0][0], 'diagnosis')
                paths.append(self.calls[0][2])
                self.assertTrue(all(call[2] == paths[-1] for call in self.calls))
        self.assertEqual(len(set(paths)), 3)

    def test_refusal_and_missing_analysis_stop_before_verification(self):
        self.omit_analysis = True
        result = self.execute(self.build())
        self.assertEqual(result.terminal_state, 'NEEDS_REDIAGNOSIS')
        self.assertEqual([c[0] for c in self.calls], ['analysis'])
        self.calls = []
        self.omit_analysis = False
        self.refuse = True
        result = self.execute(self.build())
        self.assertEqual(result.terminal_state, 'NEEDS_REDIAGNOSIS')
        self.assertEqual([c[0] for c in self.calls], ['analysis', 'implementation'])
        self.assertFalse(any(e.state == 'VERIFY' for e in result.events))

    def test_agent_non_success_and_verifier_error_do_not_retry(self):
        for kind, failure in (('deliver', 'NEEDS_REDIAGNOSIS'), ('repair', 'NEEDS_HUMAN')):
            for outcome in ('error', 'timeout', 'cancelled', 'failed'):
                loaded = self.build(kind)
                with patch.object(CodexExecutor, 'run', return_value=InvocationResponse(ExecutorResult(outcome))) as agent:
                    result = run(WorkUnit('t'), loaded.flow, loaded.executors)
                self.assertEqual(result.terminal_state, failure)
                self.assertEqual(agent.call_count, 1)
        loaded = build_change('deliver', workspace=str(self.root), codex_executable='/fake/codex',
                              verifier_executable=str(self.root / 'missing-verifier'), verifier_argv=())
        self.calls = []
        result = self.execute(loaded)
        self.assertEqual(result.terminal_state, 'NEEDS_REDIAGNOSIS')
        self.assertEqual([c[0] for c in self.calls], ['analysis', 'implementation'])

    def test_corrective_error_and_timeout_never_reverify(self):
        for outcome in ('error', 'timeout', 'failed', 'cancelled'):
            self.calls = []
            def agent(request):
                if request.diagnostics is not None:
                    return InvocationResponse(ExecutorResult(outcome))
                return self.agent(request)
            loaded = self.build()
            with patch.object(CodexExecutor, 'run', side_effect=agent):
                result = run(WorkUnit('t'), loaded.flow, loaded.executors)
            self.assertEqual(result.terminal_state, 'NEEDS_REDIAGNOSIS')
            self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'],
                             ['ANALYZE', 'IMPLEMENT', 'VERIFY', 'REPAIR_ONCE'])

    def test_recording_projection_and_application_flags(self):
        self.repair_good = True
        path = self.root / 'events.jsonl'
        result = self.execute(self.build(), JsonlRecorder(path))
        self.assertEqual(tuple(read_events(path)), result.events)
        summary, = summarize_runs(read_events(path))
        self.assertEqual((summary.correlation_id, summary.parent_run_id), ('change-123', 'external-parent'))
        self.assertIsNotNone(summary.run_duration_ns)
        self.assertEqual(project_runs(result.events, lambda events: events[-1].state), ('READY_FOR_MANUAL_QA',))
        self.initial_good = True
        with patch.object(CodexExecutor, 'run', side_effect=self.agent), contextlib.redirect_stdout(io.StringIO()) as out:
            status = main(['deliver', '--workspace', str(self.root), '--codex-executable', '/fake/codex',
                           '--verifier-executable', sys.executable, '--verifier-arg=-c', '--verifier-arg', VERIFY,
                           '--task', 'CLI TASK', '--correlation-id', 'change-123',
                           '--parent-run-id', summary.run_id, '--events-file', str(path)])
        self.assertEqual(status, 0)
        summaries = summarize_runs(read_events(path))
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[1].parent_run_id, summaries[0].run_id)
        self.assertNotIn('CLI TASK', out.getvalue())

    def test_absolute_paths_required_and_repair_bound_explicit(self):
        with self.assertRaises(ValueError):
            build_change('deliver', workspace=str(self.root), codex_executable='codex',
                         verifier_executable=sys.executable, verifier_argv=())
        loaded = self.build()
        self.assertEqual(loaded.flow.states['REPAIR_ONCE'].max_state_visits_per_run, 1)
        self.assertEqual(loaded.flow.states['VERIFY'].executor, loaded.flow.states['VERIFY_AGAIN'].executor)
        self.assertEqual(loaded.flow.states['VERIFY_AGAIN'].on['failed'], 'NEEDS_REDIAGNOSIS')

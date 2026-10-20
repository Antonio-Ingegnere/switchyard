"""Declared human recovery from NEEDS_* terminals; real processes and Git, fake providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from flow_config import FlowConfigError
from runtime import Event, ExecutionError, Flow, State, WorkUnit, run
from switchyard.observability import ObservabilityError, read_events, summarize_runs
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.qa_lifecycle import accept_v2_work, create_qa_candidate
from switchyard.recovery import (RecoveryBlocked, attach_qa_candidate, execute_recoverable_v2_work,
                                 recovery_status, resume_v2_work)
from switchyard.simple_config import simple_config_from_text
from switchyard.work_contract import WorkWorkspace

ROOT = Path(__file__).resolve().parents[1]
# Reads its consumed artifact through the generic channel; the gate lives outside the worktree.
VERIFIER = '''import os,sys,pathlib
d=pathlib.Path(os.environ['SWITCHYARD_ARTIFACTS']);h=d/'implementation_handoff'
pathlib.Path(os.environ['SEEN']).write_text(str(d)+'\\n'+oct(h.stat().st_mode&0o777)+'\\n'+h.read_text())
print('verifier ran')
sys.exit(0 if pathlib.Path(os.environ['GATE']).exists() else 1)
'''


class TerminalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.target = self.root/'target'; self.target.mkdir()
        self.work = self.root/'work'; self.gate = self.root/'gate'; self.seen = self.root/'seen'
        (self.target/'source.py').write_text('before')
        (self.target/'.gitignore').write_text('.switchyard/*\n!.switchyard/config.yaml\n__pycache__/\n')
        (self.target/'.switchyard').mkdir()
        self.data = {'agents': {
            'spec': dict(provider='codex', model='offline', workspace='read', shell=True, timeout=5),
            'code': dict(provider='claude', model='sonnet', workspace='write', shell=True, timeout=5),
            'tests': dict(provider='command', command=[sys.executable, '-c', VERIFIER], capture='verifier_evidence', timeout=5)},
            'flows': {'feature': {'start': 'SPECIFY', 'limits': {'run_invocations': 5}, 'states': {
                'NEEDS_HUMAN': dict(terminal=True), 'READY_FOR_MANUAL_QA': dict(terminal=True),
                'SPECIFY': dict(agent='spec', consumes=['request'], produces={'microspec': dict(format='yaml', schema='microspec')}, on={'ready': 'IMPLEMENT'}),
                'IMPLEMENT': dict(agent='code', consumes=['microspec'], produces={'implementation_handoff': dict(format='markdown')}, on={'ready': 'VERIFY'}),
                'VERIFY': dict(run=[dict(agent='tests', budget={'invocations': 2})], budget={'visits': 2},
                               consumes=['implementation_handoff'], produces={'verifier_evidence': dict(format='markdown')},
                               on={'pass': 'READY_FOR_MANUAL_QA', 'fail': 'NEEDS_REDIAGNOSIS'}),
                'NEEDS_REDIAGNOSIS': dict(terminal=True, resume_to='VERIFY')}}}}
        self.text = self.save()
        for args in [('init', '-b', 'main'), ('config', 'user.name', 'Recovery Tests'), ('config', 'user.email', 'tests@example.invalid'),
                     ('config', 'commit.gpgsign', 'false'), ('add', '--', '.gitignore', '.switchyard/config.yaml', 'source.py'), ('commit', '-m', 'baseline')]:
            self.git(*args)
        self.log = self.root/'calls'; plan = self.root/'plan'; plan.write_text('{"artifacts":{}}')
        script = (ROOT/'tests/fixtures/provider_cli.py').read_text().replace(
            'outcome=plan.get', "if state=='IMPLEMENT':\n    Path('source.py').write_text('finished')\noutcome=plan.get")
        for name in ('claude', 'codex'):
            exe = self.root/name; exe.write_text('#!'+sys.executable+'\n'+script); exe.chmod(0o755)
        self.config = ProviderRuntimeConfig(str(self.target), str(self.root/'claude'), str(self.root/'codex'), environment={
            **os.environ, 'FAKE_PLAN': str(plan), 'FAKE_LOG': str(self.log), 'FAKE_CONTRACT_AWARE': '1',
            'GATE': str(self.gate), 'SEEN': str(self.seen)})

    def git(self, *args): return subprocess.check_output(['git', *args], cwd=self.target, stderr=subprocess.DEVNULL).decode().strip()
    def save(self):
        text = json.dumps(self.data); (self.target/'.switchyard/config.yaml').write_text(text); return text
    def states(self): return [json.loads(l)['binding']['logical_state'] for l in self.log.read_text().splitlines()]
    def events(self): return tuple(read_events(self.work/'events.jsonl'))

    def stopped(self):
        execution = execute_recoverable_v2_work(self.text, 'feature', 'Human task', self.work, self.config)
        self.assertEqual(execution.result.terminal_state, 'NEEDS_REDIAGNOSIS')
        return execution

    def test_resume_enters_declared_target_in_same_run(self):
        self.stopped(); before = self.events(); workspace = WorkWorkspace.open(self.work)
        handoff = workspace.read_artifact('implementation_handoff')
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['resume_allowed'], status['resume_to']), ('NEEDS_REDIAGNOSIS', True, 'VERIFY'))
        # Stays stopped until a human asks: nothing ran after the terminal.
        self.assertEqual(self.events(), before)
        self.gate.write_text('environment repaired outside the worktree')
        execution = resume_v2_work(self.work, self.text, self.config, reason='verification failures confirmed pre-existing')
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        events = self.events()
        self.assertEqual(events[:len(before)], before)
        self.assertEqual(len({e.run_id for e in events}), 1)
        self.assertEqual(self.states(), ['SPECIFY', 'IMPLEMENT'])  # Successful model states never rerun.
        recovered = [e for e in events if e.kind == 'run_resumed']
        self.assertEqual([(e.state, e.target) for e in recovered], [('NEEDS_REDIAGNOSIS', '@v2:feature:VERIFY:0')])
        entered = [e.state for e in events if e.kind == 'state_entered']
        self.assertEqual(entered, ['@v2:feature:SPECIFY:0', '@v2:feature:IMPLEMENT:0', '@v2:feature:VERIFY:0',
                                   'NEEDS_REDIAGNOSIS', '@v2:feature:VERIFY:0', 'READY_FOR_MANUAL_QA'])
        self.assertEqual(len([e for e in events if e.kind == 'invocation_started']), 4)
        summary, = summarize_runs(events)
        self.assertTrue(summary.completed_normally); self.assertEqual(summary.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual((workspace.read_artifact('implementation_handoff'), len(workspace.versions('microspec'))), (handoff, 1))
        self.assertEqual(len(workspace.versions('verifier_evidence')), 2)
        store = WorkWorkspace.open(self.work/'recovery')
        records = [json.loads(store.read_artifact('operation', r.version)) for r in store.versions('operation')]
        self.assertIn(dict(from_terminal='NEEDS_REDIAGNOSIS', resume_to='VERIFY', reason='verification failures confirmed pre-existing'),
                      [r.get('recovery') for r in records])
        # Consumed artifacts reached the command read-only, and the channel is removed afterwards.
        directory, mode, content = self.seen.read_text().split('\n', 2)
        self.assertEqual((mode, content), ('0o400', handoff)); self.assertFalse(Path(directory).exists())
        self.assertFalse(Path(directory).resolve().is_relative_to(self.target))
        # The recovered run remains an ordinary QA candidate.
        qa = self.root/'qa'; create_qa_candidate(self.target, execution.workspace, execution.result, 'feature', qa)
        attach_qa_candidate(self.work, qa)
        self.assertEqual(accept_v2_work(self.target, qa).terminal, 'ACCEPTED')
        self.assertEqual(self.git('show', 'HEAD:source.py'), 'finished')

    def test_budgets_are_retained_across_recoveries(self):
        self.stopped()
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'NEEDS_REDIAGNOSIS')
        before = self.events()
        status = recovery_status(self.work)
        self.assertEqual((status['resume_to'], status['resume_allowed']), ('VERIFY', False))
        with self.assertRaisesRegex(RecoveryBlocked, 'RESUME_BLOCKED: invocation_budget_exhausted'):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(self.events(), before)
        self.assertEqual(len([e for e in before if e.kind == 'run_resumed']), 1)

    def test_refuses_without_declared_route(self):
        del self.data['flows']['feature']['states']['NEEDS_REDIAGNOSIS']['resume_to']; self.text = self.save()
        self.stopped(); before = self.events()
        self.assertEqual((recovery_status(self.work)['resume_allowed'], recovery_status(self.work)['resume_to']), (False, None))
        self.gate.write_text('ready')
        with self.assertRaisesRegex(RecoveryBlocked, 'RESUME_BLOCKED: no_declared_recovery_route'):
            resume_v2_work(self.work, self.text, self.config, reason='please continue')
        self.assertEqual(self.events(), before); self.assertEqual(self.states(), ['SPECIFY', 'IMPLEMENT'])

    def test_drift_and_semantic_changes_block(self):
        self.stopped(); before = self.events()
        (self.target/'source.py').write_text('human edit')
        with self.assertRaisesRegex(RecoveryBlocked, 'checkpoint_drift.*source.py'):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((self.target/'source.py').read_text(), 'human edit')
        (self.target/'source.py').write_text('finished')
        self.data['flows']['feature']['states']['NEEDS_REDIAGNOSIS']['resume_to'] = 'IMPLEMENT'
        with self.assertRaisesRegex(RecoveryBlocked, 'semantic_configuration_changed'):
            resume_v2_work(self.work, self.save(), self.config)
        self.assertEqual(self.events(), before)

    def test_terminal_recovery_requires_executable_target(self):
        states = self.data['flows']['feature']['states']
        for name, target, message in (('NEEDS_REDIAGNOSIS', 'READY_FOR_MANUAL_QA', 'nonterminal state'),
                                      ('NEEDS_REDIAGNOSIS', 'MISSING', 'nonterminal state')):
            data = json.loads(json.dumps(self.data)); data['flows']['feature']['states'][name] = dict(terminal=True, resume_to=target)
            with self.assertRaisesRegex(FlowConfigError, message):
                simple_config_from_text(json.dumps(data))
        shown = simple_config_from_text(self.text).show()['flows']['feature']['states']
        self.assertEqual((shown['NEEDS_REDIAGNOSIS']['resume_to'], shown['VERIFY']['resume_to']), ('VERIFY', None))
        self.assertIn('VERIFY', states)


class RuntimeRecoveryContract(unittest.TestCase):
    class Fixed:
        def __init__(self, outcome): self.outcome = outcome
        def run(self, request):
            from runtime import ExecutorResult, InvocationResponse
            return InvocationResponse(ExecutorResult(self.outcome))

    def flow(self):
        return Flow('A', {'A': State('a', {'fail': 'STOP', 'pass': 'DONE'}, max_state_visits_per_run=2),
                          'STOP': State(terminal=True, resume_to='A'), 'DONE': State(terminal=True)})

    def test_recover_to_requires_finished_history_and_nonterminal_target(self):
        first = run(WorkUnit('w'), self.flow(), {'a': self.Fixed('fail')})
        self.assertEqual(first.terminal_state, 'STOP')
        for history, target in ((first.events, 'DONE'), (first.events, 'MISSING'), (first.events[:-1], 'A'), (None, 'A')):
            with self.assertRaises(ValueError):
                run(WorkUnit('w'), self.flow(), {'a': self.Fixed('pass')}, resume_from=history, recover_to=target)
        with self.assertRaisesRegex(ValueError, 'interrupted invocation'):
            run(WorkUnit('w'), self.flow(), {'a': self.Fixed('pass')}, resume_from=first.events)
        second = run(WorkUnit('w'), self.flow(), {'a': self.Fixed('pass')}, resume_from=first.events, recover_to='A')
        self.assertEqual(second.terminal_state, 'DONE')
        self.assertEqual(summarize_runs(second.events)[0].terminal_state, 'DONE')
        # Visit budgets continue from history: a third visit of A is refused, never reset.
        again = run(WorkUnit('w'), self.flow(), {'a': self.Fixed('fail')}, resume_from=first.events, recover_to='A')
        self.assertEqual(again.terminal_state, 'STOP')
        with self.assertRaises(ExecutionError) as refused:
            run(WorkUnit('w'), self.flow(), {'a': self.Fixed('pass')}, resume_from=again.events, recover_to='A')
        self.assertEqual([e.metric for e in refused.exception.events if e.kind == 'budget_exceeded'], ['max_state_visits_per_run'])
        self.assertEqual(sum(e.kind == 'invocation_started' for e in refused.exception.events), 2)

    def test_observability_rejects_recovery_without_finished_terminal(self):
        first = run(WorkUnit('w'), self.flow(), {'a': self.Fixed('fail')})
        forged = first.events[:-1] + (Event('run_resumed', first.events[0].run_id, 'STOP', target='A'),)
        with self.assertRaisesRegex(ObservabilityError, 'expected completion|recovery without'):
            summarize_runs(forged)
        wrong = first.events + (Event('run_resumed', first.events[0].run_id, 'DONE', target='A'),)
        with self.assertRaisesRegex(ObservabilityError, 'recovery without a finished terminal'):
            summarize_runs(wrong)


if __name__ == '__main__':
    unittest.main()

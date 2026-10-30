"""Same-work corrective QA cycle: READY_FOR_MANUAL_QA -> qa-fix -> REOPENED -> DIAGNOSE -> FIX -> VERIFY."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from switchyard.cli import main
from switchyard.context_transport import StageResult, StageHandoff, ProducedArtifact
from switchyard.observability import read_events
from switchyard.provider_process import ProviderExecutionError
from switchyard.provider_stages import ProviderRuntimeConfig, render_invocation
from switchyard.qa_lifecycle import (create_qa_candidate, accept_v2_work, LifecycleError, fix_records)
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, qa_fix_v2_work,
                                 accept_v2_candidate, resume_v2_work, recovery_status, qa_history, RecoveryBlocked)
from switchyard.qa_lifecycle import fix_store
from runtime import RunResult
from switchyard.work_contract import WorkWorkspace
from switchyard import recovery

FEEDBACK = 'Rename board still does nothing when triggered from the context menu.'


class Crash(BaseException):
    """Process death: bypasses every `except Exception` cleanup path."""


def crash_once(original, when=lambda *a, **k: True):
    state = {'done': False}
    def wrapper(*args, **kwargs):
        if not state['done'] and when(*args, **kwargs):
            state['done'] = True
            raise Crash()
        return original(*args, **kwargs)
    return wrapper


class Stages:
    """Fake providers: FIX writes a per-call revision; every invocation is recorded."""
    def __init__(self, target):
        self.target, self.calls, self.fixes, self.fail_state, self.stuck, self.verdict = target, [], 0, None, False, 'pass'

    def __call__(self, binding):
        owner = self
        class Stage:
            def run(self, invocation):
                owner.calls.append(invocation)
                state = binding.logical_state
                if owner.fail_state == state:
                    owner.fail_state = None
                    raise ProviderExecutionError('provider exited with status 1', returncode=1)
                if state == 'DIAGNOSE':
                    return StageResult('ready', (ProducedArtifact('diagnosis', 'cause %d' % len(owner.calls)),), StageHandoff('Diagnosed.'))
                if state == 'FIX':
                    owner.fixes += 1
                    if not (owner.stuck and owner.fixes > 1):
                        (owner.target/'source.py').write_text('fixed-%d' % owner.fixes)
                    return StageResult('ready', (ProducedArtifact('implementation_handoff', 'fix %d' % owner.fixes),), StageHandoff('Fixed.'))
                if state == 'VERIFY':
                    return StageResult(owner.verdict, (ProducedArtifact('verifier_evidence', owner.verdict),))
                return StageResult('blocked', handoff=StageHandoff('Cannot.', blockers=('stuck',)))
        return Stage()


class QaFixBase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.target = self.root/'target'; self.target.mkdir()
        self.work, self.qa = self.root/'work', self.root/'qa1'
        (self.target/'source.py').write_text('before')
        (self.target/'.gitignore').write_text('.switchyard/*\n!.switchyard/config.yaml\n__pycache__/\n')
        (self.target/'.switchyard').mkdir()
        step = lambda **kw: dict(rung_budget_scope='visit', budget={'visits': 2}, **kw)
        self.data = {'agents': {
            'diagnoser': dict(provider='claude', model='sonnet', workspace='read', shell=True, timeout=2),
            'code': dict(provider='claude', model='sonnet', workspace='write', shell=True, timeout=2),
            'tests': dict(provider='command', command=[sys.executable, '-c', 'pass'], capture='verifier_evidence', timeout=2)},
            'flows': {'bugfix': {'start': 'DIAGNOSE', 'limits': {'run_invocations': 8}, 'states': {
                'NEEDS_HUMAN': dict(terminal=True),
                # The declared corrective route; plain `resume` can never use it.
                'READY_FOR_MANUAL_QA': dict(terminal=True, resume_to='DIAGNOSE'),
                'DIAGNOSE': step(agent='diagnoser', consumes=['request'], produces={'diagnosis': dict(format='markdown')},
                                 on={'ready': 'FIX', 'blocked': 'NEEDS_HUMAN'}),
                'FIX': step(agent='code', consumes=['diagnosis'], produces={'implementation_handoff': dict(format='markdown')},
                            on={'ready': 'VERIFY', 'blocked': 'NEEDS_HUMAN'}),
                'VERIFY': step(agent='tests', consumes=['implementation_handoff'], produces={'verifier_evidence': dict(format='markdown')},
                               on={'pass': 'READY_FOR_MANUAL_QA', 'fail': 'NEEDS_HUMAN'})}}}}
        self.configfile = self.target/'.switchyard/config.yaml'; self.text = self.save_config()
        for args in [('init', '-b', 'main'), ('config', 'user.name', 'QA Fix'), ('config', 'user.email', 'qa@example.invalid'),
                     ('config', 'commit.gpgsign', 'false'), ('add', '--', '.gitignore', '.switchyard/config.yaml', 'source.py'),
                     ('commit', '-m', 'baseline')]:
            self.git(*args)
        self.config = ProviderRuntimeConfig(str(self.target), sys.executable, sys.executable)
        self.stages = Stages(self.target)
        patcher = patch('switchyard.recovery.ProviderStageExecutorResolver', return_value=self.stages)
        patcher.start(); self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.target, stderr=subprocess.DEVNULL).decode().strip()

    def save_config(self):
        text = json.dumps(self.data); self.configfile.write_text(text); return text

    def deliver(self):
        execution = execute_recoverable_v2_work(self.text, 'bugfix', 'Rename board from the menu.', self.work, self.config)
        if execution.result.terminal_state == 'READY_FOR_MANUAL_QA':
            self.first = create_qa_candidate(self.target, execution.workspace, execution.result, 'bugfix', self.qa)
            attach_qa_candidate(self.work, self.qa)
        return execution

    def qa_fix(self, feedback=FEEDBACK):
        return qa_fix_v2_work(self.work, feedback, self.text, self.config)

    def events(self):
        return tuple(read_events(self.work/'events.jsonl'))

    def states(self, since=0):
        # Compiled ids look like '@v2:bugfix:DIAGNOSE:0'; terminals keep their name.
        return [e.state.split(':')[2] if e.state.startswith('@v2:') else e.state
                for e in self.events()[since:] if e.kind == 'state_entered']


class QaFixTests(QaFixBase):
    # 1
    def test_qa_fix_rejected_outside_ready_for_manual_qa(self):
        with self.assertRaises(Exception):
            self.qa_fix()  # No work at all.
        self.stages.verdict = 'fail'
        self.assertEqual(self.deliver().result.terminal_state, 'NEEDS_HUMAN')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.qa_fix()
        self.assertEqual(ctx.exception.reason, 'work_not_ready_for_manual_qa')
        self.assertEqual(recovery_status(self.work)['terminal'], 'NEEDS_HUMAN')

    def test_qa_fix_rejected_after_accept_and_for_blank_feedback(self):
        self.deliver()
        for blank in ('', '  \n'):
            with self.assertRaises(LifecycleError):
                self.qa_fix(blank)
        self.assertEqual(recovery_status(self.work)['terminal'], 'READY_FOR_MANUAL_QA')  # Nothing finalized.
        accept_v2_candidate(self.work)
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.qa_fix()
        self.assertEqual(ctx.exception.reason, 'candidate_already_finalized')

    def test_plain_resume_without_candidate_keeps_baseline_route(self):
        # Pre-QA-LOOP contract: a declared QA-terminal route is resumable until a candidate is attached.
        execute_recoverable_v2_work(self.text, 'bugfix', 'Rename board from the menu.', self.work, self.config)
        self.assertTrue(recovery_status(self.work)['resume_allowed'])
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertNotIn('qa_candidate', recovery_status(self.work))  # No qa-fix round: nothing auto-published.

    def test_plain_resume_cannot_leave_manual_qa(self):
        self.deliver()
        self.assertFalse(recovery_status(self.work)['resume_allowed'])
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(ctx.exception.reason, 'no_declared_recovery_route')

    def test_undeclared_route_or_budget_refuses_before_rejecting_candidate(self):
        self.data['flows']['bugfix']['states']['READY_FOR_MANUAL_QA'] = dict(terminal=True)
        self.text = self.save_config(); self.deliver()
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.qa_fix()
        self.assertEqual(ctx.exception.reason, 'no_declared_reopen_route')
        self.assertIsNone(recovery_status(self.work)['qa_candidate']['decision'])

    def test_exhausted_run_budget_is_not_reset(self):
        self.data['flows']['bugfix']['limits']['run_invocations'] = 3
        self.text = self.save_config(); self.deliver()
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.qa_fix()
        self.assertEqual(ctx.exception.reason, 'invocation_budget_exhausted')
        status = recovery_status(self.work)
        self.assertEqual((status['qa_candidate']['decision'], status['qa_fix_rounds']), (None, 0))  # Still QA-ready.
        accept_v2_candidate(self.work)

    # 2, 3, 4, 5, 6, 7
    def test_full_corrective_cycle_then_accept_new_candidate_only(self):
        delivered = self.deliver()
        before = self.events(); first_status = recovery_status(self.work)
        self.assertEqual((first_status['qa_candidate']['sequence'], self.states()), (1, ['DIAGNOSE', 'FIX', 'VERIFY', 'READY_FOR_MANUAL_QA']))
        first_files = json.loads(WorkWorkspace.open(self.qa).read_artifact('qa_candidate'))['files']
        calls_before = len(self.stages.calls)

        execution = self.qa_fix()

        # Same work, same run, history only appended; budgets were not reset.
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        events = self.events(); self.assertEqual(events[:len(before)], before)
        self.assertEqual({e.run_id for e in events}, {before[0].run_id})
        self.assertEqual(len([e for e in events if e.kind == 'run_started']), 1)
        self.assertEqual(self.states(len(before)), ['DIAGNOSE', 'FIX', 'VERIFY', 'READY_FOR_MANUAL_QA'])
        self.assertEqual(len([e for e in events if e.kind == 'invocation_started']), 6)
        self.assertEqual(len([e for e in events if e.kind == 'state_entered' and e.state.endswith(':DIAGNOSE:0')]), 2)
        work = WorkWorkspace.open(self.work)
        self.assertEqual(len(work.versions('diagnosis')), 2); self.assertEqual(len(work.versions('request')), 1)
        self.assertEqual(self.work.name, recovery_status(self.work)['work_id'])

        # Feedback persisted durably: work-level record and the retained rejected candidate's workspace.
        requests, resolved = fix_records(WorkWorkspace.open(self.work))
        self.assertEqual((len(requests), len(resolved)), (1, 1))
        self.assertEqual(requests[0]['feedback'], FEEDBACK)
        self.assertEqual(requests[0]['rejected_candidate']['identity'], self.first.identity)
        self.assertEqual(requests[0]['rejected_candidate']['files'], first_files)
        old = WorkWorkspace.open(self.qa)
        self.assertEqual(old.read_artifact('qa_feedback'), FEEDBACK)
        self.assertEqual(json.loads(old.read_artifact('qa_decision'))['terminal'], 'REOPENED')
        self.assertEqual(json.loads(old.read_artifact('qa_candidate'))['files'], first_files)  # Candidate #1 retained.

        # Corrective model stages saw feedback + request + rejected candidate; the verifier did not.
        corrective = self.stages.calls[calls_before:]
        self.assertEqual([c.binding.logical_state for c in corrective], ['DIAGNOSE', 'FIX', 'VERIFY'])
        for call in corrective[:2]:
            rendered = render_invocation(call)
            self.assertIn('MANUAL QA REJECTION', rendered); self.assertIn(FEEDBACK, rendered)
            self.assertIn(self.first.identity, rendered); self.assertIn('Rename board from the menu.', rendered)
        self.assertEqual(corrective[2].qa_feedback, ())
        self.assertEqual(self.stages.calls[0].qa_feedback, ())  # Ordinary delivery context is unchanged.
        self.assertIn('diagnosis', [a.record.name for a in corrective[1].artifacts])

        # Candidate #2 is distinguishable, retained as the latest reference; #1 stays in history.
        status = recovery_status(self.work)
        second = status['qa_candidate']
        self.assertEqual((second['sequence'], status['terminal'], status['qa_fix_open']), (2, 'READY_FOR_MANUAL_QA', False))
        self.assertNotEqual(second['identity'], self.first.identity); self.assertNotEqual(second['workspace'], str(self.qa))
        data = json.loads(WorkWorkspace.open(second['workspace']).read_artifact('qa_candidate'))
        self.assertEqual((data['sequence'], data['supersedes'], data['source_run_id']),
                         (2, self.first.identity, before[0].run_id))
        self.assertEqual(data['files']['source.py']['sha256'] != first_files['source.py']['sha256'], True)
        self.assertEqual(len(WorkWorkspace.open(self.work/'recovery').versions('qa_reference')), 2)

        # The rejected candidate can no longer be accepted; accept applies to candidate #2 only.
        with self.assertRaisesRegex(LifecycleError, 'already finalized'):
            accept_v2_work(self.target, self.qa)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')
        decision = accept_v2_candidate(self.work)
        self.assertEqual((decision.terminal, decision.candidate_identity), ('ACCEPTED', second['identity']))
        self.assertEqual(self.git('show', 'HEAD:source.py'), 'fixed-2')
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(recovery_status(self.work)['terminal'], 'ACCEPTED')

    def test_candidate_identity_advances_even_when_bytes_are_identical(self):
        self.stages.stuck = True
        self.deliver(); self.qa_fix()
        second = recovery_status(self.work)['qa_candidate']
        a = json.loads(WorkWorkspace.open(self.qa).read_artifact('qa_candidate'))
        b = json.loads(WorkWorkspace.open(second['workspace']).read_artifact('qa_candidate'))
        self.assertEqual(a['files'], b['files'])
        self.assertNotEqual(second['identity'], self.first.identity)
        self.assertEqual(accept_v2_candidate(self.work).candidate_identity, second['identity'])

    def test_second_round_obeys_unreset_state_visit_budget(self):
        self.deliver(); self.qa_fix()
        second = Path(recovery_status(self.work)['qa_candidate']['workspace'])
        frozen = {p: p.read_bytes() for p in (second/'manifest.jsonl', self.work/'qa_fix'/'manifest.jsonl',
                                              self.work/'recovery'/'manifest.jsonl', self.work/'events.jsonl')}
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.qa_fix('Still wrong in dark mode.')  # DIAGNOSE already used both of its visits.
        self.assertEqual({p: p.read_bytes() for p in frozen}, frozen)  # No partial round, #2 untouched.
        self.assertEqual(ctx.exception.reason, 'invocation_budget_exhausted')
        status = recovery_status(self.work)
        self.assertEqual((status['qa_candidate']['sequence'], status['qa_candidate']['decision'], status['qa_fix_rounds']),
                         (2, None, 1))  # Candidate #2 was not rejected by the refused round.
        self.assertEqual(accept_v2_candidate(self.work).terminal, 'ACCEPTED')

    def test_second_round_with_budget_yields_candidate_three(self):
        for state in ('DIAGNOSE', 'FIX', 'VERIFY'):
            self.data['flows']['bugfix']['states'][state]['budget']['visits'] = 3
        self.data['flows']['bugfix']['limits']['run_invocations'] = 9
        self.text = self.save_config()
        self.deliver(); self.qa_fix(); self.qa_fix('Still wrong in dark mode.')
        status = recovery_status(self.work)
        self.assertEqual((status['qa_candidate']['sequence'], status['qa_fix_rounds']), (3, 2))
        self.assertEqual(accept_v2_candidate(self.work).terminal, 'ACCEPTED')
        self.assertEqual(self.git('show', 'HEAD:source.py'), 'fixed-3')

    def test_interrupted_corrective_cycle_resumes_with_feedback_and_publishes_candidate(self):
        self.deliver(); self.stages.fail_state = 'FIX'
        with self.assertRaises(Exception):
            self.qa_fix()
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['qa_fix_open']), ('INTERRUPTED', True))
        self.assertEqual(status['qa_candidate']['decision'], 'REOPENED')
        with self.assertRaises(RecoveryBlocked):
            accept_v2_candidate(self.work)  # Rejected candidate is not acceptable.
        resumed = resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(resumed.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertIn(FEEDBACK, render_invocation(self.stages.calls[-2]))  # The retried FIX still has feedback.
        self.assertEqual(recovery_status(self.work)['qa_candidate']['sequence'], 2)
        self.assertEqual(accept_v2_candidate(self.work).terminal, 'ACCEPTED')

    # Crash consistency ---------------------------------------------------------------------
    def assert_single_round_and_accept(self):
        requests, resolved = fix_records(WorkWorkspace.open(self.work))
        self.assertEqual([(r['round'], r['feedback']) for r in requests], [(1, FEEDBACK)])
        self.assertEqual(len(resolved), 1)
        old = WorkWorkspace.open(self.qa)
        self.assertEqual([len(old.versions(n)) for n in ('qa_intent', 'qa_feedback', 'qa_decision')], [1, 1, 1])
        self.assertEqual(old.read_artifact('qa_feedback'), FEEDBACK)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['qa_candidate']['sequence']), ('READY_FOR_MANUAL_QA', 2))
        self.assertEqual(len(WorkWorkspace.open(self.work/'recovery').versions('qa_reference')), 2)
        self.assertEqual(len(self.states()), 8)  # One delivery + exactly one corrective cycle.
        self.assertEqual(accept_v2_candidate(self.work).candidate_identity, status['qa_candidate']['identity'])

    def test_crash_after_intent_before_reject_is_finished_by_retry(self):
        self.deliver()
        candidate_bytes = (self.qa/'manifest.jsonl').read_bytes()
        with patch('switchyard.recovery.finish_reopen_v2_work', crash_once(recovery.finish_reopen_v2_work)):
            with self.assertRaises(Crash):
                self.qa_fix()
        self.assertEqual((self.qa/'manifest.jsonl').read_bytes(), candidate_bytes)  # #1 not touched yet.
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['qa_fix_open'], status['resume_allowed']), ('REOPENED', True, True))
        with self.assertRaises(RecoveryBlocked):
            accept_v2_candidate(self.work)
        with self.assertRaisesRegex(RecoveryBlocked, 'different_feedback'):
            self.qa_fix('Some other complaint.')
        self.qa_fix()  # Same command again: finishes, never a second round.
        self.assert_single_round_and_accept()

    def test_crash_inside_candidate_reopen_is_finished_by_resume(self):
        self.deliver()
        original = WorkWorkspace.write_artifact
        with patch.object(WorkWorkspace, 'write_artifact', crash_once(original, lambda ws, name, *a, **k: name == 'qa_decision')):
            with self.assertRaises(Crash):
                self.qa_fix()
        old = WorkWorkspace.open(self.qa)
        self.assertEqual([len(old.versions(n)) for n in ('qa_intent', 'qa_feedback', 'qa_decision')], [1, 1, 0])
        self.assertEqual(recovery_status(self.work)['terminal'], 'REOPENED')
        resume_v2_work(self.work, self.text, self.config)
        self.assert_single_round_and_accept()

    def test_crash_after_reopened_before_corrective_run(self):
        self.deliver()
        with patch('switchyard.recovery._continue_corrective', crash_once(recovery._continue_corrective)):
            with self.assertRaises(Crash):
                self.qa_fix()
        status = recovery_status(self.work)
        self.assertEqual((status['phase'], status['terminal'], status['resume_allowed']), ('reopened', 'REOPENED', True))
        self.qa_fix()  # Accidental duplicate command continues the same round.
        self.assert_single_round_and_accept()

    def test_publication_failure_is_not_qa_ready_and_resume_publishes(self):
        self.deliver()
        failing = crash_once(recovery.create_qa_candidate)
        def fail(*args, **kwargs):
            try:
                return failing(*args, **kwargs)
            except Crash:
                raise LifecycleError('git_inspection_failed') from None
        with patch('switchyard.recovery.create_qa_candidate', fail):
            with self.assertRaisesRegex(LifecycleError, 'git_inspection_failed'):
                self.qa_fix()
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['reason'], status['qa_fix_open']), ('INTERRUPTED', 'candidate_publication_pending', True))
        self.assertIn('git_inspection_failed', status['diagnostic'])
        self.assertEqual(status['qa_candidate']['decision'], 'REOPENED')  # Never a fake or stale QA-ready candidate.
        self.assertTrue(status['resume_allowed'])
        with self.assertRaises(RecoveryBlocked):
            accept_v2_candidate(self.work)
        with self.assertRaises(RecoveryBlocked):
            self.qa_fix()
        events = self.events(); calls = len(self.stages.calls)
        resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((self.events(), len(self.stages.calls)), (events, calls))  # No stage re-executed.
        self.assert_single_round_and_accept()

    def test_crash_after_candidate_rename_before_reference_reuses_candidate(self):
        self.deliver()
        with patch('switchyard.recovery._put', crash_once(recovery._put, lambda store, name, data: name == 'qa_reference')):
            with self.assertRaises(Crash):
                self.qa_fix()
        built = json.loads(WorkWorkspace.open(self.work/'qa'/'candidate-2').read_artifact('qa_candidate'))
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['reason']), ('INTERRUPTED', 'candidate_publication_pending'))
        resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(json.loads(WorkWorkspace.open(recovery_status(self.work)['qa_candidate']['workspace']).read_artifact('qa_candidate')), built)
        self.assertEqual([p.name for p in (self.work/'qa').iterdir() if not p.name.startswith('.')], ['candidate-2'])
        self.assert_single_round_and_accept()

    def test_crash_after_reference_before_qa_ready_is_idempotent(self):
        self.deliver()
        with patch('switchyard.recovery.resolve_fix_request', crash_once(recovery.resolve_fix_request)):
            with self.assertRaises(Crash):
                self.qa_fix()
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['qa_candidate']['sequence']), ('INTERRUPTED', 2))  # Current, not yet QA-ready.
        with self.assertRaises(RecoveryBlocked):
            accept_v2_candidate(self.work)
        resume_v2_work(self.work, self.text, self.config)
        self.assert_single_round_and_accept()

    def test_corrective_cycle_ending_elsewhere_reports_that_terminal(self):
        self.deliver(); self.stages.verdict = 'fail'
        self.assertEqual(self.qa_fix().result.terminal_state, 'NEEDS_HUMAN')
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['qa_fix_open'], status['qa_candidate']['decision']), ('NEEDS_HUMAN', True, 'REOPENED'))
        for operation in (lambda: accept_v2_candidate(self.work), self.qa_fix):
            with self.assertRaises(RecoveryBlocked):
                operation()

    # 8
    def test_direct_accept_flow_is_unchanged(self):
        self.deliver()
        status = recovery_status(self.work)
        self.assertEqual((status['current_candidate_sequence'], status['current_qa_round'], status['qa_round_open']), (1, None, False))
        self.assertEqual((status['terminal'], status['qa_candidate']['sequence'], status['qa_fix_rounds']), ('READY_FOR_MANUAL_QA', 1, 0))
        self.assertEqual(self.first.identity, status['qa_candidate']['identity'])
        self.assertEqual(self.events()[-1].state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(accept_v2_work(self.target, self.qa).terminal, 'ACCEPTED')
        self.assertEqual(self.git('show', 'HEAD:source.py'), 'fixed-1')
        self.assertNotIn('sequence', json.loads(WorkWorkspace.open(self.qa).read_artifact('qa_candidate')))

    def test_cli_qa_fix_and_accept(self):
        self.deliver()
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
            return code, out.getvalue(), err.getvalue()
        code, out, err = run('work', 'qa-fix', str(self.work), FEEDBACK)
        self.assertEqual(code, 0, err)
        status = json.loads(out)
        self.assertEqual((status['qa_candidate']['sequence'], status['terminal']), (2, 'READY_FOR_MANUAL_QA'))
        self.assertEqual(run('work', 'qa-fix', str(self.work), '   ')[0], 2)
        code, out, err = run('work', 'accept', str(self.work))
        self.assertEqual((code, json.loads(out)['terminal']), (0, 'ACCEPTED'))
        self.assertEqual(run('work', 'accept', str(self.work))[0], 2)


ROUND1, ROUND2 = 'Round one: rename does nothing.', 'Round two: rename loses focus.'


class RepeatedQaRoundTests(QaFixBase):
    """Durable QA rounds: #1 -round 1-> #2 -round 2-> #3, all in one work and one run."""
    def setUp(self):
        super().setUp()
        for state in ('DIAGNOSE', 'FIX', 'VERIFY'):
            self.data['flows']['bugfix']['states'][state]['budget']['visits'] = 3
        self.data['flows']['bugfix']['limits']['run_invocations'] = 9
        self.text = self.save_config()

    def two_rounds(self):
        self.deliver(); self.qa_fix(ROUND1)
        self.round1 = qa_history(self.work)['rounds'][0]
        self.round1_bytes = (self.work/'qa_fix'/'manifest.jsonl').read_bytes()
        self.qa_fix(ROUND2)

    def assert_lineage(self):
        history = qa_history(self.work)
        c1, c2, c3 = history['candidates']
        self.assertEqual([c['sequence'] for c in (c1, c2, c3)], [1, 2, 3])
        self.assertEqual((c1['supersedes'], c2['supersedes'], c3['supersedes']), (None, c1['identity'], c2['identity']))
        self.assertEqual(len({c1['identity'], c2['identity'], c3['identity']}), 3)
        self.assertEqual([c['decision'] for c in (c1, c2)], ['REOPENED', 'REOPENED'])
        r1, r2 = history['rounds']
        self.assertEqual((r1['round'], r1['feedback'], r1['rejected']['identity'], r1['produced']['identity'], r1['open']),
                         (1, ROUND1, c1['identity'], c2['identity'], False))
        self.assertEqual((r2['round'], r2['feedback'], r2['rejected']['identity'], r2['produced']['identity'], r2['open']),
                         (2, ROUND2, c2['identity'], c3['identity'], False))
        self.assertEqual((r1['produced']['workspace'], r2['produced']['workspace']), (c2['workspace'], c3['workspace']))
        self.assertEqual(WorkWorkspace.open(c1['workspace']).read_artifact('qa_feedback'), ROUND1)
        self.assertEqual(WorkWorkspace.open(c2['workspace']).read_artifact('qa_feedback'), ROUND2)
        status = recovery_status(self.work)
        self.assertEqual((status['current_candidate_sequence'], status['current_qa_round'], status['qa_round_open'], status['terminal']),
                         (3, 2, False, 'READY_FOR_MANUAL_QA'))
        self.assertEqual(len([e for e in self.events() if e.kind == 'run_started']), 1)
        return c1, c2, c3

    def test_two_rounds_lineage_context_and_accept_latest_only(self):
        self.deliver(); self.qa_fix(ROUND1)
        round1 = qa_history(self.work)['rounds'][0]; store_bytes = (self.work/'qa_fix'/'manifest.jsonl').read_bytes()
        c2 = recovery_status(self.work)['qa_candidate']; c2_bytes = (Path(c2['workspace'])/'manifest.jsonl').read_bytes()
        calls = len(self.stages.calls)
        self.qa_fix(ROUND2)
        # Round 1 is immutable history: its records are a byte prefix of the store, unchanged after round 2.
        self.assertTrue((self.work/'qa_fix'/'manifest.jsonl').read_bytes().startswith(store_bytes))
        self.assertEqual(dict(qa_history(self.work)['rounds'][0], produced=round1['produced']), round1)
        self.assertTrue((Path(c2['workspace'])/'manifest.jsonl').read_bytes().startswith(c2_bytes))  # #2 only gained its decision.
        # Round-2 corrective model stages act on the latest rejection only.
        for call in self.stages.calls[calls:calls+2]:
            rendered = render_invocation(call)
            self.assertIn(ROUND2, rendered); self.assertNotIn(ROUND1, rendered); self.assertIn(c2['identity'], rendered)
        c1, c2, c3 = self.assert_lineage()
        for old in (c1, c2):
            with self.assertRaisesRegex(LifecycleError, 'already finalized'):
                accept_v2_work(self.target, old['workspace'])
        decision = accept_v2_candidate(self.work)
        self.assertEqual((decision.terminal, decision.candidate_identity), ('ACCEPTED', c3['identity']))
        self.assertEqual(self.git('show', 'HEAD:source.py'), 'fixed-3')
        self.assertEqual(qa_history(self.work)['candidates'][2]['decision'], 'ACCEPTED')

    def test_crash_during_round_two_reject_is_not_duplicated(self):
        self.deliver(); self.qa_fix(ROUND1)
        with patch('switchyard.recovery.finish_reopen_v2_work', crash_once(recovery.finish_reopen_v2_work)):
            with self.assertRaises(Crash):
                self.qa_fix(ROUND2)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['current_qa_round'], status['qa_round_open'], status['current_candidate_sequence']),
                         ('REOPENED', 2, True, 2))
        self.qa_fix(ROUND2)  # Retry finishes round 2.
        self.assert_lineage()
        self.assertEqual(len(fix_store(WorkWorkspace.open(self.work)).versions('qa_fix_request')), 2)

    def test_crash_during_candidate_three_publication_does_not_create_four(self):
        self.deliver(); self.qa_fix(ROUND1)
        with patch('switchyard.recovery.resolve_fix_request', crash_once(recovery.resolve_fix_request)):
            with self.assertRaises(Crash):
                self.qa_fix(ROUND2)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['current_candidate_sequence'], status['qa_round_open']), ('INTERRUPTED', 3, True))
        resume_v2_work(self.work, self.text, self.config)
        self.assert_lineage()
        self.assertEqual(sorted(p.name for p in (self.work/'qa').iterdir() if not p.name.startswith('.')), ['candidate-2', 'candidate-3'])
        self.assertEqual(accept_v2_candidate(self.work).terminal, 'ACCEPTED')

    def test_previous_single_round_format_stays_readable_and_resumable(self):
        # Records written by the first committed slice: schema-1 resolution without explicit links.
        def legacy(workspace, identity, sequence, _workspace):
            requests, _ = fix_records(workspace)
            fix_store(workspace).write_artifact('qa_fix_resolved', json.dumps(
                dict(round=len(requests), superseding_identity=identity, sequence=sequence), sort_keys=True, separators=(',', ':'))+'\n')
        self.deliver()
        with patch('switchyard.recovery.resolve_fix_request', legacy):
            self.qa_fix(ROUND1)
        status = recovery_status(self.work)
        self.assertEqual((status['current_candidate_sequence'], status['current_qa_round'], status['qa_round_open']), (2, 1, False))
        (round1,) = qa_history(self.work)['rounds']
        self.assertEqual(round1['produced']['workspace'], status['qa_candidate']['workspace'])  # Recovered from qa_reference.
        self.qa_fix(ROUND2)
        self.assert_lineage()
        self.assertEqual(accept_v2_candidate(self.work).terminal, 'ACCEPTED')

    def test_round_two_refuses_a_candidate_outside_the_lineage(self):
        self.deliver(); self.qa_fix(ROUND1)
        # An embedder attaching an unrelated (sequence-1) candidate must not silently fork the lineage.
        history = tuple(read_events(self.work/'events.jsonl'))
        foreign = create_qa_candidate(self.target, WorkWorkspace.open(self.work), RunResult(history[-1].state, history),
                                      'bugfix', self.root/'foreign')
        attach_qa_candidate(self.work, self.root/'foreign')
        frozen = {p: p.read_bytes() for p in (self.root/'foreign'/'manifest.jsonl', self.work/'qa_fix'/'manifest.jsonl')}
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.qa_fix(ROUND2)
        self.assertEqual(ctx.exception.reason, 'qa_lineage_mismatch')
        self.assertEqual({p: p.read_bytes() for p in frozen}, frozen)
        self.assertEqual(foreign.identity, recovery_status(self.work)['qa_candidate']['identity'])

if __name__ == '__main__':
    unittest.main()

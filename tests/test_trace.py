"""OBSERVABILITY (#1) slice 1: durable, read-only runtime trace. Real deliveries with the offline provider
fixtures; the trace is checked against the authoritative canonical history (`events.jsonl`)."""
import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard import trace
from switchyard.cli import main
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (attach_qa_candidate, qa_fix_v2_work, recovery_status, resume_v2_work, _operations, _store)
from switchyard.suspend import suspend_v2_work
from switchyard.trace import Tracer, trace_events

import test_human_input
from test_continuity import ContinuityBase

SECRET = 'ghp_TRACEsecretTOKEN123456'


class TraceBase(ContinuityBase):
    def canonical(self, work=None):
        return tuple(read_events((work or self.work)/'events.jsonl'))

    def events(self, work=None, **filters):
        return trace_events(work or self.work, **filters)

    def types(self, work=None, **filters):
        return [(e['event_type'], e['state']) for e in self.events(work, **filters)]

    def files(self, base):
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob('*')) if p.is_file() and not p.is_symlink()}

    def trace_bytes(self, work=None):
        d = (work or self.work)/'observability'
        return self.files(d) if d.exists() else {}

    def assert_consistent(self, work=None):
        """Dense monotonic sequences, unique ids, each canonical event projected at most once, ids = canonical."""
        events = self.events(work)
        self.assertEqual([e['sequence'] for e in events], list(range(1, len(events)+1)))
        self.assertEqual(len({e['event_id'] for e in events}), len(events))
        canonical = self.canonical(work)
        seen = {}
        for e in events:
            source = canonical[e['source']['index']]
            self.assertEqual((source.kind, source.run_id, source.state_visit_id), (e['source']['kind'], e['run_id'], e['state_visit_id']))
            seen.setdefault(e['source']['index'], []).append(e['event_type'])
        self.assertTrue(all(v in ([t] for t in trace.EVENT_TYPES) or v == ['routing_decided', 'invocation_started'] for v in seen.values()), seen)
        finished = [e['invocation_id'] for e in events if e['event_type'] == 'invocation_finished']
        self.assertEqual(len(finished), len(set(finished)))
        return events

    def candidate(self, execution, name='qa1'):
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/name)
        attach_qa_candidate(self.work, self.root/name)


class NormalDeliveryTests(TraceBase):
    # 1, 2, 4, 7, 8, 9, 14, 16, 17, 18, 23, 30
    def test_a_delivery_is_traced_run_visits_routing_invocations_budgets_transitions(self):
        execution = self.start()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        events = self.assert_consistent(); canonical = self.canonical()
        run = canonical[0].run_id
        self.assertEqual({e['run_id'] for e in events}, {run})
        self.assertEqual({e['observed_by'] for e in events}, {'runtime'})
        self.assertEqual([t for t, _ in self.types()][:7], ['run_started', 'state_entered', 'routing_decided', 'invocation_started',
                                                            'invocation_finished', 'transition_decided', 'state_exited'])
        visits = [(e['state'], e['state_visit_id']) for e in events if e['event_type'] == 'state_entered']
        self.assertEqual(visits, [(c_state, c.state_visit_id) for c, c_state in
                                  zip([c for c in canonical if c.kind == 'state_entered'], ['SPECIFY', 'IMPLEMENT', 'VERIFY', 'READY_FOR_MANUAL_QA'])])
        invocations = [c.invocation_id for c in canonical if c.kind == 'invocation_started']
        self.assertEqual([e['invocation_id'] for e in events if e['event_type'] == 'invocation_started'], invocations)
        models = [e for e in events if e['event_type'] == 'invocation_started' and e['provider'] != 'command']
        self.assertEqual(len(models), len(self.calls()))  # 30: one provider process per model invocation, none added.
        routing = self.events(event_type='routing_decided', state='IMPLEMENT')[0]
        self.assertEqual({k: routing['payload'][k] for k in ('requested_state', 'rung', 'agent', 'provider', 'model', 'workspace', 'shell',
                                                              'timeout_seconds', 'fallback_used', 'policy_identity')},
                         dict(requested_state='IMPLEMENT', rung=0, agent='code', provider='claude', model='sonnet', workspace='write',
                              shell=True, timeout_seconds=5, fallback_used=False, policy_identity='@v2:feature:IMPLEMENT:0'))
        started, finished = self.events(invocation_id=invocations[1], event_type='invocation_started')[0], \
            self.events(invocation_id=invocations[1], event_type='invocation_finished')[0]
        self.assertEqual((started['state_visit_id'], started['agent'], started['provider'], started['model']),
                         (finished['state_visit_id'], 'code', 'claude', 'sonnet'))
        p = finished['payload']
        self.assertEqual((p['outcome'], p['outcome_category'], p['termination'], p['invocation_in_visit'], p['semantic_attempt'], p['turn'],
                          p['started_at'], p['timeout_seconds']), ('ready', 'semantic_outcome', 'completed', 1, 1, None, started['timestamp'], 5))
        self.assertIsInstance(p['duration_ms'], int); self.assertGreaterEqual(p['duration_ms'], 0)
        budget = started['payload']['budget']
        self.assertEqual((budget['run_invocations'], budget['semantic_charged'], budget['state_visits'], budget['infrastructure_failures']),
                         (dict(used_before=1, used_after=2, limit=12), True, dict(used=1, limit=2), dict(agent='code', used=0, limit=2)))
        self.assertEqual([(s['used_before'], s['used_after'], s['limit']) for s in budget['invocation_scopes']], [(0, 1, 3), (0, 1, 3)])
        transition = self.events(event_type='transition_decided', state='IMPLEMENT')[0]['payload']
        self.assertEqual((transition['from_state'], transition['from_visit'], transition['to_state'], transition['outcome']),
                         ('IMPLEMENT', started['state_visit_id'], 'VERIFY', 'ready'))
        # 16: references, never contents.
        refs = self.events(event_type='invocation_finished', state='SPECIFY')[0]['payload']['artifact_refs']
        microspec = [r for r in refs if r.get('name') == 'microspec'][0]
        record = (self.work/microspec['path']).read_bytes()
        self.assertEqual(microspec['sha256'], hashlib.sha256(record).hexdigest())
        self.assertTrue(all(record not in data for data in self.trace_bytes().values()))
        self.assertIn('handoff', [r['kind'] for r in refs])
        # 17, 18: frozen runtime/config and Git delivery provenance, from the baseline (no Git command, no remote).
        provenance = self.events(event_type='run_started')[0]['payload']['provenance']
        self.assertEqual((provenance['runtime']['config_sha256'], provenance['runtime']['work_schema_version'], provenance['runtime']['flow']),
                         (hashlib.sha256(self.text.encode()).hexdigest(), 3, 'feature'))
        self.assertTrue(provenance['runtime']['switchyard_version'])
        self.assertEqual((provenance['git']['delivery_branch'], provenance['git']['delivery_base_sha'], provenance['git']['worktree']),
                         ('refs/heads/switchyard/work', self.base, str(self.delivery)))
        self.assertNotIn('subprocess', vars(trace))  # The trace never runs Git.
        self.assertEqual(recovery_status(self.work)['observability'],
                         dict(trace_events=len(events), last_sequence=len(events), last_event='run_finished'))

    # 11, 14
    def test_a_semantic_failure_keeps_its_outcome_and_transition(self):
        self.data['agents']['tests']['command'] = [sys.executable, '-c', 'raise SystemExit(1)']
        self.text = json.dumps(self.data); (self.target/'.switchyard/config.yaml').write_text(self.text)
        self.git('commit', '-qam', 'failing verifier'); self.base = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.start().result.terminal_state, 'NEEDS_HUMAN')
        finished = self.events(event_type='invocation_finished', state='VERIFY')[0]['payload']
        self.assertEqual((finished['outcome'], finished['outcome_category']), ('fail', 'semantic_outcome'))
        self.assertEqual(self.events(event_type='transition_decided', state='VERIFY')[0]['payload']['to_state'], 'NEEDS_HUMAN')
        self.assertEqual(self.events()[-1]['payload']['terminal'], 'NEEDS_HUMAN')

    # 6
    def test_structured_output_repair_stays_inside_one_invocation(self):
        config = replace(self.config, environment={**self.config.environment, 'FAKE_SCHEMA_INVALID_ATTEMPTS': '1'})
        from switchyard.recovery import execute_recoverable_v2_work
        execute_recoverable_v2_work(self.text, 'feature', 'Change source.py', self.work, config)
        implement = self.events(state='IMPLEMENT', event_type='invocation_started')
        self.assertEqual((len(implement), sum(c['provider'] == 'claude' for c in self.calls())), (1, 1))
        finished = self.events(invocation_id=implement[0]['invocation_id'], event_type='invocation_finished')
        self.assertEqual((len(finished), finished[0]['payload']['turn'], finished[0]['payload']['invocation_in_visit']), (1, None, 1))


class FailureTests(TraceBase):
    # 5, 12, 13, 15, 22
    def test_a_provider_failure_and_its_retry_are_distinct_invocations_of_one_visit(self):
        self.interrupted()
        stop = self.events()[-1]
        self.assertEqual(stop['event_type'], 'run_interrupted')
        p = stop['payload']
        self.assertEqual((stop['state'], stop['agent'], p['reason'], p['outcome_category'], p['semantic_invocation'], p['exit_code'], p['invocation_in_visit']),
                         ('IMPLEMENT', 'code', 'provider_failure', 'provider_exit', False, 7, 1))
        before = self.trace_bytes()
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')
        events = self.assert_consistent()
        self.assertTrue(all(self.trace_bytes()[k] == v for k, v in before.items()))  # Older events never rewritten.
        resumed = [e for e in events if e['event_type'] == 'run_resumed'][0]
        self.assertEqual((resumed['payload']['resume_kind'], resumed['state_visit_id'], resumed['run_id']),
                         ('interrupted_visit', stop['state_visit_id'], stop['run_id']))
        implement = self.events(state='IMPLEMENT', event_type='invocation_started')
        self.assertEqual(len(implement), 2)
        self.assertNotEqual(implement[0]['invocation_id'], implement[1]['invocation_id'])
        self.assertEqual({e['state_visit_id'] for e in implement}, {stop['state_visit_id']})
        self.assertEqual([e['payload']['invocation_in_visit'] for e in implement], [1, 2])
        self.assertEqual(implement[1]['payload']['semantic_attempt'], 1)  # The infrastructure retry is not a new attempt.
        first, retry = implement[0]['payload']['budget'], implement[1]['payload']['budget']
        self.assertEqual((first['run_invocations']['used_before'], retry['run_invocations']['used_before']), (1, 1))  # Voided: not charged.
        self.assertEqual((first['infrastructure_failures']['used'], retry['infrastructure_failures']['used']), (0, 1))
        self.assertEqual(len(self.events(event_type='invocation_finished', state='IMPLEMENT')), 1)
        self.assertEqual({e['observed_by'] for e in events}, {'runtime'})

    # 10
    def test_a_timeout_is_recorded_as_a_timeout(self):
        self.data['agents']['spec']['timeout'] = 1
        self.text = json.dumps(self.data); (self.target/'.switchyard/config.yaml').write_text(self.text)
        self.git('commit', '-qam', 'short timeout'); self.base = self.git('rev-parse', 'HEAD')
        config = replace(self.config, environment={**self.config.environment, 'FAKE_MODE': 'timeout'})
        from switchyard.recovery import execute_recoverable_v2_work
        with self.assertRaises(ExecutionError):
            execute_recoverable_v2_work(self.text, 'feature', 'Change source.py', self.work, config)
        stop = self.events()[-1]['payload']
        self.assertEqual((stop['reason'], stop['outcome_category'], stop['termination']), ('provider_timeout', 'timeout', 'timeout'))
        self.assertEqual(self.events(event_type='routing_decided')[0]['payload']['timeout_seconds'], 1)


class IdentityTests(TraceBase):
    def tracer(self, void=frozenset()):
        from switchyard.recovery import _compiled, _Projection
        from switchyard.simple_config import simple_config_from_text
        from switchyard.workflow_compiler import compile_workflow
        project = simple_config_from_text(self.text)
        compiled = compile_workflow(project.bundle, 'feature', lambda b: _Projection())
        return trace.tracer(self.work, _store(self.work), compiled, project, void)

    # 20, 22, 24
    def test_replaying_the_history_is_idempotent_and_identity_ignores_timestamps(self):
        self.start(); before = self.trace_bytes(); history = self.canonical()
        self.tracer().backfill(history, _operations(_store(self.work)))
        self.tracer().backfill(history, _operations(_store(self.work)))  # A second restart: still nothing new.
        self.assertEqual(self.trace_bytes(), before)
        shifted = tuple(replace(e, occurred_at_unix_ns=(e.occurred_at_unix_ns or 0)+10**12) for e in history)
        ids = []
        for events in (history, shifted):
            t = self.tracer(); records = []
            t._write = records.append
            t.backfill(events, [])
            ids.append([(r['sequence'], r['event_id']) for r in records])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[0], [(e['sequence'], e['event_id']) for e in self.events()])
        t = self.tracer(); t.interrupted({}); t.interrupted({})  # Nothing pending: no-ops.
        self.assertEqual(self.trace_bytes(), before)

    # 21, 31
    def test_a_trace_failure_never_changes_the_delivery_and_is_backfilled_from_durable_history(self):
        with patch('switchyard.trace._publish', side_effect=OSError('disk full')):
            execution = self.start()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual((self.events(), recovery_status(self.work)['terminal']), ([], 'READY_FOR_MANUAL_QA'))
        self.assertNotIn('observability', recovery_status(self.work))
        first_run = len(self.canonical())
        self.candidate(execution)
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.config)  # Next drive: backfill, then live.
        events = self.assert_consistent()
        backfilled = [e for e in events if e['source']['index'] < first_run]
        self.assertEqual({e['observed_by'] for e in backfilled}, {'recovery_backfill'})
        self.assertEqual({e['observed_by'] for e in events if e['source']['index'] >= first_run}, {'runtime'})
        self.assertEqual(len([e for e in backfilled if e['event_type'] == 'invocation_finished']), 3)
        self.assertTrue(all(e['payload']['artifact_refs'] is None for e in backfilled if e['event_type'] == 'invocation_finished'))  # Unknown stays unknown.
        self.assertIsNotNone(backfilled[0]['payload']['provenance'])

    def test_a_tracer_write_failure_mid_run_leaves_execution_intact(self):
        calls = {'n': 0}
        original = trace._publish
        def flaky(path, value):
            calls['n'] += 1
            if calls['n'] == 6:
                raise RuntimeError('trace bug')
            return original(path, value)
        with patch('switchyard.trace._publish', flaky):
            self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(len(self.calls()), 2)  # Same model processes as an untraced delivery.
        self.assertLess(len(self.events()), 22)  # Tracing stopped for this drive; the delivery did not.


class ContinuityTests(TraceBase):
    # 3, 34
    def test_qa_reopen_continues_the_same_run_with_new_visits(self):
        execution = self.start(); self.candidate(execution)
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.config)
        events = self.assert_consistent()
        self.assertEqual(len({e['run_id'] for e in events}), 1)
        reopen = [e for e in events if e['event_type'] == 'run_resumed'][0]
        self.assertEqual((reopen['payload']['resume_kind'], reopen['payload']['target'], reopen['payload']['context']['from_terminal'],
                          reopen['payload']['context']['qa_fix_round']), ('recover_to', 'IMPLEMENT', 'READY_FOR_MANUAL_QA', 1))
        self.assertTrue(reopen['payload']['context']['candidate']['identity'])
        visits = [e for e in events if e['event_type'] == 'state_entered' and e['state'] == 'IMPLEMENT']
        self.assertEqual(len(visits), 2); self.assertNotEqual(visits[0]['state_visit_id'], visits[1]['state_visit_id'])
        self.assertEqual([v['payload']['visit_number'] for v in visits], [1, 2])

    # 32
    def test_suspend_and_resume_keep_one_continuous_trace(self):
        self.interrupted(); before = self.trace_bytes()
        suspend_v2_work(self.work, 'Pause')
        self.assertEqual(self.trace_bytes(), before)  # Suspend is control-plane only: no trace rewrite.
        resume_v2_work(self.work, self.text, self.config)
        events = self.assert_consistent()
        self.assertEqual(len({e['run_id'] for e in events}), 1)
        self.assertEqual([e['event_type'] for e in events].count('run_interrupted'), 1)
        self.assertEqual(events[-1]['event_type'], 'run_finished')

    # 35
    def test_adopt_traces_the_new_work_and_never_touches_the_source_trace(self):
        self.w1_ready(); before = self.trace_bytes()
        self.adopt()
        self.assertEqual(self.trace_bytes(), before)
        w2 = self.events(self.w2)
        self.assertTrue(w2)
        self.assertEqual(w2[0]['work_id'], 'w2')
        self.assertNotEqual(w2[0]['run_id'], self.events()[0]['run_id'])
        self.assert_consistent(self.w2)


class HumanInputTraceTests(unittest.TestCase):
    _base = test_human_input.HumanInputTests
    setUp, setUp_base, git, save_config, start, answer = (_base.setUp, _base.setUp_base, _base.git, _base.save_config,
                                                          _base.start, _base.answer)

    # 33
    def test_a_human_wait_and_answer_stay_in_one_trace(self):
        self.start()
        waiting = trace_events(self.work)
        self.assertEqual(waiting[-1]['event_type'], 'human_input_requested')
        request = waiting[-1]['payload']['request_id']
        self.assertEqual(request, recovery_status(self.work)['human_input_request_id'])
        self.answer()
        events = trace_events(self.work)
        self.assertEqual(events[:len(waiting)], waiting)
        self.assertEqual([e['sequence'] for e in events], list(range(1, len(events)+1)))
        kinds = [e['event_type'] for e in events[len(waiting):]]
        self.assertEqual(kinds[:2], ['human_input_received', 'run_resumed'])
        self.assertEqual(events[len(waiting)+1]['payload']['resume_kind'], 'human_input')
        self.assertEqual(len({e['run_id'] for e in events}), 1)


class ReadTests(TraceBase):
    # 19
    def test_no_secrets_raw_environment_prompts_or_contents_in_the_trace(self):
        config = replace(self.config, environment={**self.config.environment, 'GH_TOKEN': SECRET, 'SECRET_ENV': SECRET})
        from switchyard.recovery import execute_recoverable_v2_work
        execute_recoverable_v2_work(self.text, 'feature', 'Change source.py '+SECRET, self.work, config)
        data = b''.join(self.trace_bytes().values())
        self.assertNotIn(SECRET.encode(), data)
        self.assertNotIn(b'STAGE OUTPUT CONTRACT', data); self.assertNotIn(b'EXECUTION BINDING', data)
        self.assertNotIn(b'Change source.py', data)
        self.assertLess(max(len(v) for v in self.trace_bytes().values()), 8192)
        self.assertEqual(trace._text('fatal: https://u:pw@h/x token=abc '+SECRET), 'fatal: https://h/x [REDACTED] [REDACTED]')
        self.assertEqual(len(trace._text('x'*5000)), trace.MAX_TEXT)

    # 25, 26, 27, 28, 29
    def test_read_api_and_cli_are_read_only_filtered_and_stable(self):
        self.start(); before = self.files(self.work)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['work', 'trace', str(self.work)]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0].split()[:6], ['SEQ', 'RUN', 'STATE', 'EVENT', 'AGENT', 'PROVIDER/MODEL'])
        self.assertTrue(any('invocation_finished' in l and 'code' in l and 'claude/sonnet' in l and 'ready' in l for l in lines))
        self.assertTrue(any('transition_decided' in l and '-> VERIFY' in l for l in lines))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['work', 'trace', str(self.work), '--json'])
        self.assertEqual(json.loads(out.getvalue()), self.events())
        invocation = self.events(event_type='invocation_started')[1]['invocation_id']
        self.assertEqual({e['event_type'] for e in self.events(invocation_id=invocation)},
                         {'routing_decided', 'invocation_started', 'invocation_finished', 'transition_decided'})
        self.assertEqual({e['state'] for e in self.events(state='VERIFY')}, {'VERIFY'})
        self.assertEqual(self.events(run_id='run:none'), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['work', 'trace', str(self.work), '--state', 'SPECIFY', '--type', 'invocation_finished', '--json'])
        self.assertEqual([e['state'] for e in json.loads(out.getvalue())], ['SPECIFY'])
        self.assertEqual(self.files(self.work), before)  # Reading never writes.
        # Legacy: a work from before observability.
        import shutil
        shutil.rmtree(self.work/'observability')
        self.assertEqual(self.events(), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['work', 'trace', str(self.work)]), 0)
        self.assertEqual(out.getvalue().strip(), 'no trace events available')
        self.assertNotIn('observability', recovery_status(self.work))


if __name__ == '__main__':
    unittest.main()

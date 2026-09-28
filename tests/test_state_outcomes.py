"""State-local semantic results; no v2 compiler or real providers."""
from dataclasses import asdict, fields, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from runtime import (Event, ExecutionError, ExecutorResult, Flow, FlowValidationError,
                     InvocationResponse, RESERVED_CONTROL_OUTCOMES, State, WorkUnit,
                     run, validate)
from runtime.recording import JsonlRecorder
from switchyard.observability import ObservabilityError, read_events, summarize_runs


class Fake:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return InvocationResponse(ExecutorResult(self.outcome))


def simple(outcome='ready'):
    return Flow('A', {'A': State('fake', {outcome: 'DONE'}), 'DONE': State(terminal=True)})


class StateOutcomeTests(unittest.TestCase):
    def test_declared_semantic_and_legacy_values(self):
        for value in ('ready', 'reproduced', 'not_reproduced', 'inconclusive', 'diagnosis_disproved',
                      'needs_human', 'pass', 'fail', 'done', 'blocked',
                      'success', 'failed', 'error', 'timeout', 'cancelled'):
            with self.subTest(value=value):
                fake = Fake(value)
                result = run(WorkUnit('task'), simple(value), {'fake': fake})
                self.assertEqual(result.terminal_state, 'DONE')
                self.assertEqual(fake.calls, 1)
                self.assertEqual([e.outcome for e in result.events if e.kind in ('invocation_finished', 'transition_selected')], [value, value])

    def test_alternative_declared_outcomes(self):
        flow = Flow('A', {'A': State('x', {'reproduced': 'YES', 'not_reproduced': 'NO'}),
                          'YES': State(terminal=True), 'NO': State(terminal=True)})
        for value, target in [('reproduced', 'YES'), ('not_reproduced', 'NO')]:
            self.assertEqual(run(WorkUnit('task'), flow, {'x': Fake(value)}).terminal_state, target)

    def test_outcomes_are_local_not_global(self):
        flow = Flow('A', {'A': State('a', {'ready': 'B'}),
                          'B': State('b', {'pass': 'DONE'}), 'DONE': State(terminal=True)})
        with self.assertRaisesRegex(ExecutionError, "state 'B' returned undeclared outcome 'ready'") as caught:
            run(WorkUnit('task'), flow, {'a': Fake('ready'), 'b': Fake('ready')})
        self.assertIn("declared outcomes: ['pass']", str(caught.exception))
        self.assertEqual([e.state for e in caught.exception.events if e.kind == 'invocation_finished'], ['A'])
        self.assertEqual(caught.exception.events[-1].kind, 'run_error')
        self.assertIsNone(caught.exception.events[-1].outcome)
        self.assertFalse(summarize_runs(caught.exception.events)[0].completed_normally)

    def test_undeclared_rejected_immediately(self):
        fake = Fake('banana')
        with self.assertRaisesRegex(ExecutionError, 'undeclared outcome.*banana') as caught:
            run(WorkUnit('task'), simple(), {'fake': fake})
        self.assertEqual(fake.calls, 1)
        self.assertEqual([e.kind for e in caught.exception.events],
                         ['run_started', 'state_entered', 'invocation_started', 'run_error'])

    def test_reserved_control_fact_cannot_be_spoofed(self):
        self.assertEqual(RESERVED_CONTROL_OUTCOMES, frozenset({'budget_exceeded'}))
        flow = Flow('A', {'A': State('x', {'ready': 'DONE', 'budget_exceeded': 'STOP'}, max_state_visits_per_run=1),
                          'DONE': State(terminal=True), 'STOP': State(terminal=True)})
        with self.assertRaisesRegex(ExecutionError, 'reserved control-plane outcome') as caught:
            run(WorkUnit('task'), flow, {'x': Fake('budget_exceeded')})
        self.assertFalse(any(e.kind in ('budget_exceeded', 'invocation_finished', 'transition_selected') for e in caught.exception.events))

    def test_lexical_contract_no_coercion_or_normalization(self):
        for invalid in ('', '\0', 'bad\0value', None, True, 1, ('x',)):
            with self.subTest(value=invalid), self.assertRaises(FlowValidationError):
                validate(simple(invalid), {'fake': Fake(invalid)})
            with self.subTest(result=invalid), self.assertRaisesRegex(ExecutionError, 'malformed'):
                run(WorkUnit('task'), simple(), {'fake': Fake(invalid)})
        for exact in (' ready ', 'Prêt ✓', 'PASS', 'two words', 'run_error'):
            # Only budget_exceeded is reserved; no normalization or global registry.
            self.assertEqual(run(WorkUnit('task'), simple(exact), {'fake': Fake(exact)}).terminal_state, 'DONE')
        with self.assertRaises(ExecutionError): run(WorkUnit('task'), simple('PASS'), {'fake': Fake('pass')})

    def test_semantic_cycles_still_require_bounds(self):
        for states in ({'A': State('x', {'again': 'A'})},
                       {'A': State('x', {'ready': 'B'}), 'B': State('x', {'retry': 'A'})}):
            states['DONE'] = State(terminal=True)
            with self.assertRaisesRegex(FlowValidationError, 'cycle without a bounded state'):
                validate(Flow('A', states), {'x': Fake('ready')})
        flow = Flow('A', {'A': State('x', {'again': 'A', 'budget_exceeded': 'DONE'}, max_state_visits_per_run=2),
                          'DONE': State(terminal=True)})
        fake = Fake('again'); result = run(WorkUnit('task'), flow, {'x': fake})
        self.assertEqual(fake.calls, 2)
        self.assertEqual(result.terminal_state, 'DONE')
        self.assertEqual(sum(e.kind == 'budget_exceeded' for e in result.events), 1)

    def test_nonterminal_budget_routing_still_rejected(self):
        flow = Flow('A', {'A': State('x', {'budget_exceeded': 'B'}, max_state_visits_per_run=1),
                          'B': State('x', {'ready': 'DONE'}), 'DONE': State(terminal=True)})
        with self.assertRaisesRegex(FlowValidationError, 'budget target must be terminal'):
            validate(flow, {'x': Fake('ready')})

    def test_v2_semantic_chain_recording_and_projection(self):
        flow = Flow('TRIAGE', {
            'TRIAGE': State('a', {'ready': 'REPRODUCE'}),
            'REPRODUCE': State('b', {'reproduced': 'DIAGNOSE', 'not_reproduced': 'STOP'}),
            'DIAGNOSE': State('c', {'inconclusive': 'STOP'}), 'STOP': State(terminal=True)})
        with TemporaryDirectory() as folder:
            path = Path(folder)/'events.jsonl'
            result = run(WorkUnit('task'), flow, {'a': Fake('ready'), 'b': Fake('reproduced'), 'c': Fake('inconclusive')},
                         recorder=JsonlRecorder(path))
            events = tuple(read_events(path))
            self.assertEqual(events, result.events)
            self.assertEqual([e.outcome for e in events if e.kind == 'invocation_finished'], ['ready', 'reproduced', 'inconclusive'])
            self.assertEqual(dict(summarize_runs(events)[0].outcome_counts), {'ready': 1, 'reproduced': 1, 'inconclusive': 1})
            self.assertEqual(set(json.loads(path.read_text().splitlines()[0])), {f.name for f in fields(Event)})
            bad = replace(next(e for e in events if e.kind == 'invocation_finished'), outcome='budget_exceeded')
            path.write_text(json.dumps(asdict(bad))+'\n')
            with self.assertRaises(ObservabilityError): tuple(read_events(path))

    def test_historical_missing_transition_records_remain_readable(self):
        result = run(WorkUnit('task'), simple('success'), {'fake': Fake('success')})
        prefix = list(result.events[:4])  # through invocation_finished
        prefix.append(replace(prefix[-1], kind='run_error'))
        with TemporaryDirectory() as folder:
            path = Path(folder)/'old.jsonl'
            path.write_text(''.join(json.dumps({k:v for k,v in asdict(e).items()
                if k not in ('occurred_at_unix_ns','duration_ns','correlation_id','parent_run_id')})+'\n' for e in prefix))
            summary, = summarize_runs(read_events(path))
            self.assertFalse(summary.completed_normally)
            self.assertEqual(dict(summary.outcome_counts), {'success': 1})

    def test_executor_exceptions_are_not_semantic_errors(self):
        class Broken:
            def run(self, request): raise ValueError('fault')
        with self.assertRaisesRegex(ExecutionError, 'executor raised') as caught:
            run(WorkUnit('task'), simple('error'), {'fake': Broken()})
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertFalse(any(e.kind == 'transition_selected' for e in caught.exception.events))


if __name__ == '__main__':
    unittest.main()

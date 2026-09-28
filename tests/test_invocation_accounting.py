"""Generic invocation accounting; no compiler or provider dependencies."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from runtime import (ExecutionError, ExecutorResult, Flow, FlowValidationError,
                     InvocationBudgetScope as Scope, InvocationResponse, State,
                     WorkUnit, run, validate)
from runtime.recording import JsonlRecorder
from switchyard.observability import ObservabilityError, read_events, summarize_runs, summarize_budgets


class Fake:
    def __init__(self, outcome='ready', error=None):
        self.outcome, self.error = outcome, error
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return InvocationResponse(ExecutorResult(self.outcome))


def state(target='DONE', scopes=(), fallback='DONE', visits=None):
    on = {'ready': target}
    if fallback is not None:
        on['budget_exceeded'] = fallback
    return State('x', on, max_state_visits_per_run=visits, invocation_budgets=scopes)


def execute(states, cap=None, initial='A', fake=None, recorder=None):
    fake = fake or Fake()
    result = run(WorkUnit('task'), Flow(initial, {**states, 'DONE': State(terminal=True)}, cap),
                 {'x': fake}, recorder=recorder)
    return result, fake


def refusals(events):
    return [(e.state, e.metric, e.limit, e.observed) for e in events if e.kind == 'budget_exceeded']


class InvocationAccountingTests(unittest.TestCase):
    def test_run_cap_and_fallback_cannot_bypass_it(self):
        result, fake = execute({'A': state('B'), 'B': state('C'),
                                'C': state(fallback='D'), 'D': state()}, cap=2)
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(refusals(result.events), [('C', 'max_invocations_per_run', 2, 2),
                                                   ('D', 'max_invocations_per_run', 2, 2)])
        self.assertEqual([e.state for e in result.events if e.kind == 'state_entered'], ['A', 'B', 'C', 'D', 'DONE'])
        self.assertEqual(result.terminal_state, 'DONE')

    def test_shared_scope_across_states_and_run_reset(self):
        states = {'A': state('B', (Scope('shared', 2),), visits=5),
                  'B': state('A', (Scope('shared', 2),), visits=5)}
        for _ in range(2):
            result, fake = execute(states)
            self.assertEqual(len(fake.requests), 2)
            self.assertEqual(refusals(result.events), [('A', 'invocation_scope:shared', 2, 2)])
            self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'], ['A', 'B'])

    def test_unique_scope_bounds_one_state_attempts(self):
        result, fake = execute({'A': state('A', (Scope('one-attempt', 1),), visits=5)})
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(refusals(result.events), [('A', 'invocation_scope:one-attempt', 1, 1)])

    def test_independent_exact_scope_names_do_not_share(self):
        names = ('analysis', ' analysis', 'Analysis', 'foo', 'state:DIAGNOSE', 'x/y', 'invocation_scope:foo')
        states = {str(i): state(str(i+1) if i+1 < len(names) else 'DONE', (Scope(name, 1),))
                  for i, name in enumerate(names)}
        result, fake = execute(states, initial='0')
        self.assertEqual(len(fake.requests), len(names))
        self.assertEqual(refusals(result.events), [])

    def test_atomic_checks_do_not_consume_other_scope_or_run_capacity(self):
        # B checks spare first, then exhausted shared. C must still have both its
        # spare allowance and the last run invocation available.
        result, fake = execute({
            'A': state('B', (Scope('shared', 1),)),
            'B': state(scopes=(Scope('spare', 1), Scope('shared', 1)), fallback='C'),
            'C': state(scopes=(Scope('spare', 1),))}, cap=2)
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(refusals(result.events), [('B', 'invocation_scope:shared', 1, 1)])
        self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'], ['A', 'C'])

    def test_deterministic_precedence_visit_then_run_then_declared_scopes(self):
        for visits, cap, scopes, metric in [
            (0, 0, (Scope('first', 0),), 'max_state_visits_per_run'),
            (1, 0, (Scope('first', 0),), 'max_invocations_per_run'),
            (1, 1, (Scope('first', 0), Scope('second', 0)), 'invocation_scope:first'),
            (1, 1, (Scope('second', 0), Scope('first', 0)), 'invocation_scope:second')]:
            with self.subTest(metric=metric):
                result, fake = execute({'A': state(scopes=scopes, visits=visits)}, cap=cap)
                self.assertEqual(fake.requests, [])
                self.assertEqual(refusals(result.events), [('A', metric, 0, 0)])

    def test_failed_and_technical_outcomes_still_consume_capacity(self):
        for outcome in ('failed', 'error', 'timeout', 'cancelled', 'inconclusive'):
            with self.subTest(outcome=outcome):
                a = State('x', {outcome: 'A', 'budget_exceeded': 'DONE'},
                          max_state_visits_per_run=5, invocation_budgets=(Scope('attempt', 1),))
                result, fake = execute({'A': a}, fake=Fake(outcome))
                self.assertEqual(len(fake.requests), 1)
                self.assertEqual(refusals(result.events), [('A', 'invocation_scope:attempt', 1, 1)])
                self.assertEqual([e.outcome for e in result.events if e.kind == 'invocation_finished'], [outcome])

    def test_executor_exception_is_an_admitted_attempt(self):
        fake = Fake(error=RuntimeError('executor failed'))
        with self.assertRaisesRegex(ExecutionError, 'executor raised') as caught:
            execute({'A': state(scopes=(Scope('attempt', 1),))}, cap=1, fake=fake)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual([e.kind for e in caught.exception.events],
                         ['run_started', 'state_entered', 'invocation_started', 'run_error'])
        summary, = summarize_runs(caught.exception.events)
        self.assertEqual(summary.invocation_count, 1)
        self.assertFalse(summary.completed_normally)

    def test_fallback_consumes_own_scope_and_run_capacity(self):
        result, fake = execute({'A': state(scopes=(Scope('denied', 0),), fallback='B'),
                                'B': state('C', (Scope('fallback', 1),)),
                                'C': state(scopes=(Scope('fallback', 1),))}, cap=2)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(refusals(result.events), [('A', 'invocation_scope:denied', 0, 0),
                                                   ('C', 'invocation_scope:fallback', 1, 1)])

    def test_missing_route_retains_existing_failure_boundary(self):
        with self.assertRaisesRegex(ExecutionError, 'missing transition: A.budget_exceeded') as caught:
            execute({'A': state(fallback=None)}, cap=0)
        self.assertEqual([e.kind for e in caught.exception.events], ['run_started', 'state_entered', 'budget_exceeded', 'state_exited', 'run_error'])
        self.assertIsNotNone(caught.exception.events[2].state_visit_id)
        self.assertTrue(all(e.invocation_id is None for e in caught.exception.events))

    def test_existing_visit_limit_remains_independent(self):
        result, fake = execute({'A': state('A', (Scope('many', 10),), visits=1)}, cap=10)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(refusals(result.events), [('A', 'max_state_visits_per_run', 1, 1)])

    def test_strict_validation_and_conflicting_unreachable_scopes(self):
        for value in (-1, True, False, '2', 2.5):
            with self.subTest(run_limit=value), self.assertRaises(FlowValidationError):
                execute({'A': state()}, cap=value)
            with self.subTest(scope_limit=value), self.assertRaises(FlowValidationError):
                execute({'A': state(scopes=(Scope('x', value),))})
        for name in ('', '\0', 'x\0y', None, True, 1):
            with self.subTest(name=name), self.assertRaises(FlowValidationError):
                execute({'A': state(scopes=(Scope(name, 1),))})
        for scopes in ((Scope('x', 1), Scope('x', 1)), ('x',), (None,)):
            with self.subTest(scopes=scopes), self.assertRaises(FlowValidationError):
                execute({'A': state(scopes=scopes)})
        with self.assertRaisesRegex(FlowValidationError, 'conflicting'):
            execute({'A': state(scopes=(Scope('shared', 1),)),
                     'UNREACHABLE': state(scopes=(Scope('shared', 2),))})
        with self.assertRaisesRegex(FlowValidationError, 'terminal'):
            validate(Flow('DONE', {'DONE': State(terminal=True, invocation_budgets=(Scope('x', 1),))}), {})

    def test_scope_configuration_is_immutable(self):
        scope = Scope('x', 1)
        values = [scope]
        a = state(scopes=values)
        values.clear()
        self.assertEqual(a.invocation_budgets, (scope,))
        with self.assertRaises(FrozenInstanceError): scope.limit = 2
        with self.assertRaises(FrozenInstanceError): a.invocation_budgets = ()
        flow = Flow('A', {'A': a}, 1)
        with self.assertRaises(FrozenInstanceError): flow.max_invocations_per_run = 2

    def test_invocation_caps_do_not_relax_cycle_rules_or_allow_refusal_loops(self):
        for states, cap in [
            ({'A': state('A', (Scope('x', 1),))}, None),
            ({'A': state('A')}, 1),
            ({'A': state(scopes=(Scope('a', 0),), fallback='B'),
              'B': state(scopes=(Scope('b', 0),), fallback='A')}, None)]:
            with self.subTest(states=states), self.assertRaisesRegex(FlowValidationError, 'cycle'):
                execute(states, cap=cap)

    def test_canonical_metric_roundtrip_and_strict_reader(self):
        for scopes, cap, expected in [((Scope('x/y:名', 0),), None, 'invocation_scope:x/y:名'),
                                      ((), 0, 'max_invocations_per_run')]:
            with self.subTest(metric=expected), TemporaryDirectory() as folder:
                path = Path(folder) / 'events.jsonl'
                result, _ = execute({'A': state(scopes=scopes)}, cap=cap, recorder=JsonlRecorder(path))
                events = tuple(read_events(path))
                self.assertEqual(events, result.events)
                self.assertEqual(events[2].metric, expected)
                self.assertIsNone(events[2].target)
                self.assertEqual(events[3].target, 'DONE')
                self.assertEqual(summarize_runs(events)[0].budget_exceeded_count, 1)
                self.assertEqual(summarize_budgets(events)[0].budget_exceeded_count, 1)
                for bad in ('unknown', 'invocation_scope:', 'invocation_scope:x\0'):
                    with self.subTest(bad=bad), self.assertRaises(ObservabilityError):
                        summarize_runs((*events[:2], replace(events[2], metric=bad), *events[3:]))

    def test_recorder_failure_keeps_observed_admission_without_compensation(self):
        for kind in ('state_entered', 'invocation_started', 'budget_exceeded'):
            with self.subTest(kind=kind):
                persisted = []
                class Recorder:
                    def record(self, event):
                        if event.kind == kind: raise OSError('persistence failed')
                        persisted.append(event)
                fake = Fake()
                with self.assertRaisesRegex(ExecutionError, 'recorder failed') as caught:
                    execute({'A': state(scopes=(Scope('x', 1),))},
                            cap=0 if kind == 'budget_exceeded' else 1, fake=fake, recorder=Recorder())
                self.assertEqual(tuple(persisted), caught.exception.events[:-1])
                self.assertEqual(caught.exception.events[-1].kind, kind)
                self.assertEqual(fake.requests, [])
                self.assertFalse(any(e.kind == 'run_error' for e in caught.exception.events))
                self.assertEqual(summarize_runs(caught.exception.events)[0].invocation_count,
                                 1 if kind == 'invocation_started' else 0)

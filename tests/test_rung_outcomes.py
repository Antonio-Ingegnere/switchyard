"""V2-001E: restrictions are runtime contracts, not provider hints."""
from copy import deepcopy
from dataclasses import replace, FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import yaml

from flow_config import FlowConfigError
from runtime import ExecutionError, WorkUnit, run
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow, WorkflowCompileError
from switchyard.context_transport import workspace_resolver, StageResult, StageHandoff
from switchyard.provider_stages import result_schema
from switchyard.work_contract import WorkWorkspace
from test_workflow_compiler import fixture, loop_fixture, FakeResolver, EXAMPLE


def restricted():
    data = fixture()
    role = data['roles']['worker']
    for binding, outcome in zip([role['primary'], *role['escalation']],
                                ['inconclusive', 'diagnosis_disproved', 'ready']):
        binding['outcomes'] = [outcome]
    return data


class RungOutcomeTests(unittest.TestCase):
    def load(self, data):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'bundle.yaml'
            path.write_text(yaml.safe_dump(data))
            return load_workflow_bundle(path)

    def compile(self, data, resolver=None):
        return compile_workflow(self.load(data), 'test', resolver or FakeResolver())

    def test_binding_schema_and_immutability(self):
        role = self.load(restricted()).roles['worker']
        self.assertEqual(role.primary.outcomes, ('inconclusive',))
        self.assertEqual(role.escalation[0].binding.outcomes, ('diagnosis_disproved',))
        with self.assertRaises(FrozenInstanceError): role.primary.outcomes = ('ready',)
        values = ['ready']; copied = replace(role.primary, outcomes=values); values.clear()
        self.assertEqual(copied.outcomes, ('ready',))
        self.assertIsNone(self.load(fixture()).roles['worker'].primary.outcomes)

    def test_invalid_restrictions_and_unknown_keys(self):
        for slot in ('primary', 'escalation'):
            for value in ([], ['ready', 'ready'], [True], [1], [''], ['a\0b'],
                          ['budget_exceeded'], 'ready', None, {}, ['bad outcome']):
                with self.subTest(slot=slot, value=value):
                    data = fixture(); role = data['roles']['worker']
                    binding = role['primary'] if slot == 'primary' else role['escalation'][0]
                    binding['outcomes'] = value
                    with self.assertRaises(FlowConfigError): self.load(data)
            data = fixture(); role = data['roles']['worker']
            binding = role['primary'] if slot == 'primary' else role['escalation'][0]
            binding['outcome'] = ['ready']
            with self.assertRaises(FlowConfigError): self.load(data)

    def test_singletons_runtime_and_context_provider_contracts(self):
        bundle = self.load(restricted()); seen = []
        class Stage:
            def run(self, invocation):
                seen.append(invocation)
                return StageResult(invocation.output_contract.allowed_outcomes[0],
                                   handoff=StageHandoff(summary='bounded observation'))
        with TemporaryDirectory() as folder:
            workspace = WorkWorkspace.open(Path(folder), bundle.artifacts)
            workspace.initialize_request('request')
            compiled = compile_workflow(bundle, 'test', workspace_resolver(
                bundle, 'test', workspace, lambda binding: Stage()))
            rungs = [p for p in compiled.plan.values() if p.binding]
            expected = [('inconclusive',), ('diagnosis_disproved',), ('ready',)]
            self.assertEqual([tuple(p.state.on) for p in rungs], expected)
            self.assertEqual([p.executor_allowed_outcomes for p in rungs], expected)
            result = run(WorkUnit('request'), compiled.flow, compiled.executors)
            self.assertEqual(result.terminal_state, 'DONE')
            self.assertEqual([i.output_contract.allowed_outcomes for i in seen], expected)
            for invocation, allowed in zip(seen, expected):
                self.assertEqual(result_schema(invocation.output_contract)['properties']['outcome']['enum'], list(allowed))
            self.assertIsNotNone(seen[1].previous_handoff)
            self.assertIsNone(seen[2].previous_handoff)
            self.assertEqual([a.record.name for a in seen[2].artifacts], ['request'])

    def test_runtime_rejects_excluded_normal_outcome(self):
        resolver = FakeResolver({'A': ['ready']}); compiled = self.compile(restricted(), resolver)
        with self.assertRaises(ExecutionError):
            run(WorkUnit('request'), compiled.flow, compiled.executors)
        self.assertEqual(resolver.executed, [('A', 0)])

    def test_unroutable_and_role_reuse_checked_per_state(self):
        data = restricted(); data['roles']['worker']['primary']['outcomes'] = ['invented']
        with self.assertRaisesRegex(WorkflowCompileError, 'A rung 0: unroutable'): self.compile(data)
        data = fixture(); del data['roles']['worker']['escalation']
        data['roles']['worker']['primary']['outcomes'] = ['ready']
        states = data['flows']['test']['states']; del states['A']['escalation_exhausted']
        states['B'] = {'role': 'worker', 'on': {'other': 'DONE'}}
        states['A']['on']['ready'] = 'B'
        with self.assertRaisesRegex(WorkflowCompileError, 'B rung 0: unroutable'): self.compile(data)

    def test_adjacency_normal_precedence_and_final_exhaustion(self):
        data = restricted(); data['roles']['worker']['primary']['outcomes'] = ['diagnosis_disproved']
        resolver = FakeResolver({'A': ['diagnosis_disproved']})
        compiled = self.compile(data, resolver)
        self.assertEqual(run(WorkUnit('request'), compiled.flow, compiled.executors).terminal_state, 'STOP')
        self.assertEqual(resolver.executed, [('A', 0)])
        data['flows']['test']['states']['A']['on']['diagnosis_disproved'] = 'DONE'
        compiled = self.compile(data, FakeResolver({'A': ['diagnosis_disproved']}))
        self.assertEqual(run(WorkUnit('request'), compiled.flow, compiled.executors).terminal_state, 'DONE')
        data = restricted(); data['roles']['worker']['escalation'][1]['outcomes'] = ['inconclusive']
        resolver = FakeResolver({('A', 0): ['inconclusive'], ('A', 1): ['diagnosis_disproved'], ('A', 2): ['inconclusive']})
        compiled = self.compile(data, resolver)
        self.assertEqual(run(WorkUnit('request'), compiled.flow, compiled.executors).terminal_state, 'STOP')
        self.assertEqual(resolver.executed, [('A', 0), ('A', 1), ('A', 2)])

    def test_canonical_unrestricted_contract_unchanged(self):
        bundle = load_workflow_bundle(EXAMPLE)
        for flow in ('feature', 'bugfix'):
            compiled = compile_workflow(bundle, flow, FakeResolver())
            for plan in compiled.plan.values():
                if plan.logical_state == 'DIAGNOSE':
                    self.assertEqual(plan.executor_allowed_outcomes,
                        ('diagnosis_disproved', 'inconclusive', 'ready', 'verification_same_failure'))
        data = fixture(); first = self.compile(data)
        explicit = deepcopy(data)
        for binding in [explicit['roles']['worker']['primary'], *explicit['roles']['worker']['escalation']]:
            binding['outcomes'] = ['ready', 'verification_same_failure', 'inconclusive', 'diagnosis_disproved']
        self.assertEqual(first.flow, self.compile(explicit).flow)

    def test_command_mapping_must_fit_restriction(self):
        data = fixture(); data['roles']['command']['primary']['outcomes'] = ['ready']
        data['flows']['test']['states']['A'] = {'role': 'command', 'on': {'ready': 'DONE'}}
        self.assertEqual(tuple(self.compile(data).flow.states['@v2:test:A:0'].on), ('ready',))
        data['profiles']['cmd']['command']['outcomes']['default'] = 'fail'
        with self.assertRaisesRegex(FlowConfigError, 'excludes command'): self.load(data)
        data['roles']['command']['primary']['outcomes'].append('fail')
        data['flows']['test']['states']['A']['on']['fail'] = 'STOP'
        self.compile(data)

    def test_budgets_unchanged_and_real_rung_refusal(self):
        broad = self.compile(fixture()); narrow = self.compile(restricted())
        self.assertEqual(broad.flow.max_invocations_per_run, narrow.flow.max_invocations_per_run)
        self.assertEqual(broad.flow.max_invocations_exceeded, narrow.flow.max_invocations_exceeded)
        for key, state in narrow.flow.states.items():
            old = broad.flow.states[key]
            for attr in ('invocation_budgets', 'max_state_visits_per_run', 'state_visit_budget_exceeded'):
                self.assertEqual(getattr(state, attr), getattr(old, attr))
        data = loop_fixture(); data['roles']['worker']['primary']['outcomes'] = ['ready']
        data['roles']['worker']['escalation'][0]['outcomes'] = ['finish']
        resolver = FakeResolver({('A', 0): ['ready'], ('A', 1): ['finish']})
        compiled = self.compile(data, resolver)
        self.assertEqual(run(WorkUnit('request'), compiled.flow, compiled.executors).terminal_state, 'DONE')
        self.assertEqual(resolver.executed, [('A', 0), ('RETURN', 0), ('A', 1)])

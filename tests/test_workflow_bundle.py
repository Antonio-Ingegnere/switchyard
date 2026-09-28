from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from flow_config import FlowConfigError, load_flow
from switchyard.workflow_bundle import (
    BudgetSpec, ExecutionProfile, InvocationBinding, RoleBinding, EscalationStep,
    WorkflowBundle, WorkflowSpec, WorkflowStateSpec, load_workflow_bundle,
)

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/delivery/workflows-v2.yaml'


def minimal():
    return {'version': 2, 'profiles': {'cmd': {'provider': 'command'}},
            'roles': {'verify': {'primary': {'profile': 'cmd'}}},
            'flows': {'check': {'entry': 'VERIFY', 'run_budget': {'max_invocations': 1},
                               'states': {'VERIFY': {'role': 'verify', 'on': {'pass': 'DONE'}},
                                          'DONE': {'terminal': True}}}}}


def ladder():
    data = minimal()
    data['profiles'].update({'haiku': {'provider': 'claude', 'model': 'haiku'},
                             'sonnet': {'provider': 'claude', 'model': 'sonnet'},
                             'codex': {'provider': 'codex', 'model': 'codex'}})
    data['roles']['diagnostician'] = {
        'primary': {'profile': 'haiku', 'effort': 'medium', 'budget': {'max_invocations': 1}},
        'escalation': [
            {'kind': 'vertical', 'on': ['inconclusive'], 'profile': 'sonnet',
             'effort': 'high', 'context': 'continue', 'budget': {'max_invocations': 1}},
            {'kind': 'horizontal', 'on': ['diagnosis_disproved'], 'profile': 'codex',
             'effort': 'high', 'context': 'fresh', 'budget': {'max_invocations': 1}}]}
    return data


class WorkflowBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'bundle.yaml'

    def load(self, data=None, text=None):
        self.file.write_text(text if text is not None else yaml.safe_dump(data if data is not None else minimal()), encoding='utf-8')
        return load_workflow_bundle(self.file)

    def invalid(self, data, path):
        with self.assertRaises(FlowConfigError) as caught:
            self.load(data)
        self.assertIn(path, str(caught.exception))
        return str(caught.exception)

    def test_minimal_command_bundle(self):
        with patch('subprocess.Popen') as launch:
            bundle = self.load()
        launch.assert_not_called()
        self.assertIsInstance(bundle, WorkflowBundle)
        self.assertIsInstance(bundle.flow('check'), WorkflowSpec)
        self.assertEqual(bundle.version, 2)
        self.assertEqual(bundle.flow('check').run_budget.max_invocations, 1)
        self.assertEqual(bundle.roles['verify'].primary, InvocationBinding('cmd'))
        self.assertEqual(bundle.profiles['cmd'], ExecutionProfile('command'))

    def test_example_independent_flows(self):
        bundle = load_workflow_bundle(EXAMPLE)
        self.assertEqual(tuple(bundle.flows), ('feature', 'bugfix'))
        self.assertEqual(bundle.flow('feature').entry, 'SPECIFY')
        self.assertEqual(bundle.flow('bugfix').entry, 'TRIAGE')
        self.assertEqual(bundle.flow('feature').states['VERIFY'].on['fail'], 'DIAGNOSE_FAILURE')
        self.assertEqual(bundle.flow('bugfix').states['VERIFY'].on['fail'], 'REDIAGNOSE')
        self.assertEqual(bundle.flow('bugfix').states['REFIX'].role, 'fixer')
        self.assertFalse(hasattr(bundle.roles['verifier'], 'budget'))
        self.assertEqual(bundle.flow('feature').states['SPECIFY'].produces, ('microspec',))
        self.assertEqual(bundle, load_workflow_bundle(EXAMPLE))

    def test_lookup_errors(self):
        bundle = self.load()
        for name in ('missing', '', None, True, []):
            with self.subTest(name=name), self.assertRaises(FlowConfigError):
                bundle.flow(name)

    def test_deep_immutable_results(self):
        bundle = self.load(ladder())
        role = bundle.roles['diagnostician']
        flow = bundle.flow('check')
        objects = [(bundle, 'version'), (flow, 'entry'), (flow.states['VERIFY'], 'role'),
                   (role, 'primary'), (role.primary, 'profile'), (role.primary.budget, 'max_invocations'),
                   (role.escalation[0], 'context'), (bundle.profiles['haiku'], 'model')]
        for obj, attr in objects:
            with self.subTest(obj=type(obj).__name__), self.assertRaises(FrozenInstanceError):
                setattr(obj, attr, None)
        for mapping in (bundle.profiles, bundle.roles, bundle.flows, flow.states, flow.states['VERIFY'].on):
            with self.assertRaises(TypeError): mapping['other'] = None
        self.assertIsInstance(role.escalation, tuple)
        self.assertIsInstance(role.escalation[0].on, tuple)

    def test_constructor_collections_are_defensive_copies(self):
        on = {'pass': 'DONE'}; consumes = ['request']
        state = WorkflowStateSpec('verify', on, consumes)
        on.clear(); consumes.clear()
        self.assertEqual(dict(state.on), {'pass': 'DONE'})
        self.assertEqual(state.consumes, ('request',))
        states = {'VERIFY': state}
        flow = WorkflowSpec('VERIFY', BudgetSpec(max_invocations=1), states)
        states.clear()
        self.assertIn('VERIFY', flow.states)
        profiles = {'cmd': ExecutionProfile('command')}
        flows = {'test': flow}; roles = {'verify': RoleBinding(InvocationBinding('cmd'))}
        bundle = WorkflowBundle(2, profiles, roles, flows)
        profiles.clear(); roles.clear(); flows.clear()
        self.assertIn('cmd', bundle.profiles)
        self.assertIn('verify', bundle.roles)
        self.assertIs(bundle.flow('test'), flow)
        triggers = ['inconclusive']
        step = EscalationStep('horizontal', triggers, InvocationBinding('cmd'), 'fresh')
        steps = [step]; role = RoleBinding(InvocationBinding('cmd'), steps)
        triggers.clear(); steps.clear()
        self.assertEqual(role.escalation[0].on, ('inconclusive',))

    def test_invalid_yaml_and_root(self):
        for text in ('', '[', '[]', 'null', '42', 'true', 'a: 1\na: 2', 'a: &x 1', 'a: *x',
                     'a: !!str 1', '<<: {}', 'a: 1\n---\na: 2', '1: x', '? [a, b]\n: c'):
            with self.subTest(text=text), self.assertRaises(FlowConfigError): self.load(text=text)
        for version in (1, 3, '2', True, None, 2.0):
            data = minimal(); data['version'] = version
            self.invalid(data, 'version')
        for key in ('version', 'profiles', 'roles', 'flows'):
            data = minimal(); del data[key]
            self.invalid(data, key)
        for key in ('profiles', 'roles', 'flows'):
            for value in ({}, [], None, 'x'):
                data = minimal(); data[key] = value
                self.invalid(data, key)

    def test_unknown_fields_everywhere(self):
        selectors = [(), ('profiles', 'haiku'), ('roles', 'diagnostician'),
                     ('roles', 'diagnostician', 'primary'), ('roles', 'diagnostician', 'primary', 'budget'),
                     ('roles', 'diagnostician', 'escalation', 0),
                     ('roles', 'diagnostician', 'escalation', 0, 'budget'),
                     ('flows', 'check'),
                     ('flows', 'check', 'run_budget'), ('flows', 'check', 'states', 'VERIFY'),
                     ('flows', 'check', 'states', 'DONE')]
        for selector in selectors:
            with self.subTest(selector=selector):
                data = ladder(); target = data
                for key in selector: target = target[key]
                target['metadata'] = {}
                self.invalid(data, 'metadata')

    def test_profile_semantics(self):
        for value in (None, [], 'claude', 1):
            data = minimal(); data['profiles']['cmd'] = value
            self.invalid(data, 'profiles.cmd')
        for provider in ('', 'human', 'other', None, True, 4, []):
            data = minimal(); data['profiles']['cmd']['provider'] = provider
            self.invalid(data, 'profiles.cmd.provider')
        for model in ('', ' ', None, True, 1, [], '/Users/person/model'):
            data = ladder(); data['profiles']['haiku']['model'] = model
            self.invalid(data, 'profiles.haiku.model')
        data = ladder(); del data['profiles']['haiku']['model']
        self.invalid(data, 'profiles.haiku.model')
        for model in ('anything', None):
            data = minimal(); data['profiles']['cmd']['model'] = model
            self.invalid(data, 'profiles.cmd.model')
        for key in ('effort', 'budget', 'executable', 'api_key', 'role'):
            data = minimal(); data['profiles']['cmd'][key] = 'x'
            self.invalid(data, 'profiles.cmd.' + key)

    def test_names_are_logical_not_paths(self):
        for collection in ('profiles', 'roles', 'flows'):
            for name in ('', ' ', '/tmp/local', 'a/b'):
                data = minimal(); data[collection][name] = deepcopy(next(iter(data[collection].values())))
                self.invalid(data, collection)

    def test_binding_references_and_effort(self):
        for binding_path in ('primary', 'escalation'):
            for key, value in [('profile', 'missing'), ('profile', True), ('profile', []),
                               ('effort', 'max'), ('effort', None), ('effort', 1)]:
                data = ladder(); role = data['roles']['diagnostician']
                target = role['primary'] if binding_path == 'primary' else role['escalation'][0]
                target[key] = value
                self.invalid(data, key)
        for effort in ('low', None, False):
            data = minimal(); data['roles']['verify']['primary']['effort'] = effort
            self.invalid(data, 'roles.verify.primary.effort')
        data = ladder(); del data['roles']['diagnostician']['primary']['effort']
        self.assertIsNone(self.load(data).roles['diagnostician'].primary.effort)

    def test_state_rung_and_run_budget_scopes(self):
        data = ladder()
        data['flows']['check']['states']['VERIFY']['budget'] = {'max_invocations': 3, 'max_state_visits': 2}
        data['flows']['check']['run_budget'] = {'max_invocations': 10, 'max_state_visits': 20}
        bundle = self.load(data)
        self.assertEqual(bundle.flow('check').states['VERIFY'].budget, BudgetSpec(3, 2))
        self.assertEqual(bundle.flow('check').run_budget, BudgetSpec(10, 20))
        self.assertEqual(bundle.roles['verify'].primary.budget, BudgetSpec(max_invocations=1))
        self.assertEqual(bundle.roles['diagnostician'].escalation[0].binding.budget, BudgetSpec(max_invocations=1))
        self.assertIsNone(self.load().flow('check').states['VERIFY'].budget)
        for limits in ({'max_invocations': 2}, {'max_state_visits': 2}):
            data['flows']['check']['states']['VERIFY']['budget'] = limits
            self.load(data)
        data['roles']['verify']['primary']['budget'] = {'max_invocations': 2}
        self.assertEqual(self.load(data).roles['verify'].primary.budget.max_invocations, 2)

    def test_invalid_budget_values_at_every_scope(self):
        for where in ('primary', 'step', 'state', 'run'):
            for key in ('max_invocations', 'max_state_visits'):
                for value in (0, -1, True, 1.0, '1', None):
                    with self.subTest(where=where, key=key, value=value):
                        data = ladder()
                        target = (data['roles']['diagnostician']['primary'] if where == 'primary' else
                                  data['roles']['diagnostician']['escalation'][0] if where == 'step' else
                                  data['flows']['check']['states']['VERIFY'] if where == 'state' else data['flows']['check'])
                        target['run_budget' if where == 'run' else 'budget'] = {'max_invocations': 1, key: value}
                        self.invalid(data, key)
        data = minimal(); data['roles']['verify']['primary']['budget'] = {'max_invocations': 1, 'max_state_visits': 1}
        self.invalid(data, 'max_state_visits')

    def test_removed_budget_fields_rejected(self):
        for where in ('primary', 'step', 'state', 'run'):
            for key in ('max_duration_seconds', 'max_total_invocations', 'max_escalations'):
                data = ladder()
                target = (data['roles']['diagnostician']['primary'] if where == 'primary' else
                          data['roles']['diagnostician']['escalation'][0] if where == 'step' else
                          data['flows']['check']['states']['VERIFY'] if where == 'state' else data['flows']['check'])
                target['run_budget' if where == 'run' else 'budget'] = {'max_invocations': 1, key: 1}
                self.invalid(data, key)
        for key in ('max_invocations', 'max_total_invocations', 'max_escalations', 'max_duration_seconds'):
            data = ladder(); data['roles']['diagnostician']['budget'] = {key: 1}
            self.invalid(data, 'roles.diagnostician.budget')
            data = ladder(); data['roles']['diagnostician'][key] = 1
            self.invalid(data, key)
        self.assertEqual(set(BudgetSpec.__dataclass_fields__), {'max_invocations', 'max_state_visits'})

    def test_malformed_and_missing_budgets(self):
        for value in ({}, [], None, 1, 'x'):
            for where in ('primary', 'state', 'step', 'run'):
                data = ladder(); role = data['roles']['diagnostician']
                target = role['primary'] if where == 'primary' else role['escalation'][0] if where == 'step' else data['flows']['check']['states']['VERIFY']
                if where == 'run': data['flows']['check']['run_budget'] = value
                else: target['budget'] = value
                self.invalid(data, 'budget')
        data = minimal(); del data['flows']['check']['run_budget']
        self.invalid(data, 'flows.check.run_budget')
        data = minimal(); data['flows']['check']['run_budget'] = {'max_state_visits': 10}
        self.invalid(data, 'flows.check.run_budget.max_invocations')

    def test_verifier_reuse_has_independent_state_budgets(self):
        bundle = load_workflow_bundle(EXAMPLE)
        for flow in bundle.flows.values():
            first, second = flow.states['VERIFY'], flow.states['VERIFY_AGAIN']
            self.assertEqual((first.role, second.role), ('verifier', 'verifier'))
            self.assertEqual(first.budget, BudgetSpec(1, 1))
            self.assertEqual(second.budget, BudgetSpec(1, 1))
            self.assertIsNot(first.budget, second.budget)
        self.assertFalse(hasattr(bundle.roles['verifier'], 'budget'))
        self.assertEqual(bundle.roles['verifier'].primary.budget.max_invocations, 1)

    def test_valid_adjacent_escalations(self):
        role = self.load(ladder()).roles['diagnostician']
        self.assertEqual([(s.kind, s.binding.profile, s.context) for s in role.escalation],
                         [('vertical', 'sonnet', 'continue'), ('horizontal', 'codex', 'fresh')])
        data = ladder(); data['profiles']['codex-other'] = {'provider': 'codex', 'model': 'other'}
        data['roles']['diagnostician']['escalation'].append(
            {'kind': 'vertical', 'on': ['verification_same_failure'], 'profile': 'codex-other', 'context': 'continue'})
        self.assertEqual(len(self.load(data).roles['diagnostician'].escalation), 3)

    def test_effort_only_vertical_and_no_model_ranking(self):
        data = ladder(); role = data['roles']['diagnostician']
        role['escalation'][0]['profile'] = 'haiku'
        self.load(data)  # same profile, medium -> high
        role['primary']['effort'] = 'high'; role['escalation'][0]['effort'] = 'low'
        self.load(data)  # structural change only; no strength ranking

    def test_invalid_escalation_shapes(self):
        for value in (None, {}, 'x', 1):
            data = ladder(); data['roles']['diagnostician']['escalation'] = value
            self.invalid(data, 'escalation')
        for value in (None, [], 'x', 1):
            data = ladder(); data['roles']['diagnostician']['escalation'][0] = value
            self.invalid(data, 'escalation[0]')
        for key in ('kind', 'on', 'profile', 'context'):
            data = ladder(); del data['roles']['diagnostician']['escalation'][0][key]
            self.invalid(data, key)
        for key, value in [('kind', 'human'), ('kind', []), ('on', []), ('on', 'inconclusive'),
                           ('on', ['typo']), ('on', [1]), ('on', ['inconclusive', 'inconclusive']),
                           ('context', 'reuse'), ('context', None)]:
            data = ladder(); data['roles']['diagnostician']['escalation'][0][key] = value
            self.invalid(data, 'escalation[0].' + key)

    def test_escalation_provider_context_and_noop(self):
        cases = [(0, 'profile', 'codex'), (0, 'context', 'fresh'),
                 (1, 'profile', 'haiku'), (1, 'context', 'continue')]
        for index, key, value in cases:
            data = ladder(); data['roles']['diagnostician']['escalation'][index][key] = value
            self.invalid(data, f'escalation[{index}].{key}')
        data = ladder(); step = data['roles']['diagnostician']['escalation'][0]
        step.update(profile='haiku', effort='medium')
        self.invalid(data, 'escalation[0]')
        # Different budgets alone do not make a vertical rung meaningful.
        step['budget']['max_invocations'] = 2
        self.invalid(data, 'escalation[0]')
        data = ladder(); step = data['roles']['diagnostician']['escalation'][1]
        step['profile'] = 'cmd'
        self.invalid(data, 'escalation[1].effort')

    def test_exhaustion_required_and_targets_validated(self):
        data = ladder(); state = data['flows']['check']['states']['VERIFY']
        state['role'] = 'diagnostician'
        self.invalid(data, 'states.VERIFY.escalation_exhausted')
        for target in ('MISSING', '', None, [], True):
            state['escalation_exhausted'] = target
            self.invalid(data, 'states.VERIFY.escalation_exhausted')
        state['escalation_exhausted'] = 'DONE'
        result = self.load(data).flow('check').states['VERIFY']
        self.assertEqual(result.escalation_exhausted, 'DONE')
        with self.assertRaises(FrozenInstanceError): result.escalation_exhausted = 'OTHER'
        data['flows']['check']['states']['DONE']['escalation_exhausted'] = 'VERIFY'
        self.invalid(data, 'states.DONE.escalation_exhausted')
        data = minimal(); data['flows']['check']['states']['VERIFY']['escalation_exhausted'] = 'DONE'
        self.invalid(data, 'states.VERIFY.escalation_exhausted')

    def test_exhaustion_edge_counts_for_reachability(self):
        data = ladder(); flow = data['flows']['check']
        flow['states']['VERIFY'].update(role='diagnostician', escalation_exhausted='RECOVER')
        flow['states']['RECOVER'] = {'role': 'verify', 'on': {'ready': 'DONE'}}
        bundle = self.load(data)
        self.assertFalse(bundle.flow('check').states['RECOVER'].terminal)
        self.assertEqual(bundle.flow('check').states['VERIFY'].escalation_exhausted, 'RECOVER')

    def test_semantic_outcomes_and_adjacent_trigger_lists_preserved(self):
        data = ladder(); flow = data['flows']['check']
        flow['states']['VERIFY'].update(role='diagnostician', escalation_exhausted='DONE',
            on={name: 'DONE' for name in ('ready', 'reproduced', 'not_reproduced', 'inconclusive', 'diagnosis_disproved', 'needs_human', 'pass', 'fail')})
        bundle = self.load(data)
        self.assertEqual(dict(bundle.flow('check').states['VERIFY'].on), flow['states']['VERIFY']['on'])
        self.assertEqual(bundle.roles['diagnostician'].escalation[0].on, ('inconclusive',))
        self.assertEqual(bundle.roles['diagnostician'].escalation[1].on, ('diagnosis_disproved',))

    def test_states_entry_targets_and_reachability(self):
        for key, value in [('entry', 'MISSING'), ('entry', None), ('states', {}), ('states', [])]:
            data = minimal(); data['flows']['check'][key] = value
            self.invalid(data, 'flows.check.' + key)
        data = minimal(); del data['flows']['check']['entry']
        self.invalid(data, 'flows.check.entry')
        for state in ({'on': {'pass': 'DONE'}}, {'role': 'typo', 'on': {'pass': 'DONE'}},
                      {'role': 'verify'}, {'role': 'verify', 'on': {}},
                      {'role': 'verify', 'on': {'fail': 'REDIAGNOS'}},
                      {'role': 'verify', 'on': []}, {'role': 'verify', 'on': {'fail': None}},
                      {'role': 'verify', 'on': {'': 'DONE'}}, None, []):
            data = minimal(); data['flows']['check']['states']['VERIFY'] = state
            self.invalid(data, 'flows.check.states.VERIFY')
        data = minimal(); data['flows']['check']['states']['STRAY'] = {'terminal': True}
        self.assertIn('unreachable', self.invalid(data, 'states.STRAY'))

    def test_terminal_rules(self):
        for definition in ({'terminal': False}, {'terminal': 1}, {'terminal': 'true'},
                           {'terminal': True, 'role': 'verify'}, {'terminal': True, 'on': {}},
                           {'terminal': True, 'on': {'pass': 'VERIFY'}},
                           {'terminal': True, 'consumes': []}, {'terminal': True, 'produces': []}):
            data = minimal(); data['flows']['check']['states']['DONE'] = definition
            self.invalid(data, 'states.DONE')
        data = minimal(); data['flows']['check']['entry'] = 'DONE'
        del data['flows']['check']['states']['VERIFY']
        self.assertTrue(self.load(data).flow('check').states['DONE'].terminal)

    def test_cycles_allowed_with_fuse(self):
        data = minimal(); data['flows']['check']['states']['VERIFY']['on']['retry'] = 'VERIFY'
        self.load(data)

    def test_artifact_validation(self):
        for field in ('consumes', 'produces'):
            for value in ('request', None, {}, [None], [1], [True], [''], [' '], ['a', 'a'], ['/tmp/file']):
                with self.subTest(field=field, value=value):
                    data = minimal(); data['flows']['check']['states']['VERIFY'][field] = value
                    self.invalid(data, 'states.VERIFY.' + field)
        data = minimal(); state = data['flows']['check']['states']['VERIFY']
        state.update(consumes=['external_input'], produces=['external_input'])
        self.load(data)  # No artifact availability or lineage analysis in this slice.

    def test_wrong_shapes_and_required_role_binding(self):
        for value in ([], None, 'x', {}):
            data = minimal(); data['roles']['verify'] = value
            self.invalid(data, 'roles.verify')
            data = minimal(); data['roles']['verify']['primary'] = value
            self.invalid(data, 'roles.verify.primary')
            data = minimal(); data['flows']['check'] = value
            self.invalid(data, 'flows.check')

    def test_error_paths(self):
        data = ladder(); data['roles']['diagnostician']['escalation'][1]['profile'] = 'foo'
        self.assertIn("roles.diagnostician.escalation[1].profile: unknown profile 'foo'", self.invalid(data, 'profile'))
        data = minimal(); data['flows']['check']['states']['VERIFY']['on']['fail'] = 'REDIAGNOS'
        self.assertIn("flows.check.states.VERIFY.on.fail: unknown target 'REDIAGNOS'", self.invalid(data, 'on.fail'))

    def test_io_errors_are_configuration_errors(self):
        for path in (self.file, self.file.parent, None):
            with self.assertRaises(FlowConfigError): load_workflow_bundle(path)
        self.file.write_bytes(b'\xff')
        with self.assertRaises(FlowConfigError): load_workflow_bundle(self.file)

    def test_v1_and_v2_are_explicit(self):
        self.load()
        with self.assertRaises(FlowConfigError): load_flow(self.file)
        old = {'version': 1, 'initial': 'verify', 'executors': {'cmd': {
            'type': 'command', 'executable': sys.executable, 'argv': [], 'timeout_seconds': 1}},
            'states': {'verify': {'executor': 'cmd', 'on': {'success': 'done'}}, 'done': {'terminal': True}}}
        self.file.write_text(yaml.safe_dump(old))
        self.assertEqual(load_flow(self.file).flow.initial, 'verify')
        with self.assertRaises(FlowConfigError): load_workflow_bundle(self.file)


if __name__ == '__main__':
    unittest.main()

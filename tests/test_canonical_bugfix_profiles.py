"""V2-001F: canonical bindings and rendered TRIAGE input, with no processes."""
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import InvocationRequest, WorkUnit
from switchyard.context_transport import workspace_resolver
from switchyard.provider_stages import (ProviderRuntimeConfig, ProviderStageExecutorResolver,
                                       render_invocation, result_schema)
from switchyard.work_contract import WorkWorkspace, microspec_guidance, handoff_guidance
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow

CANONICAL = Path(__file__).resolve().parents[1]/'examples/delivery/workflows-v2.yaml'


class CanonicalBugfixProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        # Resolution only: no model/command process may be launched by these tests.
        command = root/'python3'; command.write_text('unused'); command.chmod(0o755)
        self.bundle = load_workflow_bundle(CANONICAL)
        self.workspace = WorkWorkspace.open(root/'work', self.bundle.artifacts)
        self.workspace.initialize_request('Report an ordering bug.')
        self.config = ProviderRuntimeConfig(str(root), sys.executable, sys.executable, {'PATH':str(root)})
        guard = patch('switchyard.provider_stages.execute', side_effect=AssertionError('process forbidden'))
        self.execute = guard.start(); self.addCleanup(guard.stop)

    def compile(self, flow='bugfix', bundle=None):
        bundle = bundle or self.bundle
        return compile_workflow(bundle, flow, workspace_resolver(bundle, flow, self.workspace,
                                ProviderStageExecutorResolver(bundle.profiles, self.config)))

    def test_compiled_canonical_bindings_and_exact_change_scope(self):
        after = self.compile()
        expected = {
            'TRIAGE': ('triager','claude-sonnet','sonnet',('repo_read',)),
            'REPRODUCE': ('reproducer','claude-haiku','haiku',('repo_read','command')),
            'DIAGNOSE': ('diagnostician','claude-haiku','haiku',('repo_read','command')),
            'SPECIFY_FIX': ('specifier','claude-sonnet','sonnet',('repo_read',)),
            'FIX': ('fixer','claude-sonnet','sonnet',('repo_read','repo_write','command')),
        }
        for state, (role, profile, model, capabilities) in expected.items():
            wrapper = after.executors[after.entry_states[state]]
            binding = wrapper.binding
            with self.subTest(state=state):
                self.assertEqual((binding.provider,binding.role_name,binding.profile_name,binding.model,
                                  binding.effort,binding.capabilities,binding.rung_index),
                                 ('claude',role,profile,model,'medium',capabilities,0))
                self.assertEqual(wrapper.stage_executor.resolved_model, model)
        # Reconstruct only the prior triager selection; prove no other plan/rung changes.
        roles = dict(self.bundle.roles)
        roles['triager'] = replace(roles['triager'], primary=replace(roles['triager'].primary, profile='claude-haiku'))
        previous = replace(self.bundle, roles=roles)
        before = self.compile(bundle=previous)
        for key, plan in after.plan.items():
            if key == after.entry_states['TRIAGE']:
                self.assertEqual(plan, replace(before.plan[key], binding=replace(before.plan[key].binding,
                                 profile_name='claude-sonnet', model='sonnet')))
            else:
                self.assertEqual(plan, before.plan[key])
        feature = self.compile('feature')
        self.assertEqual(feature.plan, self.compile('feature', previous).plan)
        specify = feature.executors[feature.entry_states['SPECIFY']].binding
        self.assertEqual((specify.profile_name,specify.model,specify.effort,specify.capabilities),
                         ('claude-sonnet','sonnet','medium',('repo_read',)))
        self.execute.assert_not_called()

    def test_real_triage_render_preserves_all_contract_guidance(self):
        compiled = self.compile()
        wrapper = compiled.executors[compiled.entry_states['TRIAGE']]
        invocation = wrapper._context(InvocationRequest(WorkUnit('Report an ordering bug.')))
        rendered = render_invocation(invocation)
        self.assertEqual(wrapper.stage_executor.resolved_model, 'sonnet')
        self.assertEqual(invocation.binding.capabilities, ('repo_read',))
        self.assertIn('AVAILABLE EXECUTION CAPABILITIES\n- repository read\n\n', rendered)
        self.assertIn(microspec_guidance('yaml'), rendered)
        self.assertIn('- acceptance: a YAML sequence', rendered)
        self.assertIn(handoff_guidance(invocation.output_contract.handoff), rendered)
        self.assertEqual(invocation.output_contract.handoff.evidence_artifacts, tuple(sorted(self.bundle.artifacts)))
        self.assertIn('Return exactly one JSON StageResult object containing ALL of these top-level keys.', rendered)
        self.assertIn('outcome', rendered); self.assertIn('produced_artifacts', rendered)
        self.assertIn('handoff', rendered); self.assertIn('diagnostics', rendered)
        self.assertEqual(result_schema(invocation.output_contract,'claude')['required'], ['outcome','produced_artifacts'])
        self.assertEqual([a.name for a in invocation.output_contract.artifacts], ['bug_spec'])
        self.assertEqual(invocation.output_contract.artifacts[0].schema, 'microspec')
        self.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()

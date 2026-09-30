from dataclasses import asdict, FrozenInstanceError, replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from runtime import WorkUnit, InvocationRequest
from switchyard.context_transport import (workspace_resolver, StageResult, StageHandoff, ProducedArtifact,
    StageOutputContract, StageOutputArtifactContract, StageHandoffContract, MAX_OUTPUT_CONTRACT_BYTES)
from switchyard.provider_stages import (ProviderRuntimeConfig, ProviderStageExecutorResolver,
    render_invocation, result_schema)
from switchyard.work_contract import WorkWorkspace, WorkContractError, MICROSPEC_FIELDS, validate_microspec
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow
from test_context_transport import SPEC, REQUEST, Fake
from test_workflow_compiler import EXAMPLE


class StageOutputContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = load_workflow_bundle(EXAMPLE)
        self.workspace = WorkWorkspace.open(self.root/'work', self.bundle.artifacts)
        self.workspace.initialize_request(REQUEST)

    def compile(self, flow='feature', resolver=None):
        return compile_workflow(self.bundle, flow, workspace_resolver(
            self.bundle, flow, self.workspace, resolver or Fake(self.bundle, flow)))

    def specify(self):
        compiled = self.compile()
        return compiled.executors[compiled.entry_states['SPECIFY']]

    def test_specify_contract_and_old_failure_remain_strict(self):
        wrapper = self.specify()
        contract = wrapper.output_contract
        self.assertEqual(contract.allowed_outcomes, ('needs_clarification', 'ready'))
        self.assertEqual(len(contract.artifacts), 1)
        artifact = contract.artifacts[0]
        self.assertEqual((artifact.name, artifact.format, artifact.schema), ('microspec','yaml','microspec'))
        self.assertEqual(artifact.schema_fields, MICROSPEC_FIELDS)
        self.assertEqual(tuple(f.name for f in artifact.schema_fields),
                         ('id','goal','scope','constraints','acceptance','verification','non_goals'))
        class Bad:
            def run(self, invocation):
                return StageResult('ready', (ProducedArtifact('spec.md', SPEC),), StageHandoff('summary'))
        with self.assertRaisesRegex(WorkContractError, 'undeclared production: spec.md'):
            replace(wrapper, stage_executor=Bad()).run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertIsNone(self.workspace.latest_artifact('microspec'))
        self.assertEqual(self.workspace.handoffs(), ())
        class Aware:
            def run(self, invocation):
                output = invocation.output_contract.artifacts[0]
                assert output.name == 'microspec' and output.schema == 'microspec'
                assert 'ready' in invocation.output_contract.allowed_outcomes
                return StageResult('ready', (ProducedArtifact(output.name, SPEC),), StageHandoff('summary'))
        replace(wrapper, stage_executor=Aware()).run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertEqual(self.workspace.read_artifact('microspec'), SPEC)
        self.assertEqual(len(self.workspace.handoffs()), 1)

    def test_outcomes_come_from_every_actual_compiled_rung(self):
        compiled = self.compile('bugfix')
        diagnosis = []
        for key, plan in compiled.plan.items():
            if plan.binding is None: continue
            expected = tuple(sorted(k for k in plan.state.on if k != 'budget_exceeded'))
            self.assertEqual(plan.executor_allowed_outcomes, expected)
            self.assertEqual(compiled.executors[key].output_contract.allowed_outcomes, expected)
            self.assertNotIn('budget_exceeded', expected)
            if plan.logical_state == 'DIAGNOSE': diagnosis.append(plan)
        self.assertEqual([p.binding.rung_index for p in diagnosis], [0,1,2])
        role = self.bundle.roles[diagnosis[0].binding.role_name]
        for plan in diagnosis:
            index = plan.binding.rung_index
            if index < len(role.escalation):
                for trigger in role.escalation[index].on:
                    if trigger != 'budget_exceeded': self.assertIn(trigger, plan.executor_allowed_outcomes)
        # The context resolver consumes each binding's contract rather than rebuilding
        # a logical-state enum, even when presented with different compiler metadata.
        resolve = workspace_resolver(self.bundle, 'bugfix', self.workspace, Fake(self.bundle, 'bugfix'))
        for plan, allowed in zip(diagnosis, [('ready',), ('inconclusive',), ('needs_human',)]):
            wrapper = resolve(replace(plan.binding, executor_allowed_outcomes=allowed))
            self.assertEqual(wrapper.output_contract.allowed_outcomes, allowed)

    def test_optional_production_command_and_handoff_contract(self):
        wrapper = self.specify()
        policy = wrapper.output_contract.handoff
        self.assertTrue(policy.expected); self.assertTrue(policy.supported)
        self.assertEqual(policy.body_fields, ('summary','findings','evidence','changed_assumptions','blockers'))
        self.assertEqual(set(policy.evidence_artifacts), set(self.bundle.artifacts))
        class Empty:
            def run(self, invocation): return StageResult('needs_clarification')
        replace(wrapper, stage_executor=Empty()).run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertIsNone(self.workspace.latest_artifact('microspec'))
        compiled = self.compile()
        verifier = compiled.executors[compiled.entry_states['VERIFY']].output_contract
        command = self.bundle.profiles['command'].command
        self.assertFalse(verifier.handoff.expected)
        self.assertTrue(set(command.outcomes.exit_codes.values()) | {command.outcomes.default} <= set(verifier.allowed_outcomes))
        self.assertIn(command.capture.artifact, [a.name for a in verifier.artifacts])

    def test_fresh_and_continue_selection_unchanged(self):
        from runtime import run
        fake = Fake(self.bundle, 'bugfix', {('DIAGNOSE',0):'inconclusive', ('DIAGNOSE',1):'diagnosis_disproved'})
        compiled = self.compile('bugfix', fake)
        run(WorkUnit(REQUEST), compiled.flow, compiled.executors)
        calls = [c for c in fake.calls if c.binding.logical_state == 'DIAGNOSE']
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1].previous_handoff.rung_index, 0)
        self.assertIsNone(calls[2].previous_handoff)
        self.assertEqual(calls[2].binding.context_mode, 'fresh')
        self.assertEqual([a.record.name for a in calls[2].artifacts], ['request','bug_spec','reproduction_evidence'])
        for invocation in calls:
            self.assertEqual(invocation.output_contract.allowed_outcomes, invocation.binding.executor_allowed_outcomes)
            self.assertEqual([a.name for a in invocation.output_contract.artifacts], ['diagnosis'])

    def test_immutable_contract_and_separate_context_bounds(self):
        wrapper = self.specify()
        context = wrapper._context(InvocationRequest(WorkUnit(REQUEST)))
        contract = context.output_contract
        with self.assertRaises(FrozenInstanceError): contract.allowed_outcomes = ()
        with self.assertRaises(FrozenInstanceError): contract.artifacts[0].name = 'other'
        with self.assertRaises(WorkContractError): replace(contract, allowed_outcomes=('budget_exceeded',))
        with self.assertRaisesRegex(WorkContractError, 'byte limit'):
            replace(contract, allowed_outcomes=('x'*MAX_OUTPUT_CONTRACT_BYTES,))
        payload = asdict(context); del payload['output_contract']; del payload['binding']['executor_allowed_outcomes']
        size = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        replace(wrapper, max_context_bytes=size)._context(InvocationRequest(WorkUnit(REQUEST)))
        with self.assertRaisesRegex(WorkContractError, 'context exceeds'):
            replace(wrapper, max_context_bytes=size-1)._context(InvocationRequest(WorkUnit(REQUEST)))
        schema = result_schema(replace(contract, artifacts=()))
        self.assertEqual(schema['properties']['produced_artifacts']['maxItems'], 0)

    @unittest.skipIf(os.name == 'nt', 'fake executable requires POSIX shebang')
    def test_shared_rendering_and_native_schema_for_both_fake_providers(self):
        context = self.specify()._context(InvocationRequest(WorkUnit(REQUEST)))
        rendered = render_invocation(context)
        self.assertNotIn('spec.md', rendered)
        described = json.loads(rendered.split('STAGE OUTPUT CONTRACT\n')[1].split('\n\n')[0])
        self.assertEqual(described['allowed_outcomes'], ['needs_clarification','ready'])
        self.assertEqual(described['artifacts'][0]['format'], 'yaml')
        self.assertEqual(described['artifacts'][0]['schema'], 'microspec')
        self.assertIn('without markdown fences', rendered)
        self.assertIn('never filenames or paths', rendered)
        script = (Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        for name in ('claude','codex'):
            path = self.root/name; path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        log = self.root/'calls.jsonl'; plan = self.root/'plan.json'; plan.write_text('{"artifacts":{}}')
        config = ProviderRuntimeConfig(str(self.root), str(self.root/'claude'), str(self.root/'codex'),
            environment={'FAKE_LOG':str(log),'FAKE_PLAN':str(plan),'FAKE_CONTRACT_AWARE':'1'})
        resolver = ProviderStageExecutorResolver(self.bundle.profiles, config)
        for profile in ('claude-sonnet','codex'):
            spec = self.bundle.profiles[profile]
            binding = replace(context.binding, profile_name=profile, provider=spec.provider, model=spec.model)
            invocation = replace(context, binding=binding)
            result = resolver(binding).run(invocation)
            self.assertEqual(result.produced_artifacts[0].name, 'microspec')
            validate_microspec(result.produced_artifacts[0].content)
            call = json.loads(log.read_text().splitlines()[-1])
            schema = call.get('schema') or json.loads(call['argv'][call['argv'].index('--json-schema')+1])
            self.assertEqual(schema, result_schema(invocation.output_contract))
            self.assertEqual(schema['properties']['outcome']['enum'], ['needs_clarification','ready'])
            self.assertEqual(schema['properties']['produced_artifacts']['items']['properties']['name']['enum'], ['microspec'])

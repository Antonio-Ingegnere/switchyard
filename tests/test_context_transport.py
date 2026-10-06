from dataclasses import replace, FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import yaml

from runtime import WorkUnit, InvocationRequest, DiagnosticPayload, DiagnosticInput, DiagnosticOrigin, run
from switchyard.context_transport import (ContextualExecutor, ProducedArtifact, StageHandoff,
                                           StageResult, workspace_resolver)
from switchyard.work_contract import WorkWorkspace, WorkContractError
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow
from test_workflow_compiler import EXAMPLE, loop_fixture

REQUEST = 'When tabbing from a task row, focus skips the next row.\nKeep navigation natural.'
SPEC = yaml.safe_dump(dict(id='test', goal='Natural navigation', scope=['focus'],
                          constraints=[], acceptance=['Tab follows order'], verification=['test'], non_goals=[]))


class Fake:
    def __init__(self, bundle, flow, outcomes=None):
        self.bundle, self.flow, self.outcomes = bundle, flow, outcomes or {}
        self.bindings, self.calls = [], []

    def __call__(self, binding):
        self.bindings.append(binding)
        owner = self
        class Stage:
            def run(self, invocation):
                owner.calls.append(invocation)
                key = (binding.logical_state, binding.rung_index)
                outcome = owner.outcomes.get(key, 'pass' if binding.logical_state.startswith('VERIFY') else 'ready')
                state = owner.bundle.flow(owner.flow).states[binding.logical_state]
                outputs = tuple(ProducedArtifact(n, SPEC if owner.bundle.artifacts[n].schema else 'evidence '+str(len(owner.calls)))
                                for n in state.produces)
                return StageResult(outcome, outputs, StageHandoff('bounded reasoning', evidence=state.consumes))
        return Stage()


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = load_workflow_bundle(EXAMPLE)
        self.workspace = WorkWorkspace.open(self.root/'work', self.bundle.artifacts)
        self.workspace.initialize_request(REQUEST)

    def execute(self, flow, outcomes=None):
        fake = Fake(self.bundle, flow, outcomes)
        compiled = compile_workflow(self.bundle, flow, workspace_resolver(self.bundle, flow, self.workspace, fake))
        result = run(WorkUnit(REQUEST), compiled.flow, compiled.executors)
        self.assertEqual(len(fake.bindings), len(compiled.executors))
        return fake, result

    def wrapper(self, state='SPECIFY', rung=0, result=None):
        fake = Fake(self.bundle, 'feature')
        compiled = compile_workflow(self.bundle, 'feature', workspace_resolver(self.bundle, 'feature', self.workspace, fake))
        wrapper = next(e for e in compiled.executors.values() if e.binding.logical_state == state and e.binding.rung_index == rung)
        if result is not None:
            class Stage:
                calls = 0
                def run(self, invocation):
                    self.calls += 1
                    return result
            wrapper = replace(wrapper, stage_executor=Stage())
        return wrapper

    def test_feature_transport(self):
        fake, _ = self.execute('feature')
        self.assertEqual([c.binding.logical_state for c in fake.calls], ['SPECIFY','IMPLEMENT','VERIFY'])
        inputs = fake.calls[1].artifacts
        self.assertEqual([a.record.name for a in inputs], ['request','microspec'])
        self.assertEqual(inputs[0].content, REQUEST)
        self.assertEqual(inputs[1].content, SPEC)
        self.assertEqual(inputs[1].record.version, 1)
        self.assertEqual(self.workspace.read_artifact('request'), REQUEST)
        self.assertTrue(all(c.previous_handoff is None for c in fake.calls))
        with self.assertRaises(FrozenInstanceError): inputs[0].content = 'changed'

    def test_bugfix_straight(self):
        fake, _ = self.execute('bugfix')
        self.assertEqual([c.binding.logical_state for c in fake.calls],
                         ['TRIAGE','REPRODUCE','DIAGNOSE','SPECIFY_FIX','FIX','VERIFY'])
        self.assertEqual([a.record.name for a in fake.calls[1].artifacts],['request','bug_spec'])
        self.assertEqual(fake.calls[4].artifacts[0].content,SPEC)
        self.assertTrue(all(c.previous_handoff is None for c in fake.calls))

    def test_feature_vertical_and_bugfix_horizontal(self):
        for flow, state in [('feature','DIAGNOSE_FAILURE'),('bugfix','DIAGNOSE')]:
            with self.subTest(flow=flow):
                self.workspace = WorkWorkspace.open(self.root/flow, self.bundle.artifacts)
                self.workspace.initialize_request(REQUEST)
                fake, _ = self.execute(flow, {('VERIFY',0): 'fail' if flow=='feature' else 'pass',
                                             (state,0):'inconclusive', (state,1):'diagnosis_disproved'})
                calls = [c for c in fake.calls if c.binding.logical_state==state]
                self.assertEqual(len(calls),3)
                self.assertEqual(calls[1].previous_handoff.rung_index,0)
                self.assertEqual(calls[1].previous_handoff.summary,'bounded reasoning')
                self.assertIsNone(calls[2].previous_handoff)
                self.assertEqual(calls[2].binding.context_mode,'fresh')
                if flow=='bugfix':
                    self.assertEqual([a.record.name for a in calls[2].artifacts], ['request','bug_spec','reproduction_evidence'])
                    fix = next(c for c in fake.calls if c.binding.logical_state=='FIX')
                    self.assertIn('fix_microspec', [a.record.name for a in fix.artifacts])
                self.assertEqual(len(self.workspace.handoffs(state)),3)

    def test_later_visit_continuation(self):
        path = self.root/'loop.yaml'; path.write_text(yaml.safe_dump(loop_fixture()))
        self.bundle = load_workflow_bundle(path)
        self.workspace = WorkWorkspace.open(self.root/'loop', self.bundle.artifacts)
        self.workspace.initialize_request(REQUEST)
        fake, result = self.execute('test')
        calls = [c for c in fake.calls if c.binding.logical_state=='A']
        self.assertEqual([c.binding.rung_index for c in calls], [0,1])
        self.assertEqual(calls[1].previous_handoff.sequence,1)

    def test_missing_and_mutated_inputs_and_request_mismatch(self):
        for mode in ['missing','mutated','mismatch']:
            wrapper = self.wrapper('IMPLEMENT', result=StageResult('ready'))
            if mode=='mutated':
                record=self.workspace.write_artifact('microspec',SPEC)
                (self.workspace.root/record.path).write_text('changed')
            task = 'different' if mode=='mismatch' else REQUEST
            with self.subTest(mode=mode), self.assertRaises(WorkContractError):
                wrapper.run(InvocationRequest(WorkUnit(task)))
            self.assertEqual(wrapper.stage_executor.calls,0)

    def test_missing_continue_and_no_implicit_request(self):
        wrapper = self.wrapper('DIAGNOSE_FAILURE',1,result=StageResult('ready'))
        for name in wrapper.declaration.consumes:
            self.workspace.write_artifact(name,SPEC if self.bundle.artifacts[name].schema else 'facts')
        with self.assertRaisesRegex(WorkContractError,'missing continuation'):
            wrapper.run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertEqual(wrapper.stage_executor.calls,0)
        fresh = replace(wrapper,binding=replace(wrapper.binding,context_mode='fresh'))
        context=fresh._context(InvocationRequest(WorkUnit(REQUEST)))
        self.assertNotIn('request',[a.record.name for a in context.artifacts])

    def test_all_outputs_prevalidated(self):
        invalids = [StageResult('ready',(ProducedArtifact('microspec','bad'),),StageHandoff('summary')),
                    StageResult('ready',(ProducedArtifact('microspec',SPEC),ProducedArtifact('diagnosis','oops'))),
                    StageResult('ready',(ProducedArtifact('microspec',SPEC),ProducedArtifact('microspec',SPEC))),
                    StageResult('ready',(ProducedArtifact('microspec',SPEC),),StageHandoff('s',evidence=('../file',))),
                    StageResult('budget_exceeded'), StageResult(4)]
        for result in invalids:
            with self.subTest(result=result), self.assertRaises(WorkContractError):
                self.wrapper(result=result).run(InvocationRequest(WorkUnit(REQUEST)))
            self.assertIsNone(self.workspace.latest_artifact('microspec'))
            self.assertEqual(self.workspace.handoffs(),())

    def test_rejected_outcome_and_artifact_name_never_publish_or_leak(self):
        marker = 'UNTRUSTED_SECRET_MARKER_' * 100
        results = [StageResult(marker, (ProducedArtifact('microspec', SPEC),), StageHandoff('valid')),
                   StageResult('ready', (ProducedArtifact(marker, 'content'),))]
        before = (self.workspace.root/'manifest.jsonl').read_bytes()
        for result in results:
            with self.subTest(outcome=result.outcome == 'ready'):
                with self.assertRaises(WorkContractError) as caught:
                    self.wrapper(result=result).run(InvocationRequest(WorkUnit(REQUEST)))
                error = caught.exception
                while error is not None:
                    self.assertNotIn(marker, str(error))
                    error = error.__cause__
                self.assertEqual((self.workspace.root/'manifest.jsonl').read_bytes(), before)
                self.assertIsNone(self.workspace.latest_artifact('microspec'))
                self.assertEqual(self.workspace.handoffs(), ())

    def test_latest_snapshot_isolation_and_bound(self):
        wrapper=self.wrapper('IMPLEMENT')
        self.workspace.write_artifact('microspec',SPEC)
        first=wrapper._context(InvocationRequest(WorkUnit(REQUEST)))
        second_record=self.workspace.write_artifact('microspec',SPEC+'# revised\n')
        second=wrapper._context(InvocationRequest(WorkUnit(REQUEST)))
        self.assertEqual(first.artifacts[1].record.version,1)
        self.assertEqual(second.artifacts[1].record,second_record)
        other=WorkWorkspace.open(self.root/'other',self.bundle.artifacts)
        other.initialize_request('other')
        self.assertIsNone(other.latest_artifact('microspec'))
        self.assertEqual(other.handoffs(),())
        with self.assertRaisesRegex(WorkContractError,'context exceeds'):
            replace(wrapper,max_context_bytes=10).run(InvocationRequest(WorkUnit(REQUEST)))

    def test_optional_handoff_diagnostics_and_io_failure(self):
        payload=DiagnosticPayload(b'verifier failure')
        response=self.wrapper(result=StageResult('needs_clarification',diagnostics=payload)).run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertEqual(response.diagnostics,payload)
        self.assertEqual(self.workspace.handoffs(),())
        wrapper=self.wrapper(result=StageResult('ready',(ProducedArtifact('microspec',SPEC),),StageHandoff('s')))
        with patch.object(WorkWorkspace,'write_handoff',side_effect=WorkContractError('disk failure')):
            with self.assertRaisesRegex(WorkContractError,'disk failure'):
                wrapper.run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertIsNotNone(self.workspace.latest_artifact('microspec'))
        self.assertEqual(self.workspace.handoffs(),())

    def test_provenance_diagnostics_and_primary_history(self):
        incoming = DiagnosticInput(DiagnosticPayload(b'bounded failure'), DiagnosticOrigin('run','verify','visit','invoke'))
        wrapper = self.wrapper(result=StageResult('ready', (ProducedArtifact('microspec',SPEC),),
                                                  StageHandoff('reason', findings=['one'], evidence=['request'])))
        wrapper.run(InvocationRequest(WorkUnit(REQUEST),incoming))
        record=self.workspace.latest_handoff()
        self.assertEqual((record.flow,record.logical_state,record.role,record.rung_index,record.profile_name),
                         ('feature','SPECIFY','specifier',0,wrapper.binding.profile_name))
        self.assertEqual(record.produced_artifacts,('microspec',))
        self.assertEqual(record.outcome,'ready')
        context=wrapper._context(InvocationRequest(WorkUnit(REQUEST),incoming))
        self.assertEqual(context.diagnostics,incoming)
        self.assertIsNone(context.previous_handoff)
        self.assertEqual(context.artifacts[0].content,REQUEST)

    def test_invalid_second_content_does_not_commit_first(self):
        wrapper=self.wrapper(result=StageResult('ready', (ProducedArtifact('implementation_handoff','valid'),
                                                         ProducedArtifact('microspec','invalid'))))
        wrapper=replace(wrapper,declaration=replace(wrapper.declaration,produces=('implementation_handoff','microspec')))
        with self.assertRaises(WorkContractError):
            wrapper.run(InvocationRequest(WorkUnit(REQUEST)))
        self.assertIsNone(self.workspace.latest_artifact('implementation_handoff'))
        self.assertIsNone(self.workspace.latest_artifact('microspec'))

    def test_catalog_mismatch(self):
        other=WorkWorkspace.open(self.root/'empty',{})
        with self.assertRaisesRegex(WorkContractError,'catalog differs'):
            workspace_resolver(self.bundle,'feature',other,lambda b: None)


if __name__ == '__main__': unittest.main()

"""V2-004E: Claude transport may omit nullable StageResult keys; the canonical contract stays strict."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from runtime import DiagnosticPayload, InvocationRequest, WorkUnit
from switchyard.context_transport import (StageInvocation, StageOutputContract, StageOutputArtifactContract,
                                          StageHandoffContract, StageHandoff, workspace_resolver)
from switchyard.provider_stages import (STRUCTURED_OUTPUT_ATTEMPTS, RESULT_KEYS, CLAUDE_TRANSPORT_REQUIRED, ProviderRuntimeConfig,
                                        ProviderStageExecutorResolver, normalize_claude_transport,
                                        parse_stage_result, result_schema)
from switchyard.provider_process import ProviderExecutionError
from switchyard.work_contract import WorkWorkspace
from switchyard.workflow_bundle import load_workflow_bundle, ExecutionProfile
from switchyard.workflow_compiler import compile_workflow, ExecutionBinding
from test_context_transport import SPEC

CANONICAL = Path(__file__).resolve().parents[1]/'examples/delivery/workflows-v2.yaml'
HANDOFF = {'summary':'tie order lost','findings':[],'evidence':[],'changed_assumptions':[],'blockers':[]}
CONTRACT = StageOutputContract(('ready','needs_clarification'), (StageOutputArtifactContract('bug_spec','yaml'),),
                               StageHandoffContract(True, ()))
BASE = {'outcome':'ready','produced_artifacts':[]}


def claude(value):
    return parse_stage_result(normalize_claude_transport(value))


class NormalizationTests(unittest.TestCase):
    def test_missing_nullable_keys_become_null(self):
        cases = [  # (transport input, canonical keys added)
            (dict(BASE), {'handoff':None,'diagnostics':None}),
            (dict(BASE, handoff=HANDOFF), {'diagnostics':None}),
            (dict(BASE, diagnostics='queue trace'), {'handoff':None}),
            (dict(BASE, handoff=None, diagnostics=None), {}),
        ]
        for value, added in cases:
            with self.subTest(value=value):
                original = copy.deepcopy(value)
                normalized = normalize_claude_transport(value)
                self.assertEqual(value, original)  # Pure: the transport object is not mutated.
                self.assertEqual(normalized, {**original, **added})
                self.assertEqual(set(normalized), set(RESULT_KEYS))
        self.assertEqual(claude(dict(BASE, handoff=HANDOFF)).handoff, StageHandoff(**HANDOFF))
        self.assertIsNone(claude(dict(BASE, handoff=HANDOFF)).diagnostics)
        self.assertEqual(claude(dict(BASE, diagnostics='queue trace')).diagnostics, DiagnosticPayload(b'queue trace'))
        self.assertIsNone(claude(dict(BASE, diagnostics='queue trace')).handoff)
        result = claude(dict(BASE))
        self.assertEqual((result.outcome, result.produced_artifacts, result.handoff, result.diagnostics),
                         ('ready', (), None, None))

    def test_non_nullable_invalid_and_extra_keys_still_fail(self):
        failing = {
            'missing outcome': {'produced_artifacts':[]},
            'missing produced_artifacts': {'outcome':'ready'},
            'extra key': dict(BASE, handoff=None, diagnostics=None, foo='bar'),
            'invalid handoff': dict(BASE, handoff=[], diagnostics=None),
            'invalid diagnostics': dict(BASE, handoff=None, diagnostics=7),
            'invalid handoff, diagnostics omitted': dict(BASE, handoff='summary'),
        }
        for name, value in failing.items():
            with self.subTest(name):
                normalized = normalize_claude_transport(value)
                for key in ('outcome','produced_artifacts','foo'):
                    self.assertEqual(key in normalized, key in value)  # Never synthesized or stripped.
                for key in ('handoff','diagnostics'):
                    if key in value: self.assertEqual(normalized[key], value[key])  # Never coerced.
                with self.assertRaises(ProviderExecutionError):
                    parse_stage_result(normalized)
        for value in (None, [], 'text'):
            self.assertIs(normalize_claude_transport(value), value)

    def test_generic_parser_remains_strict(self):
        for missing in ('handoff','diagnostics'):
            value = {**BASE, 'handoff':None, 'diagnostics':None}; del value[missing]
            with self.assertRaisesRegex(ProviderExecutionError, 'invalid structured object fields'):
                parse_stage_result(value)
        with self.assertRaisesRegex(ProviderExecutionError, 'invalid structured object fields'):
            parse_stage_result(dict(BASE))

    def test_transport_schemas(self):
        self.assertEqual(CLAUDE_TRANSPORT_REQUIRED, ('outcome','produced_artifacts'))
        for contract in (None, CONTRACT, StageOutputContract(('pass',), (), StageHandoffContract(False,(),supported=False))):
            with self.subTest(contract=contract):
                canonical, claude_schema = result_schema(contract), result_schema(contract, 'claude')
                self.assertEqual(canonical['required'], list(RESULT_KEYS))
                self.assertEqual(claude_schema['required'], ['outcome','produced_artifacts'])
                self.assertEqual(claude_schema['properties'], canonical['properties'])
                self.assertFalse(claude_schema['additionalProperties'])
                self.assertEqual({k:v for k,v in claude_schema.items() if k != 'required'},
                                 {k:v for k,v in canonical.items() if k != 'required'})
        with self.assertRaises(ValueError):
            result_schema(CONTRACT, 'codex')


@unittest.skipIf(os.name=='nt','executable fixture scripts require POSIX shebang execution')
class ClaudeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.bin = self.root/'bin'; self.bin.mkdir()
        script = (Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        for name in ('claude','codex','python3'):
            path = self.bin/name; path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        self.log = self.root/'calls.jsonl'; plan = self.root/'plan.json'
        plan.write_text(json.dumps({'artifacts':{}}))
        self.env = {'PATH':str(self.bin),'FAKE_LOG':str(self.log),'FAKE_PLAN':str(plan)}

    def scripted(self, provider, result, code=0, envelope=None):
        env = {**self.env, 'FAKE_MODE':'scripted', 'FAKE_EXIT':str(code)}
        if provider == 'claude':
            envelope = envelope or {'type':'result','subtype':'success','is_error':False,'result':'prose',
                                    'structured_output':result}
            (self.root/'stdout').write_text(json.dumps(envelope)); env['FAKE_STDOUT_FILE'] = str(self.root/'stdout')
        else:
            (self.root/'result').write_text(json.dumps(result)); env['FAKE_RESULT_FILE'] = str(self.root/'result')
        return ProviderRuntimeConfig(str(self.root), str(self.bin/'claude'), str(self.bin/'codex'), env, timeout_seconds=5)

    def stage(self, provider, config):
        model = 'haiku' if provider == 'claude' else 'codex'
        caps = ('repo_read',) if provider == 'claude' else ('repo_read','command')
        binding = ExecutionBinding('bugfix','TRIAGE','triage',0,'profile',provider,model,'medium',None,caps)
        stage = ProviderStageExecutorResolver({'profile':ExecutionProfile(provider,model,None)}, config)(binding)
        return stage, StageInvocation(binding, (), output_contract=CONTRACT)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_claude_accepts_omitted_nullable_keys_with_transport_schema(self):
        stage, invocation = self.stage('claude', self.scripted('claude', dict(BASE)))
        result = stage.run(invocation)
        self.assertEqual((result.handoff, result.diagnostics), (None, None))
        call = self.calls()[-1]
        schema = json.loads(call['argv'][call['argv'].index('--json-schema')+1])
        self.assertEqual(schema, result_schema(CONTRACT, 'claude'))
        self.assertEqual(call['retries'], str(STRUCTURED_OUTPUT_ATTEMPTS))
        # Only supported options: no internal structured-output feature gate is configured.
        self.assertFalse(any('tengu' in arg or 'structured_output_strict' in arg for arg in call['argv']))

    def test_claude_still_rejects_missing_required_and_invalid_values(self):
        for value in ({'produced_artifacts':[]}, {'outcome':'ready'},
                      dict(BASE, handoff=[]), dict(BASE, diagnostics=7), dict(BASE, foo='bar')):
            with self.subTest(value=value):
                stage, invocation = self.stage('claude', self.scripted('claude', value))
                with self.assertRaises(ProviderExecutionError):
                    stage.run(invocation)

    def test_codex_remains_strict(self):
        stage, invocation = self.stage('codex', self.scripted('codex', dict(BASE)))
        with self.assertRaisesRegex(ProviderExecutionError, 'invalid structured object fields'):
            stage.run(invocation)
        self.assertEqual(self.calls()[-1]['schema'], result_schema(CONTRACT))
        self.assertEqual(self.calls()[-1]['schema']['required'], list(RESULT_KEYS))

    def test_failed_claude_process_is_not_normalized_into_a_result(self):
        # Even a nonzero exit carrying a would-be-valid structured_output stays an execution failure.
        envelope = {'type':'result','subtype':'error_max_structured_output_retries','is_error':True,
                    'errors':['Output does not match required schema'],'structured_output':dict(BASE)}
        stage, invocation = self.stage('claude', self.scripted('claude', None, code=1, envelope=envelope))
        with self.assertRaises(ProviderExecutionError) as caught:
            stage.run(invocation)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertIn('Output does not match required schema', caught.exception.diagnostics.utf8.decode())

    def test_replay_part_a_attempt_3_triage_shape(self):
        # Historical attempt-3 TRIAGE (claude-haiku, haiku, medium, [repo_read]) returning a legal
        # outcome and a valid bug_spec MicroSpec, with handoff and diagnostics omitted.
        bundle = load_workflow_bundle(CANONICAL)
        # V2-001F promotes current TRIAGE; retain this replay's original Haiku binding.
        roles = dict(bundle.roles)
        roles['triager'] = replace(roles['triager'], primary=replace(roles['triager'].primary, profile='claude-haiku'))
        bundle = replace(bundle, roles=roles)
        request = 'Equal-priority tasks lose FIFO order after a pop.'
        workspace = WorkWorkspace.open(self.root/'work', bundle.artifacts)
        workspace.initialize_request(request)
        config = self.scripted('claude', {'outcome':'ready','produced_artifacts':[{'name':'bug_spec','content':SPEC}]})
        config = ProviderRuntimeConfig(config.working_directory, config.claude_executable, config.codex_executable,
                                       config.environment, timeout_seconds=5, model_aliases={'codex':{'codex':'local'}})
        compiled = compile_workflow(bundle, 'bugfix', workspace_resolver(bundle, 'bugfix', workspace,
                                    ProviderStageExecutorResolver(bundle.profiles, config)))
        state = next(s for s, p in compiled.plan.items() if p.binding and p.binding.logical_state == 'TRIAGE')
        binding = compiled.plan[state].binding
        self.assertEqual((binding.profile_name, binding.model, binding.effort, binding.capabilities),
                         ('claude-haiku','haiku','medium',('repo_read',)))
        response = compiled.executors[state].run(InvocationRequest(WorkUnit(request)))
        self.assertEqual(response.result.outcome, 'ready')
        self.assertIn('ready', compiled.executors[state].output_contract.allowed_outcomes)  # Legal TRIAGE outcome.
        self.assertEqual([r.version for r in workspace.versions('bug_spec')], [1])
        self.assertEqual(workspace.read_artifact('bug_spec'), SPEC)
        self.assertEqual(workspace.read_artifact('request'), request)
        self.assertEqual(workspace.handoffs(), ())
        self.assertEqual(len(self.calls()), 1)


if __name__ == '__main__':
    unittest.main()

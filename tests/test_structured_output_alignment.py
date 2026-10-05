"""V2-004D: provider instructions and the structured-output schema share one StageResult shape."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from runtime import DiagnosticPayload, InvocationRequest, WorkUnit
from switchyard.context_transport import (StageInvocation, StageOutputContract, StageOutputArtifactContract,
                                          StageHandoffContract, StageHandoff, workspace_resolver)
from switchyard.provider_stages import (RESULT_FIELDS, RESULT_KEYS, ProviderRuntimeConfig,
                                        ProviderStageExecutorResolver, render_invocation, result_schema)
from switchyard.provider_process import ProviderExecutionError
from switchyard.work_contract import WorkWorkspace
from switchyard.workflow_bundle import load_workflow_bundle, ExecutionProfile
from switchyard.workflow_compiler import compile_workflow, ExecutionBinding

CANONICAL = Path(__file__).resolve().parents[1]/'examples/delivery/workflows-v2.yaml'
HANDOFF = {'summary':'queue reasoning','findings':['heap ties'],'evidence':['bug_spec'],
           'changed_assumptions':[],'blockers':[]}
CONTRACTS = (None,
             StageOutputContract(('ready','needs_clarification'),
                                 (StageOutputArtifactContract('bug_spec','yaml'),), StageHandoffContract(True,())),
             StageOutputContract(('pass',), (), StageHandoffContract(False,(),supported=False)))


def allows_null(schema):
    types = schema.get('type')
    return (types == 'null' or (type(types) is list and 'null' in types)
            or any(option.get('type') == 'null' for option in schema.get('anyOf', ())))


def allows_empty_list(schema):
    return schema.get('type') == 'array' and schema.get('minItems', 0) == 0


class SchemaInstructionAlignmentTests(unittest.TestCase):
    def test_one_contract_drives_schema_parser_and_rendered_instructions(self):
        self.assertEqual(RESULT_KEYS, ('outcome','produced_artifacts','handoff','diagnostics'))
        for contract in CONTRACTS:
            with self.subTest(contract=contract):
                schema = result_schema(contract)
                self.assertEqual(schema['required'], list(RESULT_KEYS))
                self.assertEqual(list(schema['properties']), list(RESULT_KEYS))
                self.assertFalse(schema['additionalProperties'])
                for field in RESULT_FIELDS:
                    self.assertEqual(allows_null(schema['properties'][field.name]), field.nullable, field.name)
                self.assertTrue(allows_empty_list(schema['properties']['produced_artifacts']))
                if contract is None:
                    continue
                binding = ExecutionBinding('f','S','worker',0,'p','claude','haiku','medium',None,('repo_read',))
                rendered = render_invocation(StageInvocation(binding, (), output_contract=contract))
                section = rendered.split('\n\nOUTPUT CONTRACT\n')[1]
                self.assertIn('ALL of these top-level keys. Never omit any key.', section)
                self.assertIn('"Nullable" means the KEY is mandatory and its VALUE may be null.', section)
                for index, field in enumerate(RESULT_FIELDS, 1):
                    line = next(l for l in section.splitlines() if l.startswith(f'{index}. {field.name}: '))
                    self.assertIn('KEY required', line)
                    self.assertIn('VALUE may be null' if field.nullable else 'VALUE is never null', line)
                    if field.empty:
                        self.assertIn(f'use {field.empty} when not applicable', line)
                self.assertEqual(json.loads(section.splitlines()[-1]), schema)
                self.assertNotIn('optional', rendered.lower())

    def test_canonical_triage_input_states_mandatory_keys(self):
        # Current canonical BUGFIX TRIAGE retains the failure-class guidance after V2-001F.
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('claude','python3'):  # Never launched: compilation only resolves executables.
                executable = root/name; executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o755)
            bundle = load_workflow_bundle(CANONICAL)
            workspace = WorkWorkspace.open(root/'work', bundle.artifacts)
            request = 'Equal-priority tasks lose FIFO order.'
            workspace.initialize_request(request)
            config = ProviderRuntimeConfig(str(root), str(executable), str(executable),
                                           {'PATH':str(root)}, model_aliases={'codex':{'codex':'local'}})
            resolver = workspace_resolver(bundle, 'bugfix', workspace,
                                          ProviderStageExecutorResolver(bundle.profiles, config))
            compiled = compile_workflow(bundle, 'bugfix', resolver)
            state = next(s for s, p in compiled.plan.items() if p.binding and p.binding.logical_state == 'TRIAGE')
            invocation = compiled.executors[state]._context(InvocationRequest(WorkUnit(request)))
        binding = invocation.binding
        self.assertEqual((binding.provider, binding.profile_name, binding.model, binding.effort, binding.capabilities),
                         ('claude','claude-sonnet','sonnet','medium',('repo_read',)))
        rendered = render_invocation(invocation)
        for text in ('Never omit any key.',
                     '2. produced_artifacts: KEY required; its VALUE is never null; use [] when not applicable.',
                     '3. handoff: KEY required; its VALUE may be null; use null when not applicable.',
                     '4. diagnostics: KEY required; its VALUE may be null; use null when not applicable.',
                     'never invent diagnostics to avoid null',
                     'The handoff KEY is always present; its VALUE may be null.'):
            self.assertIn(text, rendered)
        self.assertNotIn('remains optional', rendered)
        self.assertEqual(result_schema(invocation.output_contract)['required'], list(RESULT_KEYS))


@unittest.skipIf(os.name=='nt','executable fixture scripts require POSIX shebang execution')
class FakeProviderShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.bin = self.root/'bin'; self.bin.mkdir()
        script = (Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        for name in ('claude','codex'):
            path = self.bin/name; path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        self.log = self.root/'calls.jsonl'; plan = self.root/'plan.json'
        plan.write_text(json.dumps({'artifacts':{}}))
        self.env = {'PATH':str(self.bin),'FAKE_LOG':str(self.log),'FAKE_PLAN':str(plan)}

    def run_provider(self, provider, result, code=0, stdout=None):
        """One fake process; Claude returns result in its envelope, Codex in its result file."""
        env = {**self.env, 'FAKE_MODE':'scripted', 'FAKE_EXIT':str(code)}
        payload = json.dumps(result)
        if provider == 'claude':
            stdout = stdout or json.dumps({'type':'result','subtype':'success','is_error':False,
                                           'result':'prose','structured_output':result})
        else:
            (self.root/'result').write_text(payload); env['FAKE_RESULT_FILE'] = str(self.root/'result')
        if stdout:
            (self.root/'stdout').write_text(stdout); env['FAKE_STDOUT_FILE'] = str(self.root/'stdout')
        config = ProviderRuntimeConfig(str(self.root), str(self.bin/'claude'), str(self.bin/'codex'), env, timeout_seconds=5)
        model = 'haiku' if provider == 'claude' else 'codex'
        binding = ExecutionBinding('bugfix','TRIAGE','triage',0,'profile',provider,model,'medium',None,
                                   ('repo_read',) if provider == 'claude' else ('repo_read','command'))
        stage = ProviderStageExecutorResolver({'profile':ExecutionProfile(provider,model,None)}, config)(binding)
        return stage.run(StageInvocation(binding, (), output_contract=CONTRACTS[1]))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_omitted_nullable_keys_remain_rejected(self):
        # Canonical strictness is not loosened. Claude's transport-only normalization (V2-004E)
        # is covered in test_claude_transport_normalization; Codex has none.
        with self.assertRaisesRegex(ProviderExecutionError, 'invalid structured object fields'):
            self.run_provider('codex', {'outcome':'ready','produced_artifacts':[]})

    def test_null_and_empty_absence_forms_are_valid(self):
        for provider in ('claude','codex'):
            with self.subTest(provider=provider):
                result = self.run_provider(provider, {'outcome':'ready','produced_artifacts':[],
                                                      'handoff':None,'diagnostics':None})
                self.assertEqual((result.outcome, result.produced_artifacts, result.handoff, result.diagnostics),
                                 ('ready', (), None, None))

    def test_handoff_and_diagnostics_values_still_parse(self):
        for provider in ('claude','codex'):
            with self.subTest(provider=provider):
                result = self.run_provider(provider, {'outcome':'ready',
                    'produced_artifacts':[{'name':'bug_spec','content':'id: BUG-1'}],
                    'handoff':HANDOFF,'diagnostics':'tie order lost after pop'})
                self.assertEqual(result.produced_artifacts[0].name, 'bug_spec')
                self.assertEqual(result.handoff, StageHandoff(**HANDOFF))
                self.assertEqual(result.diagnostics, DiagnosticPayload(b'tie order lost after pop'))

    def test_shared_renderer_and_unchanged_retry_policy(self):
        valid = {'outcome':'ready','produced_artifacts':[],'handoff':None,'diagnostics':None}
        self.run_provider('claude', valid); self.run_provider('codex', valid)
        claude, codex = self.calls()
        contract = lambda call: call['input'].split('\n\nSTAGE OUTPUT CONTRACT\n')[1]
        self.assertEqual(contract(claude), contract(codex))
        self.assertEqual(claude['retries'], '1')
        schema = json.loads(claude['argv'][claude['argv'].index('--json-schema')+1])
        self.assertEqual(codex['schema'], result_schema(CONTRACTS[1]))
        self.assertEqual(codex['schema']['required'], list(RESULT_KEYS))
        # V2-004E: identical properties; only Claude's transport required list differs.
        self.assertEqual(schema, result_schema(CONTRACTS[1], 'claude'))
        self.assertEqual(schema['properties'], codex['schema']['properties'])

    def test_provider_schema_failure_remains_execution_failure(self):
        # Live attempt #2 envelope: the CLI rejects the shape and exits 1 (V2-004C unchanged).
        stdout = json.dumps({'type':'result','subtype':'error_max_structured_output_retries','is_error':True,
                             'terminal_reason':'structured_output_retry_exhausted',
                             'errors':["Output does not match required schema: root: must have required property 'handoff'"],
                             'permission_denials':[{'tool_name':'Read','tool_use_id':'t','tool_input':{'file_path':'/x'}}]})
        with self.assertRaises(ProviderExecutionError) as caught:
            self.run_provider('claude', None, code=1, stdout=stdout)
        self.assertEqual(caught.exception.returncode, 1)
        text = caught.exception.diagnostics.utf8.decode()
        self.assertIn("must have required property 'handoff'", text)
        self.assertIn('permission_denials: Read', text); self.assertNotIn('/x', text)


if __name__ == '__main__':
    unittest.main()

"""STRUCTURED-OUTPUT-001: one StageOutputContract drives prompt, validation and bounded correction.

Offline fake CLIs only. The Claude fake emulates the CLI's in-process StructuredOutput repair.
"""
from dataclasses import replace
import json
import unittest

from switchyard.context_transport import (StageInvocation, StageOutputContract, StageOutputArtifactContract,
                                          StageHandoffContract)
from switchyard.provider_process import ProviderExecutionError
from switchyard.provider_stages import (RESULT_FIELDS, STRUCTURED_OUTPUT_ATTEMPTS, ProviderStageExecutorResolver,
                                        StageResultContractError, parse_stage_result,
                                        render_invocation, result_field_map, result_schema)
from switchyard.recovery import recovery_status
from switchyard.workflow_bundle import ExecutionProfile
from switchyard.workflow_compiler import ExecutionBinding

import test_provider_recovery

# Verbatim envelope text of the interrupted Adeo IMPLEMENT (work 52c939b8..., Claude 2.1.282).
OBSERVED = ("Failed to provide valid structured output after 1 attempts — last StructuredOutput error: "
            "Output does not match required schema: /produced_artifacts/0: must NOT have additional "
            "properties ('content_name' is not allowed)")
EXPECTED_ERROR = {'path':'produced_artifacts[0].content_name', 'error':'additional property is not allowed',
                  'allowed_fields':['name','content']}
IMPLEMENT = StageOutputContract(('ready','blocked'), (StageOutputArtifactContract('implementation_handoff','markdown'),),
                                StageHandoffContract(True, ('implementation_handoff','microspec','request')))
CONTRACTS = (IMPLEMENT, StageOutputContract(('pass',), (), StageHandoffContract(False, (), supported=False)))


def valid(**changes):
    return {'outcome':'ready', 'produced_artifacts':[{'name':'implementation_handoff','content':'done'}],
            'handoff':None, 'diagnostics':None, **changes}


def errors(exc):
    text = exc.diagnostics.utf8.decode()
    line = next(l for l in text.splitlines() if l.startswith('stage_result_errors: '))
    return json.loads(line.removeprefix('stage_result_errors: '))


class ContractAgreementTests(unittest.TestCase):
    # Test 1
    def test_prompt_field_map_is_the_schema(self):
        binding = ExecutionBinding('feature','IMPLEMENT','code',0,'code','claude','sonnet','medium',None,('repo_read',))
        for contract in CONTRACTS:
            with self.subTest(contract=contract):
                schema = result_schema(contract)
                rendered = render_invocation(StageInvocation(binding, (), output_contract=contract))
                section = rendered.split('\n\nSTRUCTURED OUTPUT FIELDS\n')[1].splitlines()
                shown = json.loads(section[1])
                self.assertEqual(shown, result_field_map(schema))
                self.assertEqual(shown['$'], list(schema['properties']))
                items = schema['properties']['produced_artifacts']['items']
                self.assertEqual(shown['produced_artifacts[]'], list(items['properties']))
                self.assertFalse(items['additionalProperties'])
                if contract.handoff.supported:
                    handoff = schema['properties']['handoff']['anyOf'][0]
                    self.assertEqual(shown['handoff'], list(handoff['properties']))
                else:
                    self.assertNotIn('handoff', shown)  # Schema admits only null.
                rule = next(f.rule for f in RESULT_FIELDS if f.name == 'produced_artifacts')
                self.assertIn('exactly the fields '+' and '.join(shown['produced_artifacts[]']), rule)
                self.assertNotIn('content_name', rendered)  # Nothing the contract does not allow.
        # Transport schemas differ only in required keys, never in allowed fields.
        self.assertEqual(result_field_map(result_schema(IMPLEMENT, 'claude')), result_field_map(result_schema(IMPLEMENT)))


class StageResultErrorTests(unittest.TestCase):
    # Test 2
    def test_unexpected_nested_field_is_rejected_with_exact_path(self):
        bad = valid(produced_artifacts=[{'name':'implementation_handoff','content':'done','content_name':'h.md'}])
        with self.assertRaises(StageResultContractError) as caught:
            parse_stage_result(bad)
        self.assertIsInstance(caught.exception, ProviderExecutionError)
        self.assertIsNone(caught.exception.returncode)
        self.assertEqual(list(caught.exception.errors), [EXPECTED_ERROR])
        self.assertEqual(errors(caught.exception), [EXPECTED_ERROR])
        self.assertEqual(str(caught.exception),
                         'invalid structured object fields: produced_artifacts[0].content_name: additional property is not allowed')

    def test_error_records_are_bounded_and_never_echo_values(self):
        secret = 'VALUE-'+'v'*5000
        bad = valid(produced_artifacts=[{'name':'implementation_handoff','content':secret, **{f'k{i}':secret for i in range(40)}}],
                    extra=secret)
        with self.assertRaises(StageResultContractError) as caught:
            parse_stage_result(bad)
        records = errors(caught.exception)
        self.assertEqual(records[0], {'path':'extra','error':'additional property is not allowed',
                                      'allowed_fields':['outcome','produced_artifacts','handoff','diagnostics']})
        self.assertLessEqual(len(records), 8)
        self.assertNotIn('VALUE-', caught.exception.diagnostics.utf8.decode())
        missing = valid(); del missing['produced_artifacts']
        with self.assertRaises(StageResultContractError) as caught:
            parse_stage_result(missing)
        self.assertEqual(errors(caught.exception), [{'path':'produced_artifacts','error':'required property is missing',
            'allowed_fields':['outcome','produced_artifacts','handoff','diagnostics']}])
        self.assertEqual(parse_stage_result(valid()).produced_artifacts[0].name, 'implementation_handoff')


class ClaudeCorrectionTests(unittest.TestCase):
    _base = test_provider_recovery.ProviderRecoveryTests
    setUp, git, save_config, start, interrupt, resume, provider, operations, events = (
        _base.setUp, _base.git, _base.save_config, _base.start, _base.interrupt, _base.resume,
        _base.provider, _base.operations, _base.events)

    def environment(self, invalid):
        env = {**self.config.environment, 'FAKE_REPAIR_LOG':str(self.root/'repair')}
        env.pop('FAKE_SCHEMA_INVALID_ATTEMPTS', None)
        if invalid:
            env['FAKE_SCHEMA_INVALID_ATTEMPTS'] = str(invalid)
        self.config = replace(self.config, environment=env)

    def repairs(self):
        return [json.loads(line) for line in (self.root/'repair').read_text().splitlines()]

    def stage(self):
        binding = ExecutionBinding('feature','CODE','code',0,'code','claude','sonnet','medium',None,
                                   ('repo_read','repo_write','command'))
        stage = ProviderStageExecutorResolver({'code':ExecutionProfile('claude','sonnet')}, self.config)(binding)
        return stage, StageInvocation(binding, (), output_contract=IMPLEMENT)

    # Tests 3 and 4
    def test_corrected_result_succeeds_inside_one_invocation(self):
        self.environment(invalid=1)
        stage, invocation = self.stage()
        result = stage.run(invocation)
        self.assertEqual([a.name for a in result.produced_artifacts], ['implementation_handoff'])
        self.assertEqual(self.repairs(), [{'attempts':2, 'feedback':[OBSERVED.split('error: ',1)[1]]}])
        call, = [json.loads(l) for l in self.log.read_text().splitlines()]  # One provider process.
        self.assertEqual(call['retries'], str(STRUCTURED_OUTPUT_ATTEMPTS))
        # The repair conversation carries the exact path (CLI feedback) and the allowed fields (prompt).
        fields = json.loads(call['input'].split('\n\nSTRUCTURED OUTPUT FIELDS\n')[1].splitlines()[1])
        self.assertEqual(fields['produced_artifacts[]'], EXPECTED_ERROR['allowed_fields'])
        self.assertIn('remove every field not listed for its object', call['input'])

    # Test 3: exhausted repair surfaces the same machine-readable record
    def test_exhausted_correction_reports_path_and_allowed_fields(self):
        self.environment(invalid=99)
        stage, invocation = self.stage()
        with self.assertRaises(ProviderExecutionError) as caught:
            stage.run(invocation)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertEqual(errors(caught.exception), [EXPECTED_ERROR])
        self.assertEqual(self.repairs()[0]['attempts'], STRUCTURED_OUTPUT_ATTEMPTS)  # Bounded.

    # Tests 5, 6 and 7
    def test_repeated_invalid_result_interrupts_without_semantic_charge(self):
        self.provider('time.sleep(3)', 'pass')  # IMPLEMENT finishes its edits, then fails the contract.
        self.environment(invalid=99)
        self.interrupt()
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['state'], status['reason']), ('INTERRUPTED','IMPLEMENT','provider_failure'))
        self.assertEqual(status['provider_failure']['classification'], 'invalid_stage_result')
        self.assertIn('"path":"produced_artifacts[0].content_name"', status['provider_failure']['output_tail'])
        self.assertIs(status['semantic_invocation_charged'], False)
        self.assertEqual((status['semantic_invocations_used'], status['infrastructure_failures_used']), (0, 1))
        self.assertEqual((status['resume_allowed'], status['resume_to']), (True, 'IMPLEMENT'))
        self.assertIs(self.operations()[-1]['semantic_invocation'], False)
        self.assertEqual(self.repairs()[0]['attempts'], STRUCTURED_OUTPUT_ATTEMPTS)
        self.assertEqual(self.file.read_text(), 'partial')  # Implementation changes retained.
        # Explicit resume only; it reuses the partial work in place and finishes.
        before = self.events()
        self.environment(invalid=1)
        execution = self.resume()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.events()[:len(before)], before)
        self.assertEqual(len({e.run_id for e in self.events()}), 1)
        self.assertEqual(self.file.read_text(), 'finished')

    def test_codex_contract_violation_is_invalid_stage_result(self):
        # Switchyard's own parser rejects the field (Codex has no in-process repair channel).
        for name in ('claude', 'codex'):
            exe = self.root/name
            exe.write_text(exe.read_text().replace('time.sleep(3)', 'pass', 1).replace(
                'raw=json.dumps(result)',
                "if state=='IMPLEMENT': result['produced_artifacts'][0]['content_name']='x'\nraw=json.dumps(result)", 1))
        self.data['agents']['code'] = dict(provider='codex', model='offline', workspace='write', shell=True, timeout=5)
        self.text = self.save_config()
        self.interrupt()
        status = recovery_status(self.work)
        self.assertEqual(status['provider_failure']['classification'], 'invalid_stage_result')
        self.assertIs(status['semantic_invocation_charged'], False)
        self.assertEqual((status['resume_allowed'], status['resume_to']), (True, 'IMPLEMENT'))
        self.assertIn('"path":"produced_artifacts[0].content_name"', status['provider_failure']['output_tail'])


if __name__ == '__main__':
    unittest.main()

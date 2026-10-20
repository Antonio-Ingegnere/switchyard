"""PROVIDER-MODELS-001: one audited Claude model effort policy for validation and execution."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from flow_config import FlowConfigError, _yaml_data
from switchyard.claude_models import (CLAUDE_MODELS, CLAUDE_POLICY_ENVIRONMENT, ClaudeEffort,
                                      ClaudeEffortError, claude_effort_controls)
from switchyard.context_transport import StageInvocation, StageOutputContract, StageHandoffContract
from switchyard.provider_stages import (ProviderRuntimeConfig, ProviderStageExecutorResolver,
                                        ProviderResolutionError, validate_provider_configuration)
from switchyard.simple_config import simple_config_from_text
from switchyard.workflow_bundle import ExecutionProfile
from switchyard.workflow_compiler import ExecutionBinding

ROOT = Path(__file__).resolve().parents[1]
EFFORTS = ('low', 'medium', 'high')
TOKENS = {'low': '1024', 'medium': '8192', 'high': '16384'}

# Audited selectors and their expected mechanism (CLI 2.1.299 evidence).
AUDITED = [
    ('haiku', ClaudeEffort.NATIVE), ('sonnet', ClaudeEffort.NATIVE),
    ('opus', ClaudeEffort.NATIVE), ('fable', ClaudeEffort.NATIVE),
    ('claude-haiku-5-5', ClaudeEffort.NATIVE),
    ('claude-sonnet-4-6', ClaudeEffort.NATIVE), ('claude-sonnet-5', ClaudeEffort.NATIVE),
    ('claude-sonnet-5-5', ClaudeEffort.NATIVE),
    ('claude-opus-4-6', ClaudeEffort.NATIVE), ('claude-opus-4-7', ClaudeEffort.NATIVE),
    ('claude-opus-4-8', ClaudeEffort.NATIVE), ('claude-opus-5', ClaudeEffort.NATIVE),
    ('claude-opus-5-5', ClaudeEffort.NATIVE),
    ('claude-fable-5', ClaudeEffort.NATIVE), ('claude-fable-5-1', ClaudeEffort.NATIVE),
    ('claude-haiku-4-5', ClaudeEffort.THINKING_TOKENS),
    ('claude-haiku-4-5-20251001', ClaudeEffort.THINKING_TOKENS),
    ('claude-sonnet-4-5', ClaudeEffort.NONE), ('claude-sonnet-4-5-20250929', ClaudeEffort.NONE),
]
# Lookalikes must not gain a translation by prefix, family or case guessing.
UNKNOWN = ['new-model', 'claude-opus-9', 'claude-opus-5-5-preview', 'opus-latest', 'OPUS', 'Opus',
           'opus[1m]', 'claude-haiku-4', 'claude-sonnet', 'claude-3-5-haiku', 'default']


def binding(model, effort, provider='claude'):
    return ExecutionBinding('test', 'A', 'worker', 0, 'profile', provider, model, effort, None, None)


def agent_config(model, effort=None):
    agent = {'provider': 'claude', 'model': model, 'workspace': 'read', 'shell': True, 'timeout': 600}
    if effort is not None:
        agent['effort'] = effort
    return json.dumps({'agents': {'diagnose-deep': agent},
                       'flows': {'f': {'start': 'S', 'states': {
                           'S': {'agent': 'diagnose-deep', 'consumes': ['request'],
                                 'produces': {'note': {'format': 'markdown'}}, 'on': {'ready': 'DONE'}},
                           'DONE': {'terminal': True}, 'NEEDS_HUMAN': {'terminal': True}}}}})


class ClaudeModelPolicyTests(unittest.TestCase):
    def test_table_is_exactly_the_audited_set(self):
        self.assertEqual(dict(CLAUDE_MODELS), dict(AUDITED))
        with self.assertRaises(TypeError):
            CLAUDE_MODELS['opus'] = ClaudeEffort.NONE
        self.assertEqual({m.value for m in ClaudeEffort},
                         {'native_effort', 'thinking_token_budget', 'no_audited_effort'})

    def test_controls_per_mechanism(self):
        for model, mechanism in AUDITED:
            for effort in EFFORTS:
                with self.subTest(model=model, effort=effort):
                    if mechanism is ClaudeEffort.NATIVE:
                        self.assertEqual(claude_effort_controls(model, effort), (('--effort', effort), {}))
                    elif mechanism is ClaudeEffort.THINKING_TOKENS:
                        self.assertEqual(claude_effort_controls(model, effort),
                                         ((), {'MAX_THINKING_TOKENS': TOKENS[effort],
                                               'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '32768'}))
                    else:
                        with self.assertRaisesRegex(ClaudeEffortError, 'no effort mechanism'):
                            claude_effort_controls(model, effort)

    def test_no_effort_adds_no_controls_for_any_model(self):
        for model in [m for m, _ in AUDITED] + UNKNOWN:
            with self.subTest(model=model):
                self.assertEqual(claude_effort_controls(model, None), ((), {}))
                validate_provider_configuration(binding(model, None))

    def test_unknown_model_with_effort_fails_closed(self):
        for model in UNKNOWN:
            for effort in EFFORTS:
                with self.subTest(model=model, effort=effort):
                    with self.assertRaisesRegex(ProviderResolutionError,
                            'Claude effort translation is not audited for resolved model '+repr(model).replace('[', r'\[')):
                        validate_provider_configuration(binding(model, effort))

    def test_resolved_model_governs_validation(self):
        # The selector spelling is irrelevant; the concrete resolved model decides.
        validate_provider_configuration(binding('anything', 'high'), 'opus')
        validate_provider_configuration(binding('anything', 'high'), 'claude-haiku-4-5')
        for resolved in ('new-model', 'claude-sonnet-4-5'):
            with self.subTest(resolved=resolved), self.assertRaises(ProviderResolutionError):
                validate_provider_configuration(binding('opus', 'high'), resolved)

    def test_policy_is_claude_only(self):
        # Codex effort remains its own adapter translation; Claude IDs mean nothing there.
        validate_provider_configuration(binding('new-model', 'high', 'codex'))


class SimpleConfigClaudeEffortTests(unittest.TestCase):
    def test_issue_configuration_validates(self):
        for model in ('opus', 'sonnet', 'haiku', 'claude-opus-5-5', 'claude-haiku-4-5'):
            for effort in EFFORTS:
                with self.subTest(model=model, effort=effort):
                    config = simple_config_from_text(agent_config(model, effort))
                    agent = config.agents['diagnose-deep']
                    self.assertEqual((agent.model, agent.effort), (model, effort))

    def test_unknown_or_unaudited_effort_rejected_at_config_location(self):
        for model in ('new-model', 'claude-opus-9', 'claude-sonnet-4-5'):
            with self.subTest(model=model), \
                    self.assertRaisesRegex(FlowConfigError, 'agents.diagnose-deep.*not audited'):
                simple_config_from_text(agent_config(model, 'medium'))

    def test_unknown_model_without_effort_still_accepted(self):
        config = simple_config_from_text(agent_config('new-model'))
        self.assertEqual(config.agents['diagnose-deep'].model, 'new-model')

    def test_shipped_example_still_validates(self):
        data = _yaml_data((ROOT/'examples/simple/config.yaml').read_text())
        simple_config_from_text(json.dumps(data))


@unittest.skipIf(os.name == 'nt', 'executable fixture scripts require POSIX shebang execution')
class ClaudeExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.bin = self.root/'bin'; self.bin.mkdir()
        script = (Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        for name in ('claude', 'codex'):
            path = self.bin/name; path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        self.log = self.root/'calls.jsonl'; plan = self.root/'plan.json'
        plan.write_text(json.dumps({'artifacts': {'A': []}}))
        self.env = {'PATH': str(self.bin), 'FAKE_LOG': str(self.log), 'FAKE_PLAN': str(plan)}
        self.config = ProviderRuntimeConfig(str(self.root), str(self.bin/'claude'), str(self.bin/'codex'),
                                            self.env, timeout_seconds=5)

    def run_claude(self, selector, effort, config=None):
        b = binding(selector, effort)
        stage = ProviderStageExecutorResolver({'profile': ExecutionProfile('claude', selector)},
                                              config or self.config)(b)
        stage.run(StageInvocation(b, (), output_contract=StageOutputContract(
            ('ready',), (), StageHandoffContract(True, ()))))
        return stage, [json.loads(line) for line in self.log.read_text().splitlines()][-1]

    def test_validation_and_execution_use_the_same_mapping(self):
        for model, mechanism in AUDITED:
            for effort in (None,) + EFFORTS:
                with self.subTest(model=model, effort=effort):
                    try:
                        argv, env = claude_effort_controls(model, effort)
                    except ClaudeEffortError:
                        with self.assertRaises(ProviderResolutionError):
                            validate_provider_configuration(binding(model, effort))
                        with self.assertRaises(ProviderResolutionError):
                            self.run_claude(model, effort)
                        continue
                    validate_provider_configuration(binding(model, effort))
                    _, call = self.run_claude(model, effort)
                    if argv:
                        i = call['argv'].index('--effort')
                        self.assertEqual(tuple(call['argv'][i:i+2]), argv)
                    self.assertEqual(call['argv'].count('--effort'), len(argv) // 2)
                    for key in ('MAX_THINKING_TOKENS', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS'):
                        self.assertEqual(call['controls'].get(key), env.get(key))

    def test_alias_and_explicit_id_behave_identically(self):
        for alias, explicit in (('opus', 'claude-opus-5-5'), ('sonnet', 'claude-sonnet-5-5'),
                                ('haiku', 'claude-haiku-5-5'), ('fable', 'claude-fable-5-1')):
            with self.subTest(alias=alias):
                _, a = self.run_claude(alias, 'high')
                _, b = self.run_claude(explicit, 'high')
                strip = lambda argv: [x for x in argv if x not in (alias, explicit)]
                self.assertEqual(strip(a['argv']), strip(b['argv']))
                self.assertEqual(a['controls'], b['controls'])

    def test_unknown_model_without_effort_executes_without_controls(self):
        stage, call = self.run_claude('new-model', None)
        self.assertEqual(stage.resolved_model, 'new-model')
        self.assertNotIn('--effort', call['argv'])
        self.assertNotIn('MAX_THINKING_TOKENS', call['controls'])

    def test_unknown_model_with_effort_fails_before_launch(self):
        with self.assertRaisesRegex(ProviderResolutionError, 'not audited'):
            self.run_claude('new-model', 'high')
        with self.assertRaisesRegex(ProviderResolutionError, 'not audited'):
            self.run_claude('opus', 'high', replace(self.config, model_aliases={'claude': {'opus': 'new-model'}}))
        self.assertFalse(self.log.exists())

    def test_inherited_policy_environment_is_stripped(self):
        inherited = {key: 'inherited' for key in CLAUDE_POLICY_ENVIRONMENT}
        for key in ('CLAUDE_CODE_EFFORT_LEVEL', 'MAX_THINKING_TOKENS', 'CLAUDE_CODE_DISABLE_THINKING',
                    'CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
                    'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                    'ANTHROPIC_DEFAULT_FABLE_MODEL', 'CLAUDE_CODE_MODEL_CAPABILITIES'):
            self.assertIn(key, inherited)
        config = replace(self.config, environment={**self.env, **inherited, 'ANTHROPIC_UNRELATED': 'kept'})
        for model, effort in (('opus', 'high'), ('claude-haiku-4-5', 'low'), ('new-model', None)):
            with self.subTest(model=model, effort=effort):
                _, call = self.run_claude(model, effort, config)
                _, env = claude_effort_controls(model, effort)
                for key in CLAUDE_POLICY_ENVIRONMENT:
                    self.assertEqual(call['controls'].get(key), env.get(key), key)
                self.assertEqual(call['controls']['ANTHROPIC_UNRELATED'], 'kept')

    def test_no_fallback_or_substitution(self):
        for model, mechanism in AUDITED:
            effort = None if mechanism is ClaudeEffort.NONE else 'medium'
            with self.subTest(model=model):
                stage, call = self.run_claude(model, effort)
                self.assertEqual(stage.resolved_model, model)
                argv = call['argv']
                self.assertEqual(argv.count('--model'), 1)
                self.assertEqual(argv[argv.index('--model')+1], model)
                self.assertNotIn('--fallback-model', argv)
                settings = json.loads(argv[argv.index('--settings')+1])
                self.assertEqual((settings['fallbackModel'], settings['switchModelsOnFlag']), ([], False))
        self.assertEqual(len(self.log.read_text().splitlines()), len(AUDITED))


if __name__ == '__main__':
    unittest.main()

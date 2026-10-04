"""V2-003D: precise, content-free MicroSpec diagnostics and validator-derived provider guidance."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from flow_config import _yaml_data
from runtime import InvocationRequest, WorkUnit
from switchyard.context_transport import StageResult, ProducedArtifact, StageHandoff, workspace_resolver
from switchyard.provider_stages import ProviderRuntimeConfig, ProviderStageExecutorResolver, render_invocation
from switchyard.work_contract import (MICROSPEC_FIELDS, WorkContractError, WorkWorkspace,
                                      microspec_guidance, validate_microspec)
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow

CANONICAL = Path(__file__).resolve().parents[1]/'examples/delivery/workflows-v2.yaml'
SECRET = 'SECRET-CONTENT-7f3a'
STRINGS = [f.name for f in MICROSPEC_FIELDS if f.type == 'string']
LISTS = [f.name for f in MICROSPEC_FIELDS if f.type != 'string']


def spec(**changes):
    value = {'id':'BUG-1','goal':'Keep FIFO','scope':['task_queue.py'],'constraints':[],
             'acceptance':['tests pass'],'verification':['python3 -m unittest'],'non_goals':[]}
    value.update(changes)
    return value


class Rejection:
    """Capture a WorkContractError and the text of its whole exception chain."""
    def __enter__(self):
        return self
    def __exit__(self, kind, exc, tb):
        if kind is None:
            raise AssertionError('expected WorkContractError')
        if not issubclass(kind, WorkContractError):
            return False
        self.text = str(exc)
        chain = exc
        while chain is not None:  # The rejected value must not appear anywhere in the chain.
            self.text += ' | ' + str(chain)
            chain = chain.__cause__ or chain.__context__
        self.first = str(exc)
        return True


def message(content, format='yaml'):
    with Rejection() as caught:
        validate_microspec(content, format)
    return caught


class MicroSpecDiagnosticTests(unittest.TestCase):
    def check(self, value, expected, format='yaml'):
        content = yaml.safe_dump(value) if format == 'yaml' else json.dumps(value)
        caught = message(content, format)
        self.assertEqual(caught.first, expected)
        self.assertNotIn(SECRET, caught.text)

    def test_every_string_field_reports_field_and_parsed_type(self):
        cases = [(None,'null'), (5,'integer'), (1.5,'number'), (True,'boolean'), ({SECRET:SECRET},'mapping'),
                 ([SECRET],'sequence'), ('','empty string'), ('   ','empty string')]
        for field in STRINGS:
            for value, kind in cases:
                for format in ('yaml','json'):
                    with self.subTest(field=field, kind=kind, format=format):
                        self.check(spec(**{field:value}), f'MicroSpec {field}: expected non-empty string, got {kind}', format)

    def test_every_list_field_reports_container_and_item_index(self):
        for field in LISTS:
            for value, kind in ((SECRET,'string'), ({SECRET:1},'mapping'), (None,'null'), (3,'integer')):
                with self.subTest(field=field, container=kind):
                    self.check(spec(**{field:value}),
                               f'MicroSpec {field}: expected sequence of non-empty strings, got {kind}')
            items = [(None,'null'), (7,'integer'), (2.5,'number'), (False,'boolean'),
                     ({SECRET:SECRET},'mapping'), ([SECRET],'sequence'), ('','empty string'), (' \t','empty string')]
            for value, kind in items:
                with self.subTest(field=field, item=kind):
                    self.check(spec(**{field:['valid item', value]}),
                               f'MicroSpec {field}[1]: expected non-empty string, got {kind}')

    def test_root_missing_and_timestamp_diagnostics(self):
        self.assertEqual(message('- '+SECRET).first, 'MicroSpec root: expected mapping, got sequence')
        self.assertEqual(message('"'+SECRET+'"').first, 'MicroSpec root: expected mapping, got string')
        for field in MICROSPEC_FIELDS:
            value = spec(); del value[field.name]
            self.check(value, f'MicroSpec {field.name}: missing required field')
        content = yaml.safe_dump(spec(scope=[])).replace('scope: []', 'scope:\n- 2024-01-01')
        self.assertEqual(message(content).first, 'MicroSpec scope[0]: expected non-empty string, got timestamp')

    def test_unquoted_colon_item_is_a_mapping_and_rejected_precisely(self):
        unquoted = ('id: BUG-1\ngoal: Keep FIFO\nscope:\n  - task_queue.py: preserve FIFO '+SECRET+'\n'
                    'constraints: []\nacceptance: []\nverification: []\nnon_goals: []\n')
        self.assertIs(type(_yaml_data(unquoted)['scope'][0]), dict)  # YAML semantics, not Switchyard's.
        caught = message(unquoted)
        self.assertEqual(caught.first, 'MicroSpec scope[0]: expected non-empty string, got mapping')
        self.assertNotIn(SECRET, caught.text); self.assertNotIn('task_queue', caught.text)

    def test_quoted_colon_item_is_a_valid_string(self):
        quoted = ('id: BUG-1\ngoal: Keep FIFO\nscope:\n  - "task_queue.py: preserve FIFO"\n'
                  'constraints: []\nacceptance: []\nverification: []\nnon_goals: []\n')
        self.assertEqual(validate_microspec(quoted).scope, ('task_queue.py: preserve FIFO',))

    def test_acceptance_semantics_unchanged(self):
        self.assertEqual(validate_microspec(yaml.safe_dump(spec(extra='allowed'))).id, 'BUG-1')
        self.assertEqual(validate_microspec(json.dumps(spec()), 'json').scope, ('task_queue.py',))


class MicroSpecGuidanceTests(unittest.TestCase):
    def test_guidance_is_generated_from_validator_fields(self):
        for format in ('yaml','json'):
            text = microspec_guidance(format)
            for field in MICROSPEC_FIELDS:
                line = next(l for l in text.splitlines() if l.startswith(f'- {field.name}: '))
                self.assertIn('one non-empty', line)
                if field.type != 'string':
                    self.assertIn('each item is one non-empty', line)
                    self.assertEqual('may be empty' in line, field.empty_allowed)
        self.assertNotIn('Quote every list item', microspec_guidance('json'))
        with self.assertRaises(WorkContractError):
            microspec_guidance('markdown')

    def compile(self, root, stage_resolver):
        bundle = load_workflow_bundle(CANONICAL)
        workspace = WorkWorkspace.open(root/'work', bundle.artifacts)
        workspace.initialize_request('request')
        for name in ('reproduction_evidence', 'diagnosis'):
            workspace.write_artifact(name, name+' evidence')
        compiled = compile_workflow(bundle, 'bugfix', workspace_resolver(bundle, 'bugfix', workspace, stage_resolver))
        state = next(s for s, p in compiled.plan.items() if p.binding and p.binding.logical_state == 'SPECIFY_FIX')
        return workspace, compiled.executors[state]

    def test_canonical_specify_fix_input_carries_yaml_string_rules(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('claude','python3'):  # Never launched: compilation only resolves executables.
                (root/name).write_text('#!/bin/sh\nexit 0\n'); (root/name).chmod(0o755)
            config = ProviderRuntimeConfig(str(root), str(root/'claude'), str(root/'claude'), {'PATH':str(root)},
                                           model_aliases={'codex':{'codex':'local'}})
            bundle = load_workflow_bundle(CANONICAL)
            _, executor = self.compile(root, ProviderStageExecutorResolver(bundle.profiles, config))
            invocation = executor._context(InvocationRequest(WorkUnit('request')))
        self.assertEqual([a.name for a in invocation.output_contract.artifacts], ['fix_microspec'])
        rendered = render_invocation(invocation)
        for text in ('MICROSPEC CONTENT RULES (yaml)',
                     '- scope: a YAML sequence (may be empty: []); each item is one non-empty YAML string.',
                     'never a mapping, never a nested sequence',
                     'Quote every list item as a double-quoted YAML string.',
                     'an item containing ": " parses as a mapping unless quoted',
                     'Correct:   - "task_queue.py: preserve FIFO ordering"',
                     'Incorrect: - task_queue.py: preserve FIFO ordering'):
            self.assertIn(text, rendered)
        self.assertEqual(rendered.count('MICROSPEC CONTENT RULES'), 1)

    def test_rejected_fix_microspec_commits_nothing_and_names_location(self):
        bad = ('id: FIX-1\ngoal: Monotonic tiebreaker\nscope:\n  - task_queue.py: use a counter '+SECRET+'\n'
               'constraints: []\nacceptance: []\nverification: []\nnon_goals: []\n')
        class Stage:
            def run(self, invocation):
                return StageResult('ready', (ProducedArtifact('fix_microspec', bad),), StageHandoff(SECRET+' summary'))
        with TemporaryDirectory() as folder:
            workspace, executor = self.compile(Path(folder), lambda binding: Stage())
            with self.assertRaises(WorkContractError) as caught:
                executor.run(InvocationRequest(WorkUnit('request')))
            self.assertEqual(str(caught.exception), 'bugfix.SPECIFY_FIX[0]: artifact fix_microspec: '
                             'MicroSpec scope[0]: expected non-empty string, got mapping')
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(workspace.versions('fix_microspec'), ())
            self.assertEqual(workspace.handoffs(), ())


if __name__ == '__main__':
    unittest.main()

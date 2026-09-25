from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from flow_config import FlowConfigError, load_flow
from runtime import Flow, State, FlowValidationError
from runtime.command import CommandExecutor
from runtime.codex import CodexExecutor
from runtime.claude import ClaudeExecutor


def definition():
    return {'version': 1, 'initial': 'verify',
            'executors': {'verifier': {'type': 'command', 'executable': sys.executable,
                                      'argv': [], 'timeout_seconds': 5}},
            'states': {'verify': {'executor': 'verifier', 'on': {'success': 'accepted'}},
                       'accepted': {'terminal': True}}}


class FlowConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root/'flow.yaml'

    def load(self, data=None, text=None):
        self.file.write_text(text if text is not None else yaml.safe_dump(data if data is not None else definition()), encoding='utf-8')
        return load_flow(self.file)

    def test_types_immutable_and_no_execution(self):
        data = definition()
        for kind in ('codex', 'claude'):
            data['executors'][kind] = {'type': kind, 'executable': sys.executable, 'timeout_seconds': 5}
        with patch('subprocess.Popen') as launch:
            loaded = self.load(data)
        launch.assert_not_called()
        self.assertIs(type(loaded.flow), Flow)
        self.assertIs(type(loaded.flow.states['verify']), State)
        for name, cls in [('verifier',CommandExecutor), ('codex',CodexExecutor), ('claude',ClaudeExecutor)]:
            self.assertIs(type(loaded.executors[name]), cls)
        with self.assertRaises(TypeError): loaded.executors['bad'] = None
        with self.assertRaises(FrozenInstanceError): loaded.flow = None
        self.assertEqual(loaded.executors['verifier'].command.argv, ())

    def test_symlink_and_cwd_resolution(self):
        a, b = self.root/'A', self.root/'B'
        a.mkdir(); b.mkdir()
        real = a/'flow.yaml'; link = b/'flow.yaml'
        data = definition(); data['executors']['verifier']['workspace'] = '.'
        real.write_text(yaml.safe_dump(data))
        try: link.symlink_to(real)
        except OSError as exc: self.skipTest(f'symlink unavailable: {exc.__class__.__name__}')
        cwd = os.getcwd()
        try:
            os.chdir(a)
            first = load_flow(link)
            os.chdir(b)
            second = load_flow(link)
        finally: os.chdir(cwd)
        self.assertEqual(first, second)
        self.assertEqual(first.executors['verifier'].command.cwd, str(b.resolve()))
        self.assertNotEqual(first.executors['verifier'].command.cwd, str(a.resolve()))

    def test_workspace_variants(self):
        for workspace in (None, '.', '..', str(self.root)):
            data = definition()
            if workspace is not None: data['executors']['verifier']['workspace'] = workspace
            actual = self.load(data).executors['verifier'].command.cwd
            self.assertEqual(actual, str((self.root/(workspace or '.')).resolve()))
        target = self.root/'target'; target.mkdir()
        link = self.root/'workspace-link'
        try: link.symlink_to(target, target_is_directory=True)
        except OSError: return
        data['executors']['verifier']['workspace'] = 'workspace-link'
        self.assertEqual(self.load(data).executors['verifier'].command.cwd, str(target.resolve()))

    def test_private_resolver_no_global_mutation(self):
        text = 'on: off\nyes: true\n'
        before = yaml.safe_load(text)
        snapshot = {key:list(value) for key,value in yaml.SafeLoader.yaml_implicit_resolvers.items()}
        source = yaml.safe_dump(definition()).replace("'on':", 'on:')
        loaded = self.load(text=source)
        self.assertEqual(dict(loaded.flow.states['verify'].on), {'success':'accepted'})
        self.assertEqual(yaml.safe_load(text), before)
        self.assertEqual(yaml.SafeLoader.yaml_implicit_resolvers, snapshot)

    def test_dependency_boundary_import_and_load(self):
        # A clean interpreter blocks yaml imports; flow_config import must still work.
        script = '''
import builtins
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'yaml': raise ModuleNotFoundError("missing yaml", name='yaml')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import flow_config
import importlib
original_module = importlib.import_module
def missing(name, *args):
    if name == 'yaml': raise ModuleNotFoundError("missing yaml", name='yaml')
    return original_module(name, *args)
importlib.import_module = missing
try: flow_config.load_flow(__import__('sys').argv[1])
except flow_config.FlowConfigError as e:
    assert '$dependency' in str(e) and 'pip install' in str(e)
    assert isinstance(e.__cause__, ModuleNotFoundError)
else: raise AssertionError('missing dependency accepted')
'''
        self.file.write_text('version: 1')
        result = subprocess.run([sys.executable,'-c',script,str(self.file)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_fields_types_and_canonical_states(self):
        cases = [(['version'], None), (['version'], True), (['version'], '1'), (['version'], 2),
                 (['task'],'secret'), (['states','accepted','terminal'],False),
                 (['states','accepted','terminal'],'false'), (['states','accepted','on'],{}),
                 (['states','verify','terminal'],False), (['states','verify','max_state_visit_per_run'],1),
                 (['states','verify','max_state_visits_per_run'],True),
                 (['states','verify','diagnostics_from_previous_max_bytes'],4097),
                 (['states','verify','on','succes'],'accepted'),
                 (['states','verify','on','success'],'absent'), (['states','verify','executor'],'absent'),
                 (['executors','verifier','timeout_seconds'],'120'), (['executors','verifier','timeout_seconds'],True),
                 (['executors','verifier','timeout_seconds'],float('nan')), (['executors','verifier','timeout_seconds'],float('inf')),
                 (['executors','verifier','argv'],'-c'), (['executors','verifier','argv'],[7]),
                 (['executors','verifier','executable'],'python'), (['executors','verifier','workspace'],'missing'),
                 (['executors','verifier','env'],{}), (['executors','verifier','diagnostic_capture_max_bytes'],-1)]
        for path, value in cases:
            with self.subTest(path=path,value=value):
                data = definition(); node = data
                for part in path[:-1]: node = node[part]
                node[path[-1]] = value
                with self.assertRaises(FlowConfigError) as caught: self.load(data)
                self.assertIn('.'.join(path), str(caught.exception))
        for path in (['version'], ['states','verify','executor'], ['executors','verifier','argv']):
            data = definition(); node = data
            for part in path[:-1]: node = node[part]
            del node[path[-1]]
            with self.assertRaises(FlowConfigError): self.load(data)

    def test_yaml_duplicates_keys_and_unsafe_features(self):
        for text in ('', '# comment', 'null', '[]', '1', 'x: \0', '---\n{}\n---\n',
                     'version: 1\nversion: 1', '"version": 1\nversion: 1',
                     'executors:\n  v: {}\n  v: {}', 'executors:\n  v:\n    type: codex\n    type: claude',
                     'states:\n  x:\n    terminal: true\n    terminal: true', 'states:\n  fix:\n    on:\n      failed: a\n      failed: b',
                     '1: x', 'true: x', 'null: x', '? [a, b]\n: x', 'x: 2026-99-99',
                     'x: &anchor {}', 'x: *anchor', 'x: &a [*a]', 'x: !!str hi',
                     'x: !!python/object/apply:os.system ["touch should-not-exist"]', '<<: {}', '"<<": {}'):
            with self.subTest(text=text):
                with self.assertRaises(FlowConfigError) as caught: self.load(text=text)
                self.assertLessEqual(len(str(caught.exception)),512)
        with self.assertRaisesRegex(FlowConfigError, r'states.fix.on.failed: duplicate.*line'):
            self.load(text='states:\n  fix:\n    on:\n      failed: a\n      failed: b')
        with self.assertRaisesRegex(FlowConfigError, 'states.*scalar string.*line'):
            self.load(text='states:\n  1: {}')
        data = {'version':1, 'initial':'1', 'executors':{}, 'states':{'1':{'terminal':True}}}
        self.assertEqual(self.load(data).flow.initial,'1')

    def test_existing_domain_validator_and_causes(self):
        for bounded in (False,True):
            data = definition()
            data['states']['verify']['on'] = {'success':'verify'}
            if bounded: data['states']['verify']['max_state_visits_per_run'] = 1
            if bounded: self.load(data)
            else:
                with self.assertRaises(FlowConfigError) as caught: self.load(data)
                self.assertIsInstance(caught.exception.__cause__,FlowValidationError)
                self.assertIn('cycle', str(caught.exception.__cause__))
        data['states']['verify']['on']['budget_exceeded'] = 'verify'
        with self.assertRaises(FlowConfigError) as caught: self.load(data)
        self.assertIsInstance(caught.exception.__cause__,FlowValidationError)
        data = definition(); data['unknown\n'+'x'*900] = 'DO-NOT-ECHO'
        with self.assertRaises(FlowConfigError) as caught: self.load(data)
        self.assertNotIn('DO-NOT-ECHO',str(caught.exception))
        self.assertNotIn('\n',str(caught.exception))
        self.assertLessEqual(len(str(caught.exception)),512)
        self.file.write_bytes(b'\xff')
        with self.assertRaises(FlowConfigError) as caught: load_flow(self.file)
        self.assertIsInstance(caught.exception.__cause__,UnicodeError)

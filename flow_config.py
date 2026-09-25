"""Strict YAML frontend. Runtime objects and domain validation remain in runtime."""

from dataclasses import dataclass
import importlib
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from runtime import Executor, Flow, FlowValidationError, State, validate
from runtime.command import Command, CommandExecutor
from runtime.codex import CodexConfig, CodexExecutor
from runtime.claude import ClaudeConfig, ClaudeExecutor


class FlowConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedFlow:
    flow: Flow
    executors: Mapping[str, Executor]

    def __post_init__(self):
        object.__setattr__(self, 'executors', MappingProxyType(dict(self.executors)))


def _error(path, message):
    return FlowConfigError((path + ': ' + message)[:512])


def _at(path, key):
    key = key[:64]
    component = key if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', key) else '[' + json.dumps(key, ensure_ascii=True) + ']'
    return (path + ('.' if path and not component.startswith('[') else '') + component)[:400]


def _yaml_data(text):
    try:
        yaml = importlib.import_module('yaml')
    except ModuleNotFoundError as exc:
        if exc.name != 'yaml':
            raise
        raise _error('$dependency', 'PyYAML>=6.0.3,<7 required; install with python3 -m pip install -r requirements.txt') from exc

    class Loader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            event = self.peek_event()
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, 'anchor', None) is not None or getattr(event, 'tag', None) is not None:
                mark = event.start_mark
                raise _error('$', f'anchors, aliases and explicit tags are forbidden (line {mark.line+1}, column {mark.column+1})')
            return super().compose_node(parent, index)

    # Resolver entries contain compiled regexes, which are immutable. Copy each list.
    Loader.yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in entries if tag != 'tag:yaml.org,2002:bool']
        for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    Loader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(true|false)$'), ['t', 'f'])
    try:
        loader = Loader(text)
    except yaml.YAMLError as exc:
        raise _error('$', 'invalid YAML character stream') from exc

    def build(node, path):
        if isinstance(node, yaml.MappingNode):
            pairs = []
            seen = set()
            for key_node, value_node in node.value:
                mark = key_node.start_mark
                location = f' (line {mark.line+1}, column {mark.column+1})'
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == '<<':
                    raise _error(_at(path, '<<'), 'merge keys are forbidden' + location)
                if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != 'tag:yaml.org,2002:str':
                    raise _error(path or '$', 'expected scalar string mapping key' + location)
                key = loader.construct_object(key_node)
                child_path = _at(path, key)
                if key in seen:
                    raise _error(child_path, 'duplicate mapping key' + location)
                seen.add(key)
                pairs.append((key, value_node, child_path))
            # All this mapping's keys were checked before creating its dictionary.
            return {key: build(value, child_path) for key, value, child_path in pairs}
        if isinstance(node, yaml.SequenceNode):
            return [build(value, f'{path}[{i}]') for i, value in enumerate(node.value)]
        try:
            return loader.construct_object(node)
        except (ValueError, OverflowError) as exc:
            raise _error(path or '$', 'invalid YAML scalar value') from exc

    try:
        node = loader.get_single_node()
        if node is None:
            raise _error('$', 'expected mapping')
        return build(node, '')
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        location = f' (line {mark.line+1}, column {mark.column+1})' if mark else ''
        raise _error('$', 'invalid YAML or multiple documents' + location) from exc
    except RecursionError as exc:
        raise _error('$', 'YAML nesting exceeds parser capacity') from exc
    finally:
        loader.dispose()


def _mapping(value, path):
    if type(value) is not dict:
        raise _error(path or '$', 'expected mapping')


def _fields(value, required, optional, path):
    _mapping(value, path)
    for key in value:
        if key not in required | optional:
            raise _error(_at(path, key), 'unknown field')
    for key in sorted(required):
        if key not in value:
            raise _error(_at(path, key), 'missing required field')


def _string(value, path, nonempty=True):
    if type(value) is not str or (nonempty and not value) or '\0' in value:
        raise _error(path, 'expected ' + ('nonempty ' if nonempty else '') + 'string without NUL')


def _limit(value, path, maximum=None):
    if value is not None and (type(value) is not int or value < 0 or (maximum is not None and value > maximum)):
        raise _error(path, 'expected null or nonnegative integer' + (f' <= {maximum}' if maximum is not None else '') + ', excluding bool')


def load_flow(path: str | os.PathLike[str]) -> LoadedFlow:
    # abspath is lexical: do not resolve a flow-file symlink before selecting base.
    try:
        file_path = Path(os.path.abspath(path))
        text = file_path.read_text(encoding='utf-8')
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise _error('$', 'cannot read UTF-8 flow file') from exc
    data = _yaml_data(text)
    _mapping(data, '$')
    if 'version' not in data:
        raise _error('version', 'missing required field')
    if type(data['version']) is not int:
        raise _error('version', 'expected integer, excluding bool')
    if data['version'] != 1:
        # Avoid rendering arbitrarily large numbers in public messages.
        version = str(data['version']) if data['version'].bit_length() < 256 else '<large integer>'
        raise _error('version', 'unsupported flow configuration version: ' + version)
    _fields(data, {'version', 'initial', 'executors', 'states'}, set(), '')
    _string(data['initial'], 'initial')
    _mapping(data['executors'], 'executors')
    _mapping(data['states'], 'states')
    if not data['states']:
        raise _error('states', 'expected nonempty mapping')
    executors = {}
    for name, definition in data['executors'].items():
        here = _at('executors', name)
        _string(name, here)
        _mapping(definition, here)
        kind = definition.get('type')
        if type(kind) is not str or kind not in ('command', 'codex', 'claude'):
            raise _error(here + '.type', 'expected command, codex or claude')
        required = {'type', 'executable', 'timeout_seconds'} | ({'argv'} if kind == 'command' else set())
        optional = {'workspace'} | ({'diagnostic_capture_max_bytes'} if kind == 'command' else set())
        _fields(definition, required, optional, here)
        executable = definition['executable']
        _string(executable, here + '.executable')
        if not os.path.isabs(executable):
            raise _error(here + '.executable', 'expected absolute executable path')
        timeout = definition['timeout_seconds']
        try:
            valid_timeout = type(timeout) in (int, float) and timeout > 0 and math.isfinite(timeout)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise _error(here + '.timeout_seconds', f'expected positive finite number, got {type(timeout).__name__}')
        workspace = definition.get('workspace', '.')
        _string(workspace, here + '.workspace')
        try:
            workspace = (file_path.parent / workspace).resolve(strict=True)
            if not workspace.is_dir():
                raise _error(here + '.workspace', 'expected existing directory')
        except (OSError, RuntimeError) as exc:
            raise _error(here + '.workspace', 'cannot resolve existing directory') from exc
        argv = definition.get('argv')
        cap = definition.get('diagnostic_capture_max_bytes')
        if kind == 'command':
            if type(argv) is not list:
                raise _error(here + '.argv', 'expected list of strings')
            for i, arg in enumerate(argv):
                _string(arg, f'{here}.argv[{i}]', nonempty=False)
            _limit(cap, here + '.diagnostic_capture_max_bytes', 4096)
        try:
            if kind == 'command':
                executor = CommandExecutor(Command(executable, tuple(argv), str(workspace), timeout, cap))
            elif kind == 'codex':
                executor = CodexExecutor(CodexConfig(executable, str(workspace), timeout))
            else:
                executor = ClaudeExecutor(ClaudeConfig(executable, str(workspace), timeout))
        except (TypeError, ValueError) as exc:
            raise _error(here, 'executor construction failed') from exc
        executors[name] = executor
    states = {}
    for name, definition in data['states'].items():
        here = _at('states', name)
        _string(name, here)
        _mapping(definition, here)
        if 'terminal' in definition:
            if definition['terminal'] is not True:
                raise _error(here + '.terminal', 'only terminal: true is permitted; executable states omit terminal')
            _fields(definition, {'terminal'}, set(), here)
            states[name] = State(terminal=True)
            continue
        _fields(definition, {'executor', 'on'}, {'max_state_visits_per_run', 'diagnostics_from_previous_max_bytes'}, here)
        ref = definition['executor']
        _string(ref, here + '.executor')
        if ref not in executors:
            raise _error(here + '.executor', 'unknown executor reference')
        transitions = definition['on']
        _mapping(transitions, here + '.on')
        for outcome, target in transitions.items():
            location = _at(here + '.on', outcome)
            if outcome not in ('success', 'failed', 'error', 'timeout', 'cancelled', 'budget_exceeded'):
                raise _error(location, 'unknown outcome/event')
            _string(target, location)
            if target not in data['states']:
                raise _error(location, 'unknown state target')
        visits = definition.get('max_state_visits_per_run')
        cap = definition.get('diagnostics_from_previous_max_bytes')
        _limit(visits, here + '.max_state_visits_per_run')
        _limit(cap, here + '.diagnostics_from_previous_max_bytes', 4096)
        states[name] = State(executor=ref, on=transitions, max_state_visits_per_run=visits, diagnostics_from_previous_max_bytes=cap)
    if data['initial'] not in states:
        raise _error('initial', 'unknown state reference')
    flow = Flow(data['initial'], states)
    try:
        validate(flow, executors)
    except FlowValidationError as exc:
        raise _error('states', 'domain validation failed (cause: FlowValidationError)') from exc
    return LoadedFlow(flow, executors)

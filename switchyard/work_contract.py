"""Durable application-layer work artifacts; no runtime or executor transport."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from flow_config import FlowConfigError, _yaml_data
from .workflow_bundle import ArtifactSpec, _artifact_catalog


class WorkContractError(ValueError):
    """Invalid content, corrupt history, unavailable storage or conflicting writes."""


def _check(condition, message):
    if not condition:
        raise WorkContractError(message)


def _boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, UnicodeError, FlowConfigError) as exc:
            raise WorkContractError(str(exc)) from exc
    return wrapped


def _name(value):
    _check(type(value) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', value), 'invalid logical identifier')


def _text(value, label, nonempty=True):
    _check(type(value) is str and (bool(value.strip()) or not nonempty), f'invalid {label}')
    value.encode('utf-8')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _json(text):
    def invalid(value):
        raise WorkContractError('non-finite JSON number')
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid)
    except WorkContractError:
        raise
    except (ValueError, RecursionError) as exc:
        raise WorkContractError('invalid JSON') from exc


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


@dataclass(frozen=True, slots=True)
class MicroSpec:
    id: str
    goal: str
    scope: tuple[str, ...]
    constraints: tuple[str, ...]
    acceptance: tuple[str, ...]
    verification: tuple[str, ...]
    non_goals: tuple[str, ...]

    def __post_init__(self):
        for key in ('scope', 'constraints', 'acceptance', 'verification', 'non_goals'):
            object.__setattr__(self, key, tuple(getattr(self, key)))


@_boundary
def validate_microspec(content: str, format: str = 'yaml') -> MicroSpec:
    """Validate required concepts from docs/protocols/micro-spec.md, not extensions."""
    _text(content, 'MicroSpec content')
    _check(format in ('yaml', 'json'), 'MicroSpec requires yaml or json')
    value = _yaml_data(content) if format == 'yaml' else _json(content)
    _check(type(value) is dict, 'MicroSpec root must be a mapping')
    required = ('id', 'goal', 'scope', 'constraints', 'acceptance', 'verification', 'non_goals')
    _check(all(key in value for key in required), 'missing required MicroSpec field')
    for key in ('id', 'goal'):
        _text(value[key], 'MicroSpec ' + key)
    lists = {}
    for key in required[2:]:
        _check(type(value[key]) is list, f'MicroSpec {key} must be a list')
        for item in value[key]:
            _text(item, 'MicroSpec ' + key + ' item')
        lists[key] = tuple(value[key])
    return MicroSpec(value['id'], value['goal'], **lists)


@dataclass(frozen=True, slots=True)
class ArtifactProducer:
    flow_name: str
    logical_state: str
    rung_index: int
    profile_name: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    name: str
    version: int
    path: str
    format: str
    schema: str | None
    sha256: str
    byte_size: int
    producer: ArtifactProducer | None = None


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    flow: str
    logical_state: str
    role: str
    rung_index: int
    outcome: str
    summary: str
    profile_name: str | None = None
    findings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    changed_assumptions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    produced_artifacts: tuple[str, ...] = ()
    version: int = 1
    sequence: int = 0

    def __post_init__(self):
        for key in ('findings', 'evidence', 'changed_assumptions', 'blockers', 'produced_artifacts'):
            value = getattr(self, key)
            _check(type(value) in (list, tuple), f'handoff {key} must be a sequence')
            object.__setattr__(self, key, tuple(value))


def _producer(value):
    _check(type(value) is ArtifactProducer, 'invalid producer')
    for key in ('flow_name', 'logical_state', 'profile_name'):
        _name(getattr(value, key))
    _check(type(value.rung_index) is int and value.rung_index >= 0, 'invalid producer rung index')


def _handoff(value, catalog):
    _check(type(value) is HandoffRecord, 'expected HandoffRecord')
    for key in ('flow', 'logical_state', 'role'):
        _name(getattr(value, key))
    if value.profile_name is not None:
        _name(value.profile_name)
    _check(type(value.rung_index) is int and value.rung_index >= 0, 'invalid handoff rung index')
    _check(type(value.version) is int and value.version == 1, 'unsupported handoff version')
    _check(type(value.sequence) is int and value.sequence >= 0, 'invalid handoff sequence')
    for key in ('outcome', 'summary'):
        text = getattr(value, key)
        _text(text, 'handoff ' + key)
        _check(len(text) <= 8192, 'handoff text exceeds 8192 characters')
    for key in ('findings', 'evidence', 'changed_assumptions', 'blockers', 'produced_artifacts'):
        values = getattr(value, key)
        _check(len(values) <= 64, 'handoff collection exceeds 64 items')
        for text in values:
            _text(text, 'handoff ' + key)
            _check(len(text) <= 8192, 'handoff item exceeds 8192 characters')
            if key in ('evidence', 'produced_artifacts'):
                _name(text)
                _check(text in catalog, 'undeclared handoff artifact')
                _check(key != 'produced_artifacts' or catalog[text].source != 'request', 'cannot produce request')


def _catalog_data(catalog):
    _check(isinstance(catalog, Mapping), 'expected artifact catalog mapping')
    _check(all(type(spec) is ArtifactSpec for spec in catalog.values()), 'expected ArtifactSpec definitions')
    return {name: {key: value for key, value in asdict(spec).items() if value is not None}
            for name, spec in catalog.items()}


def _keys(value, fields, label):
    _check(type(value) is dict and set(value) == set(fields), f'invalid {label} fields')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


_EXTENSIONS = {'markdown': 'md', 'text': 'txt', 'yaml': 'yaml', 'json': 'json'}


@dataclass(frozen=True)
class WorkWorkspace:
    root: Path
    artifacts: Mapping[str, ArtifactSpec]

    def __post_init__(self):
        object.__setattr__(self, 'artifacts', MappingProxyType(dict(self.artifacts)))

    @classmethod
    @_boundary
    def open(cls, path, artifacts: Mapping[str, ArtifactSpec] | None = None):
        root = Path(path).absolute()
        _check(not root.is_symlink(), 'workspace root cannot be a symlink')
        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(root, {})
        manifest = workspace._path('manifest.jsonl')
        if not manifest.exists():
            _check(artifacts is not None, 'new workspace requires artifact catalog')
            data = _catalog_data(artifacts)
            _artifact_catalog(data)
            with workspace._locked():
                workspace._publish('manifest.jsonl', _encode({'record_version': 1, 'kind': 'catalog', 'artifacts': data}))
        catalog, _, _ = workspace._scan()
        if artifacts is not None:
            _check(_artifact_catalog(_catalog_data(artifacts)) == catalog, 'workspace catalog mismatch')
        return cls(root, catalog)

    def _path(self, relative):
        _check(type(relative) is str and relative and not Path(relative).is_absolute(), 'invalid relative path')
        parts = relative.split('/')
        _check(all(part not in ('', '.', '..') and '\\' not in part and '\0' not in part for part in parts), 'unsafe path')
        _check(not self.root.is_symlink() and self.root.is_dir(), 'invalid workspace root')
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            _check(not current.is_symlink(), 'managed symlinks are forbidden')
            if current.exists():
                _check(current.is_dir() if index < len(parts)-1 else current.is_file(), 'invalid managed file type')
        return current

    @contextmanager
    def _locked(self):
        lock = self._path('.write.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkContractError('workspace writer active or stale lock; inspect before recovery') from exc
        os.close(fd)
        try:
            _check(not self._path('.pending').exists(), 'orphan temporary file; manual recovery required')
            yield
        finally:
            lock.unlink()

    def _publish(self, relative, data):
        target = self._path(relative)
        _check(not target.exists(), 'unindexed or existing file would be overwritten; manual recovery required')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path('.pending')
        with temporary.open('xb') as stream:
            _check(stream.write(data) == len(data), 'short artifact write')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # Atomic no-overwrite publication, unlike replace/rename.
        temporary.unlink()

    def _append(self, record):
        data = _encode({'record_version': 1, **record})
        with self._path('manifest.jsonl').open('ab') as stream:
            _check(stream.write(data) == len(data), 'short manifest append')
            stream.flush()
            os.fsync(stream.fileno())

    def _scan(self):
        raw = self._path('manifest.jsonl').read_bytes()
        _check(raw and raw.endswith(b'\n'), 'empty or truncated manifest')
        records = []
        for line in raw.decode('utf-8').splitlines():
            value = _json(line)
            _check(type(value) is dict and type(value.get('record_version')) is int and value['record_version'] == 1,
                   'unsupported manifest record')
            records.append(value)
        header = records[0]
        _keys(header, ('record_version', 'kind', 'artifacts'), 'catalog record')
        _check(header['kind'] == 'catalog', 'manifest must begin with catalog')
        catalog = _artifact_catalog(header['artifacts'])
        if self.artifacts:
            _check(catalog == self.artifacts, 'manifest catalog changed')
        artifacts, handoffs, counts = [], [], {}
        for record in records[1:]:
            kind = record.get('kind')
            if kind == 'artifact':
                _keys(record, ('record_version', 'kind', *ArtifactRecord.__dataclass_fields__), 'artifact record')
                name = record['name']; _name(name)
                _check(name in catalog, 'undeclared manifest artifact')
                spec = catalog[name]
                version = record['version']
                _check(type(version) is int and version == counts.get(name, 0)+1, 'invalid artifact version sequence')
                expected = 'request.md' if spec.source == 'request' else f'artifacts/{name}/{version:06d}.{_EXTENSIONS[spec.format]}'
                _check(spec.source != 'request' or version == 1, 'request is immutable')
                _check(record['format'] == spec.format and record['schema'] == spec.schema, 'artifact contract mismatch')
                producer = record['producer']
                if producer is not None:
                    _keys(producer, ArtifactProducer.__dataclass_fields__, 'producer')
                    producer = ArtifactProducer(**producer); _producer(producer)
                _check(spec.source != 'request' or producer is None, 'request cannot have agent producer')
                artifact = ArtifactRecord(**{key: record[key] for key in ArtifactRecord.__dataclass_fields__ if key != 'producer'}, producer=producer)
                artifacts.append(artifact)
                counts[name] = version
            elif kind == 'handoff':
                _keys(record, ('record_version', 'kind', 'sequence', 'path', 'sha256', 'byte_size'), 'handoff record')
                seq = record['sequence']
                _check(type(seq) is int and seq == len(handoffs)+1, 'invalid handoff sequence')
                expected = f'handoffs/{seq:06d}.json'
                handoffs.append(record)
            else:
                raise WorkContractError('unknown manifest record kind')
            _check(record['path'] == expected, 'noncanonical manifest path')
            _check(type(record['byte_size']) is int and record['byte_size'] >= 0, 'invalid byte size')
            _check(type(record['sha256']) is str and re.fullmatch('[0-9a-f]{64}', record['sha256']), 'invalid sha256')
        return catalog, tuple(artifacts), tuple(handoffs)

    def _verified(self, record):
        values = asdict(record) if type(record) is ArtifactRecord else record
        data = self._path(values['path']).read_bytes()
        _check(len(data) == values['byte_size'] and _digest(data) == values['sha256'], 'artifact/handoff integrity mismatch')
        return data.decode('utf-8')

    def _definition(self, name):
        _name(name)
        _check(name in self.artifacts, 'undeclared artifact')
        return self.artifacts[name]

    def _validate_content(self, spec, content):
        _text(content, 'artifact content', nonempty=False)
        if spec.schema == 'microspec':
            validate_microspec(content, spec.format)
        elif spec.format == 'yaml':
            _yaml_data(content)
        elif spec.format == 'json':
            _json(content)

    def _write(self, name, content, producer, version):
        spec = self._definition(name)
        self._validate_content(spec, content)
        if producer is not None:
            _producer(producer)
        data = content.encode('utf-8')
        path = 'request.md' if spec.source == 'request' else f'artifacts/{name}/{version:06d}.{_EXTENSIONS[spec.format]}'
        record = ArtifactRecord(name, version, path, spec.format, spec.schema, _digest(data), len(data), producer)
        self._publish(path, data)
        self._append({'kind': 'artifact', **asdict(record)})
        return record

    @_boundary
    def initialize_request(self, text: str) -> ArtifactRecord:
        spec = self._definition('request')
        _check(spec.source == 'request', 'no immutable request declaration')
        _text(text, 'request', nonempty=False)
        with self._locked():
            _, artifacts, _ = self._scan()
            existing = [r for r in artifacts if r.name == 'request']
            if existing:
                _check(self._verified(existing[0]) == text, 'request differs from immutable original')
                return existing[0]
            return self._write('request', text, None, 1)

    @_boundary
    def validate_artifact(self, name: str, content: str, producer: ArtifactProducer | None = None) -> None:
        """Validate a proposed production without writing any workspace state."""
        spec = self._definition(name)
        _check(spec.source != 'request', 'request may only be initialized, never produced')
        self._validate_content(spec, content)
        if producer is not None:
            _producer(producer)

    @_boundary
    def validate_handoff(self, record: HandoffRecord) -> None:
        """Validate a proposed handoff before committing a batch of outputs."""
        _handoff(record, self.artifacts)
        _check(record.sequence == 0, 'store assigns handoff sequence')

    @_boundary
    def write_artifact(self, name: str, content: str, producer: ArtifactProducer | None = None) -> ArtifactRecord:
        _check(self._definition(name).source != 'request', 'request may only be initialized, never produced')
        with self._locked():
            _, artifacts, _ = self._scan()
            previous = [r for r in artifacts if r.name == name]
            return self._write(name, content, producer, len(previous)+1)

    @_boundary
    def versions(self, name: str) -> tuple[ArtifactRecord, ...]:
        self._definition(name)
        _, records, _ = self._scan()
        selected = tuple(record for record in records if record.name == name)
        for record in selected:
            self._verified(record)
        return selected

    def latest_artifact(self, name: str) -> ArtifactRecord | None:
        versions = self.versions(name)
        return versions[-1] if versions else None

    @_boundary
    def read_artifact(self, name: str, version: int | None = None) -> str:
        _check(version is None or (type(version) is int and version > 0), 'invalid requested version')
        versions = self.versions(name)
        selected = next((r for r in versions if r.version == version), None) if version is not None else (versions[-1] if versions else None)
        _check(selected is not None, 'artifact version unavailable')
        return self._verified(selected)

    @_boundary
    def write_handoff(self, record: HandoffRecord) -> HandoffRecord:
        _handoff(record, self.artifacts)
        _check(record.sequence == 0, 'store assigns handoff sequence')
        with self._locked():
            _, _, handoffs = self._scan()
            committed = replace(record, sequence=len(handoffs)+1)
            data = _encode(asdict(committed))
            path = f'handoffs/{committed.sequence:06d}.json'
            self._publish(path, data)
            self._append({'kind': 'handoff', 'sequence': committed.sequence, 'path': path,
                          'sha256': _digest(data), 'byte_size': len(data)})
            return committed

    @_boundary
    def handoffs(self, logical_state: str | None = None) -> tuple[HandoffRecord, ...]:
        if logical_state is not None:
            _name(logical_state)
        _, _, records = self._scan()
        result = []
        for record in records:
            value = _json(self._verified(record))
            _keys(value, HandoffRecord.__dataclass_fields__, 'handoff')
            handoff = HandoffRecord(**value)
            _handoff(handoff, self.artifacts)
            _check(handoff.sequence == record['sequence'], 'handoff sequence mismatch')
            if logical_state is None or handoff.logical_state == logical_state:
                result.append(handoff)
        return tuple(result)

    def latest_handoff(self) -> HandoffRecord | None:
        records = self.handoffs()
        return records[-1] if records else None

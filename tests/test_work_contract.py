"""Durable contracts and integrity; no provider or executor transport."""
from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from flow_config import FlowConfigError
from switchyard.workflow_bundle import _bundle_yaml_data, ArtifactSpec, load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow
from switchyard.work_contract import (ArtifactProducer, HandoffRecord, WorkContractError,
                                     WorkWorkspace, validate_microspec)

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/delivery/workflows-v2.yaml'
REQUEST = 'When tabbing from a task row, focus skips unexpectedly.\r\nI want keyboard navigation to move in the natural order. λ\n'


def catalog():
    return {'request': ArtifactSpec('markdown', source='request'),
            'microspec': ArtifactSpec('yaml', 'microspec'),
            'fix_microspec': ArtifactSpec('json', 'microspec'),
            'reproduction_evidence': ArtifactSpec('markdown'),
            'diagnosis': ArtifactSpec('text'), 'data': ArtifactSpec('json'), 'notes': ArtifactSpec('yaml')}


def spec_data():
    return {'id': 'FOCUS-001', 'goal': 'Make keyboard focus follow the natural order.',
            'scope': ['Keyboard navigation'], 'constraints': ['Preserve user intent'],
            'acceptance': ['Tab follows visible row order'], 'verification': ['Observable navigation test'],
            'non_goals': []}


def handoff(**kwargs):
    return HandoffRecord('bugfix', 'DIAGNOSE', 'diagnostician', 0, 'inconclusive',
                         'Reproduced; inspect actual focus owner.', profile_name='haiku', **kwargs)


class WorkContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'work'
        self.store = WorkWorkspace.open(self.root, catalog())

    def test_exact_request_idempotency_and_mutation_rejection(self):
        first = self.store.initialize_request(REQUEST)
        before = (self.root / 'manifest.jsonl').read_bytes()
        reopened = WorkWorkspace.open(self.root)
        self.assertEqual(reopened.initialize_request(REQUEST), first)
        self.assertEqual((self.root / 'manifest.jsonl').read_bytes(), before)
        self.assertEqual((self.root / 'request.md').read_bytes(), REQUEST.encode('utf-8'))
        with self.assertRaisesRegex(WorkContractError, 'differs'): reopened.initialize_request('different')
        with self.assertRaisesRegex(WorkContractError, 'never produced'): self.store.write_artifact('request', 'replacement')
        (self.root / 'request.md').write_text('tampered')
        with self.assertRaisesRegex(WorkContractError, 'integrity'): reopened.initialize_request(REQUEST)

    def test_progressive_refinement_keeps_request_and_versions(self):
        self.store.initialize_request(REQUEST)
        producer = ArtifactProducer('bugfix', 'DIAGNOSE', 1, 'sonnet')
        micro = self.store.write_artifact('microspec', yaml.safe_dump(spec_data()))
        evidence = self.store.write_artifact('reproduction_evidence', 'Observed Tab skip in reproducible navigation test.')
        first = self.store.write_artifact('diagnosis', 'Initial hypothesis', producer)
        second = self.store.write_artifact('diagnosis', 'Revised diagnosis from observed flow', producer)
        fix = self.store.write_artifact('fix_microspec', json.dumps(spec_data()))
        self.assertEqual(self.store.read_artifact('request'), REQUEST)
        self.assertEqual(self.store.versions('diagnosis'), (first, second))
        self.assertEqual(self.store.latest_artifact('diagnosis'), second)
        self.assertEqual(self.store.read_artifact('diagnosis', 1), 'Initial hypothesis')
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(first.producer, producer)
        self.assertEqual((first.version, second.version), (1, 2))
        for record in (micro, evidence, first, second, fix):
            self.assertFalse(Path(record.path).is_absolute())
            self.assertTrue((self.root / record.path).is_file())
        with self.assertRaises(FrozenInstanceError): first.version = 9
        with self.assertRaises(TypeError): self.store.artifacts['x'] = ArtifactSpec('text')
        self.assertEqual(WorkWorkspace.open(self.root).versions('diagnosis'), (first, second))

    def test_integrity_read_and_lookup_reject_mutation(self):
        record = self.store.write_artifact('diagnosis', 'original')
        (self.root / record.path).write_bytes(b'changed!')
        for operation in (lambda: self.store.read_artifact('diagnosis'), lambda: self.store.latest_artifact('diagnosis'),
                          lambda: self.store.versions('diagnosis')):
            with self.assertRaisesRegex(WorkContractError, 'integrity'): operation()

    def test_no_version_availability_is_invented(self):
        self.assertIsNone(self.store.latest_artifact('diagnosis'))
        self.assertEqual(self.store.versions('diagnosis'), ())
        with self.assertRaisesRegex(WorkContractError, 'unavailable'): self.store.read_artifact('diagnosis')
        for version in (True, 0, -1, '1'):
            with self.subTest(version=version), self.assertRaises(WorkContractError):
                self.store.read_artifact('diagnosis', version)

    def test_names_cannot_be_paths(self):
        for name in ('../foo', '/foo', 'a/b', '.', '..', 'x\0', 'a\\b', '', None, True):
            with self.subTest(name=name), self.assertRaises(WorkContractError):
                self.store.write_artifact(name, 'data')
        with self.assertRaisesRegex(WorkContractError, 'undeclared'): self.store.write_artifact('typo', 'data')

    def test_valid_microspec_optional_fields_and_empty_collections(self):
        data = spec_data(); data['context'] = ['docs/protocols/micro-spec.md']; data['extension'] = {'custom': True}
        result = validate_microspec(yaml.safe_dump(data))
        self.assertEqual(result.id, 'FOCUS-001')
        self.assertEqual(result.non_goals, ())  # Protocol doesn't forbid empty lists.
        with self.assertRaises(FrozenInstanceError): result.id = 'changed'
        content = yaml.safe_dump(data)
        self.store.write_artifact('microspec', content)
        self.assertEqual(self.store.read_artifact('microspec'), content)
        self.assertEqual(validate_microspec(json.dumps(data), 'json'), result)

    def test_invalid_microspec_has_no_commit(self):
        invalid = ['bad: [', '[]', 'null', 'true']
        for key in spec_data():
            data = spec_data(); del data[key]; invalid.append(yaml.safe_dump(data))
            data = spec_data(); data[key] = 5; invalid.append(yaml.safe_dump(data))
        data = spec_data(); data['scope'] = [True]; invalid.append(yaml.safe_dump(data))
        before = (self.root / 'manifest.jsonl').read_bytes()
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(WorkContractError):
                self.store.write_artifact('microspec', content)
        self.assertEqual((self.root / 'manifest.jsonl').read_bytes(), before)
        self.assertFalse((self.root / 'artifacts').exists())

    def test_formats_strict_encoding_json_and_yaml(self):
        for name, content in [('data', '{'), ('data', '{"x":1,"x":2}'), ('data', 'NaN'),
                              ('notes', 'x: ['), ('notes', 'x: 1\nx: 2'), ('diagnosis', '\ud800')]:
            with self.subTest(name=name, content=content), self.assertRaises(WorkContractError):
                self.store.write_artifact(name, content)
        self.store.write_artifact('data', '[1, true, null]')
        self.store.write_artifact('notes', '- note\n- 42\n')
        self.store.write_artifact('diagnosis', '')  # Opaque text has no invented schema.

    def test_handoffs_order_immutability_and_declared_references(self):
        self.assertEqual(self.store.handoffs(), ())
        self.assertIsNone(self.store.latest_handoff())
        values = ['Observed repeatable skip']
        original = handoff(findings=values, evidence=('reproduction_evidence',), produced_artifacts=('diagnosis',))
        values.clear()
        first = self.store.write_handoff(original)
        second = self.store.write_handoff(replace(original, rung_index=1, profile_name='sonnet', outcome='ready'))
        self.assertEqual(original.sequence, 0)
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(first.findings, ('Observed repeatable skip',))
        reopened = WorkWorkspace.open(self.root)
        self.assertEqual(reopened.handoffs(), (first, second))
        self.assertEqual(reopened.latest_handoff(), second)
        self.assertEqual(reopened.handoffs('DIAGNOSE'), (first, second))
        self.assertEqual(reopened.handoffs('FIX'), ())
        with self.assertRaises(FrozenInstanceError): first.summary = 'changed'
        self.assertNotIn('chat', asdict(first))
        self.assertNotIn('prompt', asdict(first))
        self.assertIsNone(self.store.latest_artifact('diagnosis'))  # References do not fabricate artifacts.

    def test_invalid_handoff_and_producer(self):
        for record in (handoff(evidence=('../outside',)), handoff(evidence=('unknown',)),
                       handoff(produced_artifacts=('request',)), replace(handoff(), rung_index=True),
                       replace(handoff(), summary=''), replace(handoff(), summary='x'*8193),
                       replace(handoff(), version=2), replace(handoff(), sequence=1)):
            with self.subTest(record=record), self.assertRaises(WorkContractError): self.store.write_handoff(record)
        for producer in ({'metadata': 'anything'}, ArtifactProducer('flow', 'STATE', True, 'profile')):
            with self.assertRaises(WorkContractError): self.store.write_artifact('diagnosis', 'x', producer)

    def test_handoff_integrity(self):
        self.store.write_handoff(handoff())
        (self.root / 'handoffs/000001.json').write_text('{}')
        with self.assertRaisesRegex(WorkContractError, 'integrity'): self.store.handoffs()

    def test_manifest_corruption_fails_explicitly(self):
        path = self.root / 'manifest.jsonl'
        initial = path.read_bytes()
        for suffix in (b'{', b'\xff\n', b'[]\n', b'{}\n', b'\n', b'{"record_version":1,"record_version":1}\n'):
            with self.subTest(suffix=suffix):
                path.write_bytes(initial + suffix)
                with self.assertRaises(WorkContractError): WorkWorkspace.open(self.root)
        path.write_bytes(initial)
        self.store.write_artifact('diagnosis', 'x')
        lines = path.read_text().splitlines()
        record = json.loads(lines[-1])
        for field, value in [('path', '../../outside'), ('version', 2), ('version', True), ('sha256', 'x'),
                             ('byte_size', True), ('extra', 'unsupported'), ('format', 'json')]:
            with self.subTest(field=field, value=value):
                bad = {**record, field: value}
                path.write_text(lines[0] + '\n' + json.dumps(bad) + '\n')
                with self.assertRaises(WorkContractError): WorkWorkspace.open(self.root)

    def test_catalog_conflict_and_symlink_protection(self):
        changed = catalog(); changed['diagnosis'] = ArtifactSpec('markdown')
        with self.assertRaisesRegex(WorkContractError, 'catalog mismatch'): WorkWorkspace.open(self.root, changed)
        outside = Path(self.tmp.name) / 'outside'; outside.mkdir()
        (self.root / 'artifacts').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(WorkContractError, 'symlink'): self.store.write_artifact('diagnosis', 'x')
        self.assertEqual(list(outside.iterdir()), [])

    def test_stale_lock_and_orphan_files_never_overwritten(self):
        lock = self.root / '.write.lock'; lock.write_text('')
        with self.assertRaisesRegex(WorkContractError, 'stale lock'): self.store.initialize_request('a')
        lock.unlink()
        target = self.root / 'artifacts/diagnosis/000001.txt'; target.parent.mkdir(parents=True)
        target.write_text('orphan')
        with self.assertRaisesRegex(WorkContractError, 'overwritten'): self.store.write_artifact('diagnosis', 'new')
        self.assertEqual(target.read_text(), 'orphan')
        self.assertIsNone(self.store.latest_artifact('diagnosis'))

    def test_failed_manifest_append_does_not_adopt_orphan(self):
        with patch.object(WorkWorkspace, '_append', side_effect=OSError('disk full')):
            with self.assertRaises(WorkContractError): self.store.write_artifact('diagnosis', 'orphan')
        self.assertIsNone(WorkWorkspace.open(self.root).latest_artifact('diagnosis'))
        with self.assertRaisesRegex(WorkContractError, 'overwritten'): self.store.write_artifact('diagnosis', 'retry')

    def test_request_text_format_and_empty_request_are_exact(self):
        other = WorkWorkspace.open(Path(self.tmp.name) / 'text', {'request': ArtifactSpec('text', source='request')})
        record = other.initialize_request('')
        self.assertEqual(record.path, 'request.md')
        self.assertEqual(record.byte_size, 0)
        self.assertEqual(other.read_artifact('request'), '')

    def test_missing_file_and_handoff_manifest_sequence_are_errors(self):
        record = self.store.write_artifact('diagnosis', 'evidence')
        (self.root / record.path).unlink()
        with self.assertRaises(WorkContractError): self.store.read_artifact('diagnosis')
        self.store.write_handoff(handoff())
        path = self.root / 'manifest.jsonl'
        lines = path.read_text().splitlines()
        record = json.loads(lines[-1]); record['sequence'] = 2
        path.write_text('\n'.join([*lines[:-1], json.dumps(record)]) + '\n')
        with self.assertRaisesRegex(WorkContractError, 'sequence'): WorkWorkspace.open(self.root)

    def test_unexpected_handoff_fields_are_not_transcripts(self):
        self.store.write_handoff(handoff())
        path = self.root / 'handoffs/000001.json'
        data = json.loads(path.read_text()); data['chat_history'] = ['not allowed']
        raw = json.dumps(data).encode(); path.write_bytes(raw)
        # Even when a caller replaces the manifest digest, strict payload validation holds.
        import hashlib
        manifest = self.root / 'manifest.jsonl'; lines = manifest.read_text().splitlines()
        record = json.loads(lines[-1]); record.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
        manifest.write_text('\n'.join([*lines[:-1], json.dumps(record)]) + '\n')
        with self.assertRaisesRegex(WorkContractError, 'handoff fields'): self.store.handoffs()

    def test_storage_is_deterministic_and_has_no_absolute_paths(self):
        other = WorkWorkspace.open(Path(self.tmp.name) / 'second', catalog())
        for store in (self.store, other):
            store.initialize_request('request')
            store.write_artifact('diagnosis', 'diagnosis')
            store.write_handoff(handoff())
        first = (self.root / 'manifest.jsonl').read_bytes()
        self.assertEqual(first, (other.root / 'manifest.jsonl').read_bytes())
        self.assertNotIn(str(self.root).encode(), first)
        self.assertEqual((self.root / 'handoffs/000001.json').read_bytes(),
                         (other.root / 'handoffs/000001.json').read_bytes())


class ArtifactCatalogTests(unittest.TestCase):
    def load(self, data):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'bundle.yaml'; path.write_text(yaml.safe_dump(data))
            return load_workflow_bundle(path)

    def test_canonical_and_compilation_metadata(self):
        bundle = load_workflow_bundle(EXAMPLE)
        for name in ('request', 'microspec', 'bug_spec', 'reproduction_evidence', 'diagnosis',
                     'fix_microspec', 'revised_fix_microspec', 'verifier_evidence'):
            self.assertIn(name, bundle.artifacts)
        class Fake:
            def run(self, request): raise AssertionError('compilation must not execute')
        compiled = compile_workflow(bundle, 'feature', lambda binding: Fake())
        self.assertEqual(compiled.artifacts, bundle.artifacts)
        self.assertEqual(compiled.artifacts['microspec'], ArtifactSpec('yaml', 'microspec'))
        with self.assertRaises(TypeError): compiled.artifacts['new'] = ArtifactSpec('text')
        self.assertEqual(compiled.plan[compiled.entry_states['IMPLEMENT']].declaration.consumes, ('request', 'microspec'))

    def test_catalog_rejects_invalid_contracts(self):
        for definition in ({'format': 'png'}, {'format': 'yaml', 'schema': 'plugin'},
                           {'format': 'text', 'schema': 'microspec'}, {'format': 'json', 'extra': 1},
                           {'format': 1}, {'format': 'markdown', 'source': 'agent'}, None):
            with self.subTest(definition=definition):
                data = _bundle_yaml_data(EXAMPLE.read_text()); data['artifacts']['diagnosis'] = definition
                with self.assertRaises(FlowConfigError): self.load(data)
        for definition in ({'format': 'yaml', 'source': 'request'}, {'format': 'markdown'},
                           {'format': 'text', 'source': 'request', 'schema': 'microspec'}):
            data = _bundle_yaml_data(EXAMPLE.read_text()); data['artifacts']['request'] = definition
            with self.assertRaises(FlowConfigError): self.load(data)
        data = _bundle_yaml_data(EXAMPLE.read_text()); data['artifacts']['other'] = {'format': 'text', 'source': 'request'}
        with self.assertRaises(FlowConfigError): self.load(data)
        data = _bundle_yaml_data(EXAMPLE.read_text()); del data['artifacts']['request']
        with self.assertRaises(FlowConfigError): self.load(data)

    def test_references_and_request_production_rejected(self):
        for key, value in [('consumes', ['typo']), ('produces', ['typo']), ('produces', ['request'])]:
            data = _bundle_yaml_data(EXAMPLE.read_text()); data['flows']['feature']['states']['SPECIFY'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(FlowConfigError): self.load(data)
        data = _bundle_yaml_data(EXAMPLE.read_text()); data['artifacts']['../bad'] = {'format': 'text'}
        with self.assertRaises(FlowConfigError): self.load(data)

    def test_artifact_free_bundle_still_valid(self):
        data = _bundle_yaml_data(EXAMPLE.read_text()); del data['artifacts']
        del data['profiles']['command']['command']['capture']
        for flow in data['flows'].values():
            for state in flow['states'].values():
                state.pop('consumes', None); state.pop('produces', None)
        self.assertEqual(dict(self.load(data).artifacts), {})
        data['flows']['feature']['states']['SPECIFY']['consumes'] = ['request']
        with self.assertRaises(FlowConfigError): self.load(data)

"""V2-003E: complete handoff diagnostics and fake-process TRIAGE conformance."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from runtime import InvocationRequest, WorkUnit
from switchyard.context_transport import StageHandoff, StageHandoffContract, workspace_resolver
from switchyard.provider_process import ProviderExecutionError
from switchyard.provider_stages import (ProviderRuntimeConfig, ProviderStageExecutorResolver,
                                       parse_stage_result, render_invocation, result_schema)
from switchyard.work_contract import (HANDOFF_BODY_FIELDS, HANDOFF_COLLECTION_FIELDS,
    HandoffRecord, WorkContractError, WorkWorkspace, handoff_guidance)
from switchyard.workflow_bundle import load_workflow_bundle, ArtifactSpec
from switchyard.workflow_compiler import compile_workflow

CANONICAL = Path(__file__).resolve().parents[1]/'examples/delivery/workflows-v2.yaml'
MARKER = 'REJECTED-HANDOFF-8bd76'
SPEC = json.dumps(dict(id='BUG-1', goal='Preserve FIFO', scope=[], constraints=[],
                       acceptance=['FIFO'], verification=['tests'], non_goals=[]))


def record(**changes):
    return replace(HandoffRecord('bugfix', 'TRIAGE', 'triager', 0, 'ready', 'summary'), **changes)


def stage_value(**changes):
    return dict(outcome='ready', produced_artifacts=[dict(name='bug_spec', content=SPEC)],
                handoff=dict(summary='summary', findings=[], evidence=[], changed_assumptions=[], blockers=[]),
                diagnostics=None) | changes


def all_errors(exc):
    """Inspect both context and cause, including suppressed exception contexts."""
    seen, pending, text = set(), [exc], []
    while pending:
        value = pending.pop()
        if value is None or id(value) in seen:
            continue
        seen.add(id(value)); text.append(str(value))
        diagnostic = getattr(value, 'diagnostics', None)
        if diagnostic is not None:
            text.append(diagnostic.utf8.decode('utf-8'))
        pending.extend((value.__cause__, value.__context__))
    return '\n'.join(text)


class HandoffDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = WorkWorkspace.open(self.root/'work', {
            'request': ArtifactSpec('text', source='request'), 'bug_spec': ArtifactSpec('text')})
        self.workspace.initialize_request('request')
        self.before = self.snapshot()

    def snapshot(self):
        return {p.relative_to(self.workspace.root).as_posix():p.read_bytes()
                for p in self.workspace.root.rglob('*') if p.is_file()}

    def rejected(self, changes, expected):
        with self.assertRaises(WorkContractError) as caught:
            self.workspace.write_handoff(record(**changes))
        self.assertEqual(str(caught.exception), expected)
        self.assertNotIn(MARKER, all_errors(caught.exception))
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.workspace.handoffs(), ())
        self.assertNotIn(MARKER.encode(), b''.join(self.snapshot().values()))

    def test_all_record_identifier_fields(self):
        for field in ('flow', 'logical_state', 'role', 'profile_name'):
            for value, category in ((None,'null'), (True,'boolean'), (2,'integer'),
                                    ({MARKER:1},'mapping'), ('','empty string'),
                                    (MARKER+' path','invalid format')):
                if field == 'profile_name' and value is None:
                    continue
                with self.subTest(field=field, value=category):
                    self.rejected({field:value}, f'handoff.{field}: expected logical identifier, got {category}')

    def test_all_scalar_and_item_types_and_utf8(self):
        cases = [(None,'null'), (True,'boolean'), (1,'integer'), (1.2,'number'),
                 ({MARKER:1},'mapping'), ([MARKER],'sequence'), ('','empty string'),
                 (' \t','empty string'), (object(),'unsupported value')]
        for field in ('summary','outcome', *HANDOFF_COLLECTION_FIELDS):
            for value, category in cases:
                location = field if field in ('summary','outcome') else field+'[1]'
                payload = value if field in ('summary','outcome') else ['bug_spec',value]
                with self.subTest(field=field, category=category):
                    self.rejected({field:payload}, f'handoff.{location}: expected non-empty string, got {category}')
            location = field if field in ('summary','outcome') else field+'[1]'
            payload = MARKER+'\ud800'
            if field not in ('summary','outcome'):
                payload = ['bug_spec',payload]
            self.rejected({field:payload}, f'handoff.{location}: expected UTF-8 string, got invalid UTF-8')

    def test_collection_shapes_preserve_location_in_both_types(self):
        for field in HANDOFF_COLLECTION_FIELDS:
            for value, kind in (({MARKER:1},'mapping'), (MARKER,'string'), (None,'null'), (4,'integer')):
                self.rejected({field:value}, f'handoff.{field}: expected sequence, got {kind}')
                if field != 'produced_artifacts':
                    with self.assertRaisesRegex(WorkContractError, f'handoff.{field}: expected sequence, got {kind}'):
                        StageHandoff('summary', **{field:value})

    def test_identifier_syntax_and_membership_at_original_index(self):
        invalid = [MARKER+' path', '/'+MARKER, MARKER+'\\file', MARKER+'\0',
                   '['+MARKER+']', '1'+MARKER, '_'+MARKER, '.'+MARKER,
                   MARKER+':label', MARKER+'\n', 'é'+MARKER]
        for field in ('evidence','produced_artifacts'):
            for value in invalid:
                self.rejected({field:['bug_spec',value]},
                              f'handoff.{field}[1]: expected logical identifier, got invalid format')
            self.rejected({field:['bug_spec',MARKER]},
                          f'handoff.{field}[1]: expected declared artifact identifier, got undeclared reference')
        self.rejected({'produced_artifacts':['bug_spec','request']},
                      'handoff.produced_artifacts[1]: expected produced artifact identifier, got immutable request reference')

    def test_integer_metadata_and_store_sequence(self):
        for field in ('rung_index','version','sequence'):
            expected = 'integer equal to 1' if field == 'version' else 'non-negative integer'
            for value, kind in ((True,'boolean'), (None,'null'), (MARKER,'string'), (1.0,'number')):
                self.rejected({field:value}, f'handoff.{field}: expected {expected}, got {kind}')
            self.rejected({field:-1}, f'handoff.{field}: expected {expected}, got out of range')
        self.rejected({'version':2}, 'handoff.version: expected integer equal to 1, got out of range')
        self.rejected({'sequence':1}, 'handoff.sequence: expected 0 for a new handoff, got store-assigned sequence')

    def test_bounds_and_root(self):
        for field in ('summary','outcome', *HANDOFF_COLLECTION_FIELDS):
            location = field if field in ('summary','outcome') else field+'[1]'
            value = MARKER+'x'*8192
            self.rejected({field:value if field in ('summary','outcome') else ['bug_spec',value]},
                          f'handoff.{location}: expected at most 8192 characters, got length exceeded')
        for field in HANDOFF_COLLECTION_FIELDS:
            self.rejected({field:['bug_spec']*65}, f'handoff.{field}: expected at most 64 items, got length exceeded')
        with self.assertRaisesRegex(WorkContractError, 'handoff: expected HandoffRecord, got mapping'):
            self.workspace.validate_handoff({MARKER:1})

    def test_valid_boundaries_and_no_new_rules(self):
        # Catalog membership does not require a committed artifact; duplicates remain legal.
        saved = self.workspace.write_handoff(record(summary='x'*8192, outcome='prose with spaces\0',
            findings=['text\0']*64, evidence=['request','bug_spec','bug_spec'], produced_artifacts=['bug_spec']))
        self.assertEqual(saved.sequence, 1)
        self.assertIsNone(self.workspace.latest_artifact('bug_spec'))
        self.assertEqual(self.workspace.handoffs(), (saved,))
        name = 'A'+'a'*8191
        long = WorkWorkspace.open(self.root/'long', {'request':ArtifactSpec('text',source='request'),
                                                   name:ArtifactSpec('text'), 'A_b-1.ext':ArtifactSpec('text')})
        long.write_handoff(record(evidence=[name,'A_b-1.ext']))


class HandoffTransportDiagnosticsTests(unittest.TestCase):
    def test_exact_body_fields_and_transport_types(self):
        body = stage_value()['handoff']
        for field in body:
            missing = dict(body); del missing[field]
            with self.assertRaisesRegex(ProviderExecutionError, f'handoff.{field}: expected required field, got missing field'):
                parse_stage_result(stage_value(handoff=missing))
        for handoff, location, expected in (
            ({**body, MARKER:'extra'}, '', 'exact body fields, got unexpected fields'),
            (MARKER, '', 'body object or null, got string'),
            ({**body,'summary':None}, '.summary', 'non-empty string, got null'),
            ({**body,'findings':{MARKER:1}}, '.findings', 'sequence, got mapping'),
            ({**body,'findings':['ok',{MARKER:1}]}, '.findings[1]', 'non-empty string, got mapping'),
            ({**body,'summary':MARKER+'\ud800'}, '.summary', 'UTF-8 string, got invalid UTF-8')):
            with self.assertRaises(ProviderExecutionError) as caught:
                parse_stage_result(stage_value(handoff=handoff))
            self.assertEqual(str(caught.exception), 'handoff'+location+': expected '+expected)
            self.assertNotIn(MARKER, all_errors(caught.exception))

    def test_transport_semantics_and_schema_unchanged(self):
        body = stage_value()['handoff']
        # Semantic identifiers are still checked downstream, not in transport.
        result = parse_stage_result(stage_value(handoff={**body,'summary':' ', 'evidence':[MARKER+' path']}))
        self.assertEqual(result.handoff.evidence, (MARKER+' path',))
        self.assertEqual(result.handoff.summary, ' ')
        schema = result_schema()['properties']['handoff']['anyOf'][0]
        self.assertEqual(schema['required'], list(StageHandoff.__dataclass_fields__))
        self.assertEqual(schema['properties'], {f.name:{'type':'array','items':{'type':'string'}} if f.sequence
                                              else {'type':'string'} for f in HANDOFF_BODY_FIELDS})
        self.assertFalse(schema['additionalProperties'])
        self.assertIsNone(parse_stage_result(stage_value(handoff=None)).handoff)


@unittest.skipIf(os.name == 'nt', 'fake CLI needs POSIX executable scripts')
class TriageHandoffTests(unittest.TestCase):
    def prepare(self, root, evidence):
        bundle = load_workflow_bundle(CANONICAL)
        workspace = WorkWorkspace.open(root/'work', bundle.artifacts)
        workspace.initialize_request('request')
        fake = (Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        bin = root/'bin'; bin.mkdir()
        for name in ('claude','codex','python3'):
            path = bin/name; path.write_text('#!'+sys.executable+'\n'+fake); path.chmod(0o755)
        result = stage_value()
        result['handoff']['evidence'] = evidence
        response = root/'response.json'
        response.write_text(json.dumps(dict(type='result', subtype='success', is_error=False, structured_output=result)))
        env = {'PATH':str(bin), 'FAKE_MODE':'scripted', 'FAKE_EXIT':'0',
               'FAKE_STDOUT_FILE':str(response), 'FAKE_LOG':str(root/'calls.jsonl')}
        config = ProviderRuntimeConfig(str(root), str(bin/'claude'), str(bin/'codex'), env)
        compiled = compile_workflow(bundle, 'bugfix', workspace_resolver(bundle, 'bugfix', workspace,
                   ProviderStageExecutorResolver(bundle.profiles, config)))
        executor = compiled.executors['@v2:bugfix:TRIAGE:0']
        return workspace, executor

    def test_attempt_5_failure_class_successful_process_then_no_commits(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace, executor = self.prepare(root, ['request', MARKER+' path'])
            before = {p.name:p.read_bytes() for p in workspace.root.iterdir() if p.is_file()}
            with self.assertRaises(WorkContractError) as caught:
                executor.run(InvocationRequest(WorkUnit('request')))
            self.assertEqual(str(caught.exception), 'bugfix.TRIAGE[0]: handoff.evidence[1]: '
                             'expected logical identifier, got invalid format')
            self.assertNotIn(MARKER, all_errors(caught.exception))
            self.assertEqual(len((root/'calls.jsonl').read_text().splitlines()), 1)
            self.assertEqual(workspace.versions('bug_spec'), ())
            self.assertEqual(workspace.handoffs(), ())
            after = {p.name:p.read_bytes() for p in workspace.root.rglob('*') if p.is_file()}
            self.assertEqual(after, before)
            self.assertNotIn(MARKER.encode(), b''.join(after.values()))

    def test_valid_equivalent_triage_commits_and_exact_prompt(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace, executor = self.prepare(root, ['request','bug_spec'])
            invocation = executor._context(InvocationRequest(WorkUnit('request')))
            expected_prompt = render_invocation(invocation)
            response = executor.run(InvocationRequest(WorkUnit('request')))
            self.assertEqual(response.result.outcome, 'ready')
            self.assertEqual(workspace.latest_artifact('bug_spec').version, 1)
            self.assertEqual(workspace.handoffs()[0].evidence, ('request','bug_spec'))
            calls = [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['input'], expected_prompt)
            self.assertIn('handoff key is required and nullable', expected_prompt)
            self.assertIn('Logical identifiers start with an ASCII letter', expected_prompt)
            self.assertIn('evidence entries must use only the exact logical artifact names in evidence_artifacts', expected_prompt)
            self.assertIn('Do not use filenames, paths, sentences, labels with spaces, Markdown references', expected_prompt)
            self.assertIn('Each item is a declared logical artifact name, not prose or a reference object.', expected_prompt)
            for field in HANDOFF_BODY_FIELDS:
                self.assertIn('- '+field.name+': required;', expected_prompt)
            section = expected_prompt.split('Valid evidence artifact identifiers for this invocation')[1].split('\n\n')[0]
            self.assertEqual(section.splitlines()[1:], ['- '+n for n in sorted(workspace.artifacts)])
            self.assertIn('Prose is allowed, including repository paths and observations.', expected_prompt)

    def test_empty_catalog_and_unsupported_guidance(self):
        self.assertIn('(none; use evidence: [])', handoff_guidance(StageHandoffContract(True, ())))
        self.assertIn('return null', handoff_guidance(StageHandoffContract(False, (), supported=False)))


if __name__ == '__main__':
    unittest.main()

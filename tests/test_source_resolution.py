"""Offline source ingestion through real workspace/context and recovery boundaries."""
from dataclasses import replace, asdict
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError, URLError

from runtime import WorkUnit, run, ExecutionError, InvocationRequest
from switchyard.context_transport import workspace_resolver
from switchyard.github_sources import GitHubSourceResolver, HTTP_TIMEOUT, MAX_RESPONSE_BYTES, http_get, _NoRedirect
from switchyard.provider_stages import render_invocation
from switchyard.source_resolution import (SourceResolutionError, canonical_json, detect_references,
                                         prepare_sources)
from switchyard.work_contract import WorkWorkspace, WorkContractError
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow
from test_context_transport import Fake
from test_workflow_compiler import EXAMPLE
import test_recovery
from switchyard.recovery import execute_recoverable_v2_work, recovery_status, resume_v2_work

URL = 'https://github.com/Antonio-Ingegnere/adeo/issues/34'
ISSUE = json.loads((Path(__file__).parent/'fixtures/source_issue_34.json').read_text())


class Transport:
    def __init__(self, payload=None, comments=()):
        self.payload = ISSUE if payload is None else payload
        self.comments, self.calls = comments, []

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers), timeout, max_bytes))
        if '/comments?' in url:
            page = int(url.rsplit('=', 1)[1])
            value = self.comments[(page-1)*10:page*10]
        else:
            value = self.payload
        return json.dumps(value).encode()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = load_workflow_bundle(EXAMPLE)
        self.transport = Transport()
        self.resolver = GitHubSourceResolver(self.transport, environment={}, clock=lambda: '2026-10-19T00:00:00Z')

    def workspace(self, request, suffix='work'):
        workspace = WorkWorkspace.open(self.root/suffix, self.bundle.artifacts)
        workspace.initialize_request(request)
        return workspace

    def resolve(self, url=URL):
        return self.resolver.resolve(self.resolver.reference(url)).document()

    def test_real_regression_entry_context_durable_original_and_later_handoff(self):
        request = URL + '\r\n'
        workspace = self.workspace(request)
        fake = Fake(self.bundle, 'feature')
        resolver = workspace_resolver(self.bundle, 'feature', workspace, fake, source_resolvers=(self.resolver,))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(fake.calls, [])
        compiled = compile_workflow(self.bundle, 'feature', resolver)
        result = run(WorkUnit(request), compiled.flow, compiled.executors)
        self.assertEqual(result.terminal_state, 'READY_FOR_MANUAL_QA')
        specify = fake.calls[0]
        self.assertEqual(specify.binding.logical_state, 'SPECIFY')
        content = json.loads(specify.sources[0].content)['sources'][0]
        self.assertEqual(content['content']['title'], 'TQL-001 — SQL-inspired query syntax extensions')
        for phrase in ('Existing saved queries continue to work.', 'Board moves', 'IN (...)', 'No raw SQL'):
            self.assertIn(phrase, content['content']['body'])
        self.assertIn('https://github.com/antonio-ingegnere/adeo/issues/34', render_invocation(specify))
        self.assertIn('SQL-inspired query syntax extensions', render_invocation(specify))
        self.assertTrue(all(not call.sources for call in fake.calls[1:]))
        self.assertEqual((workspace.root/'request.md').read_bytes(), request.encode())
        stored = WorkWorkspace.open(workspace.root/'sources')
        self.assertEqual(stored.read_artifact('resolved_sources'), specify.sources[0].content)
        self.assertIsNone(specify.sources[0].record.producer)
        self.assertEqual(specify.sources[0].record.sha256, hashlib.sha256(specify.sources[0].content.encode()).hexdigest())
        with patch.object(self.resolver, 'resolve', side_effect=AssertionError('no refetch')):
            prepare_sources(workspace, (self.resolver,))
        self.assertEqual(len(stored.versions('resolved_sources')), 1)

    def test_detection_order_prose_canonical_duplicates_and_unsupported(self):
        request = f'Implement ({URL}?x=1#body), then https://github.com/o/r/pull/7. Also {URL.lower()}/#a and https://example.com/x'
        refs = detect_references(request, (self.resolver,))
        self.assertEqual([r.source_type for _, r in refs], ['github_issue', 'github_pull_request'])
        self.assertEqual([r.canonical_url for _, r in refs], [URL.lower(), 'https://github.com/o/r/pull/7'])
        for index, request in enumerate(('add keyboard shortcut X', 'https://example.com/foo', 'https://github.com/o/r', 'https://github.com.evil/o/r/issues/1')):
            self.assertIsNone(prepare_sources(self.workspace(request, str(index)), (self.resolver,)))
        self.assertEqual(self.transport.calls, [])

    def test_multiple_resolve_only_once_and_preserve_order(self):
        t = self.transport
        def transport(url, *args):
            t.payload = dict(ISSUE, number=int(url.rsplit('/', 1)[1]))
            return t(url, *args)
        self.resolver.transport = transport
        store = prepare_sources(self.workspace(f'{URL} {URL} https://github.com/o/r/pull/7?tab=foo'), (self.resolver,))
        docs = json.loads(store.read_artifact('resolved_sources'))['sources']
        self.assertEqual([d['content']['number'] for d in docs], [34, 7])
        self.assertEqual(len(t.calls), 2)
        self.assertTrue(t.calls[1][0].endswith('/pulls/7'))

    def test_issue_and_pr_top_level_comments(self):
        for path in ('issues', 'pull'):
            with self.subTest(path=path):
                self.transport.payload = dict(ISSUE, comments=1)
                self.transport.comments = [dict(id=12, body='Acceptance: preserve AST semantics.')]
                doc = self.resolve(URL.replace('issues', path))
                self.assertEqual(doc['content']['comments'][0]['body'], 'Acceptance: preserve AST semantics.')
                self.assertIn('/issues/34/comments?', self.transport.calls[-1][0])

    def test_stable_content_hash_excludes_retrieval_and_original_url(self):
        first = self.resolve()
        self.resolver.clock = lambda: '2026-10-20T00:00:00Z'
        second = self.resolve(URL+'?a=1#body')
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        self.assertNotEqual(first['retrieved_at'], second['retrieved_at'])
        self.assertEqual(first['content_sha256'], hashlib.sha256(canonical_json(first['content']).encode()).hexdigest())
        self.transport.payload = dict(ISSUE, body='changed')
        self.assertNotEqual(first['content_sha256'], self.resolve()['content_sha256'])

    def test_classified_failures_block_all_provider_resolution(self):
        cases = [(HTTPError('url', code, 'SECRET remote response', {}, None), kind) for code, kind in
                 [(401, 'authentication_required'), (403, 'forbidden_or_rate_limited'), (404, 'not_found'), (500, 'http_error')]]
        cases += [(TimeoutError('SECRET'), 'timeout'), (URLError(TimeoutError('SECRET')), 'timeout'),
                  (URLError('SECRET'), 'network_error'), (OSError('SECRET'), 'network_error')]
        for index, (exc, kind) in enumerate(cases):
            with self.subTest(kind=kind):
                self.resolver.transport = lambda *args, error=exc: (_ for _ in ()).throw(error)
                fake = Fake(self.bundle, 'feature')
                with self.assertRaises(SourceResolutionError) as caught:
                    workspace_resolver(self.bundle, 'feature', self.workspace(URL, str(index)), fake,
                                       source_resolvers=(self.resolver,))
                self.assertEqual(caught.exception.classification, kind)
                self.assertIn(URL.lower(), str(caught.exception))
                self.assertNotIn('SECRET', str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(fake.bindings, [])
                self.assertEqual(fake.calls, [])

    def test_malformed_and_response_bounds(self):
        for raw, kind in [(b'invalid SECRET', 'malformed_response'), (b'{}', 'malformed_response'),
                          (b'[]', 'malformed_response'), (b'x'*(MAX_RESPONSE_BYTES+1), 'response_size_limit')]:
            self.resolver.transport = lambda *args, value=raw: value
            with self.assertRaises(SourceResolutionError) as caught:
                self.resolve()
            self.assertEqual(caught.exception.classification, kind)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_auth_precedence_redaction_and_anonymous(self):
        for env, expected in [({}, None), ({'GITHUB_TOKEN':'secret-B'}, 'secret-B'),
                              ({'GH_TOKEN':'secret-A', 'GITHUB_TOKEN':'secret-B'}, 'secret-A')]:
            self.resolver.environment = env
            self.transport.payload = dict(ISSUE, body='echo '+str(expected))
            doc = self.resolve()
            headers = self.transport.calls[-1][1]
            self.assertEqual(headers.get('Authorization'), None if expected is None else 'Bearer '+expected)
            if expected:
                self.assertNotIn(expected, canonical_json(doc))
            self.assertEqual(self.transport.calls[-1][2:], (HTTP_TIMEOUT, MAX_RESPONSE_BYTES))

    def test_comment_page_count_and_field_clipping(self):
        self.transport.payload = dict(ISSUE, comments=100, body='é'*5000, title='x'*2000)
        self.transport.comments = [dict(id=i, body='é'*1100) for i in range(100)]
        doc = self.resolve()['content']
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(len(doc['comments']), 20)
        self.assertEqual(len(doc['body'].encode()), 8192)
        self.assertEqual(len(doc['comments'][0]['body'].encode()), 2048)
        self.assertTrue(doc['truncation']['comments_truncated'])
        self.assertEqual(len(doc['truncation']['fields']), 22)

    def test_reference_and_aggregate_limits_fail_before_provider(self):
        with self.assertRaisesRegex(SourceResolutionError, 'reference_limit'):
            prepare_sources(self.workspace(' '.join(f'https://github.com/o/r/issues/{n}' for n in range(1,6))), (self.resolver,))
        self.assertEqual(self.transport.calls, [])
        self.transport.payload = dict(ISSUE, comments=20)
        self.transport.comments = [dict(id=i, body='x'*2048) for i in range(20)]
        with self.assertRaisesRegex(SourceResolutionError, 'source_size_limit'):
            prepare_sources(self.workspace(URL, 'large'), (self.resolver,))
        self.assertFalse((self.root/'large/sources/manifest.jsonl').exists())

    def test_plain_delivery_never_fetches(self):
        request = 'Add keyboard shortcut X; see https://example.com/foo'
        workspace = self.workspace(request)
        fake = Fake(self.bundle, 'feature')
        with patch('switchyard.github_sources.http_get', side_effect=AssertionError('network')):
            compiled = compile_workflow(self.bundle, 'feature', workspace_resolver(self.bundle, 'feature', workspace, fake))
            run(WorkUnit(request), compiled.flow, compiled.executors)
        self.assertFalse((workspace.root/'sources').exists())
        self.assertTrue(all(not c.sources for c in fake.calls))

    def test_truncation_is_persisted_and_supplied(self):
        self.transport.payload = dict(ISSUE, body='é'*5000, comments=100)
        self.transport.comments = [dict(id=i, body='short comment') for i in range(100)]
        workspace = self.workspace(URL)
        fake = Fake(self.bundle, 'feature')
        compiled = compile_workflow(self.bundle, 'feature', workspace_resolver(
            self.bundle, 'feature', workspace, fake, source_resolvers=(self.resolver,)))
        run(WorkUnit(URL), compiled.flow, compiled.executors)
        content = json.loads(fake.calls[0].sources[0].content)['sources'][0]['content']
        self.assertTrue(content['truncation']['comments_truncated'])
        self.assertEqual(content['truncation']['fields'][0]['field'], 'body')
        self.assertEqual(len(content['body'].encode()), 8192)

    def test_http_transport_bounds_deadline_and_no_redirect(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        response.read1.side_effect = [b'12345']
        with patch('switchyard.github_sources.build_opener', return_value=opener):
            with self.assertRaises(OverflowError):
                http_get('https://api.github.com/test', {}, 10, 4)
            self.assertEqual(response.read1.call_args.args, (5,))
            with patch('switchyard.github_sources.time.monotonic', side_effect=[0, 11]):
                with self.assertRaises(TimeoutError):
                    http_get('https://api.github.com/test', {}, 10, 4)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid'))

    def test_source_snapshot_counts_toward_context_limit(self):
        workspace = self.workspace(URL)
        fake = Fake(self.bundle, 'feature')
        compiled = compile_workflow(self.bundle, 'feature', workspace_resolver(
            self.bundle, 'feature', workspace, fake, source_resolvers=(self.resolver,)))
        wrapper = next(e for e in compiled.executors.values() if e.binding.logical_state == 'SPECIFY')
        request = InvocationRequest(WorkUnit(URL))
        plain = replace(wrapper, source_workspace=None)
        payload = asdict(plain._context(request))
        del payload['output_contract'], payload['sources'], payload['binding']['executor_allowed_outcomes']
        size = len(canonical_json(payload).encode())
        replace(plain, max_context_bytes=size)._context(request)
        with self.assertRaisesRegex(WorkContractError, 'context exceeds'):
            replace(wrapper, max_context_bytes=size).run(request)
        self.assertEqual(fake.calls, [])

    def test_tampered_source_fails_without_refetch(self):
        workspace = self.workspace(URL)
        store = prepare_sources(workspace, (self.resolver,))
        (store.root/store.latest_artifact('resolved_sources').path).write_text('{}')
        with self.assertRaises(WorkContractError):
            prepare_sources(workspace, (self.resolver,))
        self.assertEqual(len(self.transport.calls), 1)


class RecoverySourceTests(unittest.TestCase):
    _base = test_recovery.RecoveryTests
    setUp, git, save_config = _base.setUp, _base.git, _base.save_config

    def test_startup_failure_no_events_no_provider_no_new_terminal(self):
        with patch('switchyard.github_sources.http_get', side_effect=HTTPError('url', 401, 'secret', {}, None)):
            with self.assertRaises(SourceResolutionError):
                execute_recoverable_v2_work(self.text, 'feature', URL, self.work, self.config)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.work/'events.jsonl').exists())
        status = recovery_status(self.work)
        self.assertEqual(status['terminal'], 'INTERRUPTED')
        self.assertEqual(status['reason'], 'execution_interrupted')
        self.assertFalse(status['resume_allowed'])
        self.assertIn('authentication_required', status['diagnostic'])
        self.assertNotIn('secret', status['diagnostic'])

    def test_real_provider_prompt_and_durable_secret_exclusion(self):
        # Finish without the fixture's intentional IMPLEMENT timeout.
        self.file.write_text('partial')
        self.data['agents']['code']['timeout'] = 5
        self.text = self.save_config()
        transport = Transport(dict(ISSUE, body=ISSUE['body']+' echoed-auth-secret'))
        with patch('switchyard.github_sources.http_get', transport), patch.dict('os.environ', {'GH_TOKEN':'echoed-auth-secret'}):
            result = execute_recoverable_v2_work(self.text, 'feature', URL, self.work, self.config)
        self.assertEqual(result.result.terminal_state, 'READY_FOR_MANUAL_QA')
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn('SQL-inspired query syntax extensions', calls[0]['input'])
        self.assertNotIn('RESOLVED REQUEST SOURCES', calls[1]['input'])
        for path in list(self.work.rglob('*')) + [self.log]:
            if path.is_file():
                self.assertNotIn(b'echoed-auth-secret', path.read_bytes(), str(path))

    def test_provider_recovery_reuses_exact_source_snapshot_offline(self):
        transport = Transport()
        with patch('switchyard.github_sources.http_get', transport):
            with self.assertRaises(ExecutionError):
                execute_recoverable_v2_work(self.text, 'feature', URL, self.work, self.config)
        store = WorkWorkspace.open(self.work/'sources')
        snapshot = store.read_artifact('resolved_sources')
        self.data['agents']['code']['timeout'] = 5
        with patch('switchyard.github_sources.http_get', side_effect=AssertionError('must reuse sources')):
            execution = resume_v2_work(self.work, self.save_config(), self.config)
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(store.read_artifact('resolved_sources'), snapshot)
        self.assertEqual(len(transport.calls), 1)

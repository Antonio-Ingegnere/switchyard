"""INTEGRATION (#9) slice 5: review branch + pull request for an accepted delivery. Local bare remotes for Git;
an in-memory provider for the PR API; a fake `gh` executable for the GitHub adapter. No network."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

from switchyard import integration, publication, pull_request
from switchyard.cli import main
from switchyard.pull_request import (GitHubPullRequestProvider, PullRequestProvider, PullRequestProviderError,
                                     normalize_github_pull, pr_create_v2_work, pr_status_v2_work, pull_request_summary)
from switchyard.publication import remote_identity
from switchyard.recovery import recovery_status

from test_continuity import ROOT
from test_delivery_isolation import Crash
from test_delivery_suspend import LOGO
from test_push import PushBase

B = 'PR_BLOCKED'
HEAD = 'refs/heads/switchyard/work'
REPO = 'github.com/acme/widget'


class FakeProvider(PullRequestProvider):
    """In-memory PR host over a real bare repository: head SHAs are read from the repository itself."""
    name, identity = 'github', 'fake-pulls/1'

    def __init__(self, test, endpoints):
        self.test, self.endpoints, self.pulls, self.calls = test, endpoints, [], []
        self.lose_create_response = False

    def repository(self, push_url):
        return REPO if remote_identity(push_url) in self.endpoints else None

    def _head(self, ref):
        out = self.test.git('ls-remote', str(self.test.bare), ref)
        return out.split('\t')[0] if out else None

    def add(self, head_sha=None, base='refs/heads/main', state='OPEN', head_repository=REPO):
        pull = dict(provider='github', repository=REPO, number=len(self.pulls)+1, provider_id='PR_%d' % (len(self.pulls)+1),
                    url='https://github.com/acme/widget/pull/%d' % (len(self.pulls)+1), state=state, draft=False,
                    mergeability='UNKNOWN', head_ref=HEAD, head_sha=head_sha or self._head(HEAD), head_repository=head_repository,
                    base_ref=base, base_sha=self._head(base), merged_commit_sha=None, merged_at=None, closed_at=None, merged_by=None)
        self.pulls.append(pull)
        return pull

    def find(self, repository, head_ref, base_ref):
        self.calls.append(('find', repository, head_ref, base_ref))
        return [dict(p) for p in self.pulls if p['head_ref'] == head_ref and p['base_ref'] == base_ref]

    def create(self, repository, head_ref, base_ref, title, body):
        self.calls.append(('create', repository, head_ref, base_ref, title, body))
        pull = self.add()
        if self.lose_create_response:
            raise PullRequestProviderError('provider_unavailable', 'connection reset after the request was sent')
        return dict(pull)

    def get(self, repository, number):
        self.calls.append(('get', repository, number))
        pull = dict(self.pulls[number-1])
        if pull['state'] == 'OPEN':  # GitHub tracks the live branches of an open PR.
            pull.update(base_sha=self._head(pull['base_ref']), head_sha=self._head(HEAD) or pull['head_sha'])
        return pull

    def mutations(self):
        return [c for c in self.calls if c[0] == 'create']


class PullRequestBase(PushBase):
    def setUp(self):
        super().setUp()
        self.provider = FakeProvider(self, {'file://'+str(self.bare), 'file://localhost'+str(self.bare)})

    def ready(self, drift=False):
        """Accepted C; remote main == local main == T; latest assessment READY (or READY_WITH_DRIFT)."""
        self.c = self.accepted()
        if drift:
            self.advance(write={'keep.txt': 'upstream\n'})
        self.git('push', '-q', 'origin', 'main')
        self.t = self.git('rev-parse', 'main')
        self.a = self.assess()
        self.assertEqual(self.a['readiness'], 'READY_WITH_DRIFT' if drift else 'READY')
        return self.c

    def create(self, remote='origin', target=None, title=None):
        return pr_create_v2_work(self.work, remote, target, title, self.provider)

    def status(self):
        return pr_status_v2_work(self.work, self.provider)

    def remote_head(self):
        out = self.git('ls-remote', str(self.bare), HEAD)
        return out.split('\t')[0] if out else None

    def phases(self):
        return [r['phase'] for r in pull_request._history(pull_request._records(self.work))]

    def other_commit(self, parent, message='X'):
        return self.git('commit-tree', parent+'^{tree}', '-p', parent, '-m', message)


class CreateTests(PullRequestBase):
    # 1, 2, 4, 10, 11, 12, 29, 30
    def test_accepted_commit_is_published_as_the_review_head_and_one_pr_is_opened(self):
        c = self.ready(drift=True); calls = self.model_calls()
        with patch('switchyard.publication.push_v2_work', side_effect=AssertionError('direct push')), \
                patch('switchyard.integration.integrate_v2_work', side_effect=AssertionError('integrate')):
            r = self.create()
        self.assertEqual((self.remote_head(), self.remote_main()), (c, self.t))  # Head is C; the target is never pushed.
        self.assertEqual((r['phase'], r['created_by'], r['provider'], r['repository'], r['remote'], r['remote_identity']),
                         ('created', 'switchyard', 'github', REPO, 'origin', 'file://'+str(self.bare)))
        self.assertEqual((r['accepted_commit_sha'], r['delivery_base_sha'], r['assessment_id'], r['readiness'], r['target_ref'],
                          r['target_sha'], r['head_ref'], r['head_sha']),
                         (c, self.base, self.a['assessment_id'], 'READY_WITH_DRIFT', 'refs/heads/main', self.t, HEAD, c))
        self.assertEqual(r['pull_request'], dict(provider='github', repository=REPO, number=1, provider_id='PR_1',
                                                 url='https://github.com/acme/widget/pull/1', head_ref=HEAD, base_ref='refs/heads/main'))
        self.assertEqual((r['observed']['state'], r['observed']['head_drift'], r['observed']['base_sha']), ('OPEN', False, self.t))
        self.assertEqual(self.phases(), ['intent', 'head_published', 'created'])
        self.assertEqual([c[0] for c in self.provider.calls], ['find', 'create'])
        self.assertFalse((self.work/'publication').exists())  # Not the direct path: no push record ...
        self.assertIsNone(integration.integration_record(self.work))  # ... and no integration.
        self.assertEqual(self.model_calls(), calls)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['accepted_commit_sha']), ('ACCEPTED', c))
        self.assertEqual(status['pull_request'], dict(state='OPEN', provider='github', repository=REPO, number=1,
                                                      url='https://github.com/acme/widget/pull/1', head_ref=HEAD, head_sha=c,
                                                      accepted_commit_sha=c, target_ref='refs/heads/main', target_sha=self.t,
                                                      head_drift=False, merged_commit_sha=None))
        self.assertNotIn('remote_integration', status)

    # 13, 18
    def test_repeated_create_returns_the_same_pr_without_remote_calls(self):
        self.ready(); r = self.create()
        with patch('switchyard.publication._push', side_effect=AssertionError('republish')), \
                patch('switchyard.publication._remote_sha', side_effect=AssertionError('remote read')):
            self.assertEqual(self.create(), r)
        self.assertEqual((len(self.provider.pulls), len(self.provider.calls), self.phases()), (1, 2, ['intent', 'head_published', 'created']))
        e = self.blocked(lambda: self.create(target=self.base))
        self.assertEqual((e.reason, e.pull_request['number']), ('pull_request_exists', 1))

    # 3
    def test_a_moved_local_delivery_branch_does_not_change_the_published_head(self):
        c = self.ready(); x = self.other_commit(c)
        self.git('update-ref', HEAD, x)  # Someone moved the delivery branch after acceptance.
        r = self.create()
        self.assertEqual((self.remote_head(), r['head_sha'], self.provider.pulls[0]['head_sha']), (c, c, c))

    # 5
    def test_a_remote_head_already_at_the_accepted_commit_is_reused(self):
        c = self.ready(); self.git('push', '-q', 'origin', c+':'+HEAD)
        with patch('switchyard.publication._push', side_effect=AssertionError('must not push')):
            r = self.create()
        self.assertEqual((r['head_sha'], self.remote_head()), (c, c))
        self.assertEqual(pull_request._history(pull_request._records(self.work))[1]['head_published_by'], 'already_present')

    # 18
    def test_title_and_body_are_deterministic_bounded_provenance(self):
        c = self.ready(); r = self.create()
        self.assertEqual(r['title'], 'Switchyard delivery work')
        self.assertEqual(r['body'], 'Switchyard-Work: work\nSwitchyard-Accepted-Commit: %s\nSwitchyard-Base: %s\n'
                                    'Switchyard-Assessment: %s\nSwitchyard-Target: refs/heads/main %s\n' % (c, self.base, self.a['assessment_id'], self.t))
        import hashlib
        self.assertEqual(r['body_sha256'], hashlib.sha256(r['body'].encode()).hexdigest())
        self.assertEqual(self.provider.mutations()[0][4:], (r['title'], r['body']))
        request = (self.work/'request.md').read_text() if (self.work/'request.md').exists() else ''
        for line in request.splitlines():
            if len(line) > 12:
                self.assertNotIn(line, r['body'])
        self.assertEqual(pull_request._title('  Fix\tthe   logo ', 'w'), 'Fix the logo')
        for bad in ('', '   ', 'x'*257, 'a\x00b'):
            self.assertEqual(self.blocked(lambda: pull_request._title(bad, 'w')).reason, 'title_invalid: one line, 1-256 characters')


class GateTests(PullRequestBase):
    # 6
    def test_a_foreign_remote_head_blocks_without_overwrite(self):
        c = self.ready(); x = self.other_commit(c); self.git('push', '-q', 'origin', x+':'+HEAD)
        e = self.blocked(self.create)
        self.assertEqual((e.terminal, e.reason, e.pull_request), (B, 'pr_head_moved', dict(expected=c, actual=x)))
        self.assertEqual((self.remote_head(), self.provider.calls, (self.work/'pull_request').exists()), (x, [], False))

    # 7, 8
    def test_the_remote_target_must_still_be_the_assessed_target(self):
        self.ready(); q = self.remote_commit(self.t)
        e = self.blocked(self.create)
        self.assertEqual((e.reason, e.pull_request), ('target_moved', dict(expected_target_sha=self.t, actual_target_sha=q)))
        self.assertEqual((self.remote_head(), self.provider.calls, (self.work/'pull_request').exists()), (None, [], False))
        self.git('push', '-q', '--force', 'origin', self.t+':refs/heads/main')
        later = self.advance(write={'later.txt': 'L\n'}, message='L')  # The local target moved: the assessment is stale.
        e = self.blocked(self.create)
        self.assertEqual((e.reason, e.pull_request['actual_target_sha']), ('assessment_stale: run work integration-status again', later))
        self.assertIsNone(self.remote_head())

    # 9
    def test_blocked_or_missing_assessments_cannot_open_a_pr(self):
        self.accepted(); self.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.blocked(self.create).reason, 'assessment_missing: run work integration-status first')
        self.advance(write={'source.py': 'upstream edit', 'logo.png': LOGO[1:]}); self.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.assess()['readiness'], 'BLOCKED_CONFLICT')
        self.assertEqual(self.blocked(self.create).reason, 'assessment_not_ready: BLOCKED_CONFLICT')
        self.assertEqual((self.remote_head(), self.provider.calls, (self.work/'pull_request').exists()), (None, [], False))

    def test_an_already_integrated_delivery_gets_no_pr(self):
        self.integrated(); publication.push_v2_work(self.work, 'origin')  # The direct path published N.
        e = self.blocked(self.create)
        self.assertEqual((e.reason, e.pull_request['pushed_commit_sha']), ('already_integrated', self.n))
        self.assertEqual(self.assess()['readiness'], 'ALREADY_INTEGRATED')
        self.assertEqual(self.blocked(self.create).reason, 'already_integrated')
        self.assertEqual((self.remote_head(), self.provider.calls), (None, []))

    def test_unsupported_remotes_and_ineligible_works_are_refused(self):
        self.ready()
        other = self.root/'other.git'; subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(other)], check=True)
        self.git('remote', 'add', 'gitlab', str(other))
        e = self.blocked(lambda: self.create('gitlab'))
        self.assertEqual((e.terminal, e.reason, e.paths), (B, 'unsupported_pr_provider', ('file://'+str(other),)))
        self.git('config', '--add', 'remote.origin.pushurl', str(self.bare)); self.git('config', '--add', 'remote.origin.pushurl', str(other))
        self.assertEqual(self.blocked(self.create).reason, 'ambiguous_push_destination')
        self.assertEqual(self.blocked(lambda: self.create('nope')).reason, 'remote_not_configured')
        self.assertEqual((self.remote_head(), self.provider.calls, (self.work/'pull_request').exists()), (None, [], False))


class CrashTests(PullRequestBase):
    def crash_on(self, phase):
        original = pull_request._put
        def put(records, record):
            if record['phase'] == phase:
                raise Crash()
            return original(records, record)
        return patch('switchyard.pull_request._put', put)

    # 14
    def test_crash_after_head_publication_resumes_without_another_push(self):
        c = self.ready()
        with self.crash_on('head_published'), self.assertRaises(Crash):
            self.create()
        self.assertEqual((self.remote_head(), self.phases(), self.provider.calls), (c, ['intent'], []))
        self.assertEqual(pull_request_summary(self.work)['state'], 'PR_PENDING')
        self.remote_commit(self.t)  # A retry finishes the recorded intent: it is not re-gated.
        with patch('switchyard.publication._push', side_effect=AssertionError('must not push again')):
            r = self.create()
        self.assertEqual((r['created_by'], r['head_sha'], len(self.provider.pulls), self.phases()),
                         ('switchyard', c, 1, ['intent', 'head_published', 'created']))

    # 15
    def test_crash_after_the_provider_created_the_pr_reconciles_it(self):
        c = self.ready()
        with self.crash_on('created'), self.assertRaises(Crash):
            self.create()
        self.assertEqual((len(self.provider.pulls), self.phases()), (1, ['intent', 'head_published']))
        r = self.create()
        self.assertEqual((r['created_by'], r['pull_request']['number'], len(self.provider.pulls), len(self.provider.mutations())),
                         ('reconciled', 1, 1, 1))  # Adopted, never duplicated.
        self.assertEqual(self.create(), r)

    # 16
    def test_several_matching_prs_or_a_foreign_head_are_ambiguous(self):
        c = self.ready()
        with self.crash_on('created'), self.assertRaises(Crash):
            self.create()
        self.provider.add()  # Someone opened a second PR from the same head.
        e = self.blocked(self.create)
        self.assertEqual((e.reason, e.pull_request), ('pr_reconciliation_ambiguous', dict(numbers=[1, 2])))
        self.provider.pulls = [dict(self.provider.pulls[0], head_sha=self.other_commit(c))]
        self.assertEqual(self.blocked(self.create).reason, 'pr_reconciliation_ambiguous')
        self.assertEqual((len(self.provider.mutations()), self.phases()), (1, ['intent', 'head_published']))

    # 17
    def test_an_unknown_create_outcome_keeps_the_intent_and_never_duplicates(self):
        self.ready(); self.provider.lose_create_response = True
        e = self.blocked(self.create)
        self.assertEqual((e.reason, self.phases(), len(self.provider.pulls)), ('provider_unavailable', ['intent', 'head_published'], 1))
        self.provider.lose_create_response = False
        r = self.create()
        self.assertEqual((r['created_by'], len(self.provider.pulls), len(self.provider.mutations())), ('reconciled', 1, 1))


class ObserveTests(PullRequestBase):
    def opened(self):
        self.ready(); self.created = self.create()
        return self.provider.pulls[0]

    def records(self):
        return [r for r in pull_request._history(pull_request._records(self.work)) if r['phase'] == 'observed']

    # 20, 26, 29
    def test_open_observation_is_recorded_only_when_it_changes(self):
        self.opened(); calls = self.model_calls()
        o = self.status()
        self.assertEqual((o['state'], o['number'], o['head_drift'], o['base_moved'], o['remote_head_sha'], o['merged_commit_sha'], o['recorded']),
                         ('OPEN', 1, False, False, self.c, None, False))  # Same as the `created` reading: nothing new.
        self.provider.pulls[0]['mergeability'] = 'CLEAN'
        self.assertEqual((self.status()['mergeability'], self.records()), ('CLEAN', []))  # Unstable; never by itself recorded.
        self.provider.pulls[0]['draft'] = True
        self.assertTrue(self.status()['recorded']); self.assertFalse(self.status()['recorded'])
        self.assertEqual((len(self.records()), self.model_calls()), (1, calls))
        self.assertEqual([c[0] for c in self.provider.calls if c[0] != 'get'], ['find', 'create'])  # Observation never mutates.

    # 21
    def test_closed_unmerged(self):
        self.opened()['state'] = 'CLOSED'; self.provider.pulls[0]['closed_at'] = '2026-11-01T10:00:00Z'
        o = self.status()
        self.assertEqual((o['state'], o['merged_commit_sha'], pull_request_summary(self.work)['state']), ('CLOSED', None, 'CLOSED'))

    # 22, 23, 24
    def test_merged_records_the_providers_merged_commit_which_may_differ_from_c(self):
        pull = self.opened(); c = self.c
        t2 = self.remote_commit(self.t, 'T2')  # The base advances: normal, observed, never corruption.
        o = self.status()
        self.assertEqual((o['state'], o['base_sha'], o['base_moved'], o['created_target_sha'], o['head_drift']), ('OPEN', t2, True, self.t, False))
        p = self.git('commit-tree', c+'^{tree}', '-p', t2, '-m', 'squash')  # Squash: P does not contain C.
        self.git('push', '-q', 'origin', p+':refs/heads/main'); self.git('push', '-q', 'origin', ':'+HEAD)  # Head deleted on merge.
        pull.update(state='MERGED', merged_commit_sha=p, merged_at='2026-11-01T10:00:00Z', closed_at='2026-11-01T10:00:00Z',
                    merged_by='reviewer', base_sha=t2)
        o = self.status()
        self.assertEqual((o['state'], o['merged_commit_sha'], o['accepted_commit_sha'], o['head_observed_sha'], o['remote_head_sha'], o['head_drift']),
                         ('MERGED', p, c, c, None, False))
        self.assertNotEqual(p, c)
        self.assertNotEqual(subprocess.run(['git', 'merge-base', '--is-ancestor', c, p], cwd=self.target).returncode, 0)
        summary = recovery_status(self.work)
        self.assertEqual((summary['terminal'], summary['pull_request']['state'], summary['pull_request']['merged_commit_sha'],
                          summary['pull_request']['accepted_commit_sha'], summary['pull_request']['number']), ('ACCEPTED', 'MERGED', p, c, 1))
        created = pull_request._history(pull_request._records(self.work))[2]
        self.assertEqual(created, self.created)  # Creation provenance (T, C, assessment) is never rewritten.

    # 25
    def test_head_drift_is_reported_and_never_repaired(self):
        self.opened(); x = self.other_commit(self.c)
        self.git('push', '-q', '--force', 'origin', x+':'+HEAD)
        with patch('switchyard.publication._push', side_effect=AssertionError('must not repair')):
            o = self.status()
        self.assertEqual((o['head_expected_sha'], o['head_observed_sha'], o['remote_head_sha'], o['head_drift'], self.remote_head()),
                         (self.c, x, x, True, x))
        self.git('push', '-q', 'origin', ':'+HEAD)
        o = self.status()
        self.assertEqual((o['remote_head_sha'], o['head_drift'], o['state']), (None, True, 'OPEN'))
        self.assertEqual((pull_request_summary(self.work)['head_drift'], self.remote_head()), (True, None))
        self.assertEqual(self.create()['pull_request']['number'], 1)  # Identity stays durable; nothing re-pushed.
        self.assertIsNone(self.remote_head())

    # 27
    def test_recovery_status_is_local_only(self):
        self.opened(); self.provider.pulls[0]['state'] = 'MERGED'
        with patch('switchyard.publication._remote_sha', side_effect=AssertionError('remote')), \
                patch.object(GitHubPullRequestProvider, '_api', side_effect=AssertionError('provider')), \
                patch.object(FakeProvider, 'get', side_effect=AssertionError('provider')):
            status = recovery_status(self.work)
        self.assertEqual(status['pull_request']['state'], 'OPEN')  # The last observation, not a live refresh.

    def test_status_without_a_pr(self):
        self.ready()
        self.assertEqual(self.blocked(self.status).reason, 'no_pull_request: run work pr-create first')


class CredentialTests(PullRequestBase):
    # 19
    def test_credentials_never_enter_pr_records(self):
        self.ready(); url = 'file://user:s3cret@localhost'+str(self.bare)
        self.git('config', 'remote.origin.pushurl', url)
        r = self.create()
        self.assertEqual((r['remote_identity'], self.remote_head()), ('file://localhost'+str(self.bare), self.c))
        self.status()
        self.assertEqual([p for p in self.work.rglob('*') if p.is_file() and b's3cret' in p.read_bytes()], [])


FAKE_GH = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    log = os.environ['FAKE_GH_LOG']
    with open(log, 'a') as f:
        f.write(json.dumps(dict(argv=sys.argv[1:], prompt=os.environ.get('GH_PROMPT_DISABLED'))) + '\\n')
    sys.stderr.write(os.environ.get('FAKE_GH_STDERR', ''))
    sys.stdout.write(open(os.environ['FAKE_GH_RESPONSE']).read())
    sys.exit(int(os.environ.get('FAKE_GH_EXIT', '0')))
''')


def github_pull(number=7, state='open', merged=False, **extra):
    c, t, p = 'c'*40, 'a'*40, 'f'*40
    pull = dict(number=number, node_id='PR_kw', html_url='https://github.com/acme/widget/pull/%d' % number, state=state,
                draft=False, merged=merged, mergeable=None, merge_commit_sha=p if merged else 'e'*40,
                merged_at='2026-11-01T10:00:00Z' if merged else None, closed_at='2026-11-01T10:00:00Z' if state == 'closed' else None,
                merged_by={'login': 'reviewer'} if merged else None,
                head=dict(ref='switchyard/work', sha=c, repo=dict(full_name='Acme/Widget')),
                base=dict(ref='main', sha=t, repo=dict(full_name='Acme/Widget')), title='t', body='b')
    pull.update(extra)
    return pull


class GitHubProviderTests(unittest.TestCase):
    """The `gh` adapter and normalization, separately from Git publication."""
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp()); self.addCleanup(__import__('shutil').rmtree, self.dir)
        self.gh = self.dir/'gh'; self.gh.write_text(FAKE_GH.format(python=sys.executable)); self.gh.chmod(0o755)
        self.response, self.log = self.dir/'response.json', self.dir/'log.jsonl'
        self.env = dict(PATH=os.environ.get('PATH', ''), FAKE_GH_LOG=str(self.log), FAKE_GH_RESPONSE=str(self.response),
                        GH_TOKEN='ghp_supersecrettoken')
        self.provider = GitHubPullRequestProvider(str(self.gh), self.env)

    def respond(self, value, exit=0, stderr=''):
        self.response.write_text(json.dumps(value) if not isinstance(value, str) else value)
        self.provider.environment.update(FAKE_GH_EXIT=str(exit), FAKE_GH_STDERR=stderr)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_repository_identity_comes_only_from_github_push_urls(self):
        r = self.provider.repository
        for url in ('https://github.com/Acme/Widget.git', 'https://x-access-token:tok@github.com/acme/widget', 'git@github.com:acme/widget.git',
                    'ssh://git@github.com/acme/widget.git', 'https://github.com/acme/widget/'):
            self.assertEqual(r(url), REPO, url)
        for url in ('https://gitlab.com/acme/widget.git', 'https://github.com.evil.example/acme/widget', 'file:///tmp/widget.git',
                    '/tmp/widget.git', 'https://github.com/acme', 'https://github.com/acme/widget/extra', 'https://ghe.corp/acme/widget'):
            self.assertIsNone(r(url), url)

    def test_create_find_and_get_are_non_interactive_gh_api_calls_normalized(self):
        self.respond(github_pull())
        pull = self.provider.create(REPO, HEAD, 'refs/heads/main', 'Title', 'Body\n')
        self.assertEqual(pull, dict(provider='github', repository=REPO, number=7, provider_id='PR_kw',
                                    url='https://github.com/acme/widget/pull/7', state='OPEN', draft=False, mergeability='UNKNOWN',
                                    head_ref=HEAD, head_sha='c'*40, head_repository=REPO, base_ref='refs/heads/main', base_sha='a'*40,
                                    merged_commit_sha=None, merged_at=None, closed_at=None, merged_by=None))
        call = self.calls()[-1]
        self.assertEqual(call['prompt'], '1')
        self.assertEqual(call['argv'][:2], ['api', '--hostname'])
        self.assertEqual(call['argv'][-11:], ['--method', 'POST', 'repos/acme/widget/pulls', '-f', 'title=Title', '-f', 'head=switchyard/work',
                                              '-f', 'base=main', '-f', 'body=Body\n'])
        self.assertNotIn('ghp_supersecrettoken', json.dumps(call))  # The token stays in gh's environment.
        self.respond([github_pull(), github_pull(8, state='closed')])
        self.assertEqual([p['number'] for p in self.provider.find(REPO, HEAD, 'refs/heads/main')], [7, 8])
        self.assertEqual(self.calls()[-1]['argv'][-11:], ['--method', 'GET', 'repos/acme/widget/pulls', '-f', 'head=acme:switchyard/work',
                                                           '-f', 'base=main', '-f', 'state=all', '-f', 'per_page=100'])
        self.respond(github_pull(state='closed', merged=True, mergeable=False))
        merged = self.provider.get(REPO, 7)
        self.assertEqual((merged['state'], merged['merged_commit_sha'], merged['merged_by'], merged['mergeability']),
                         ('MERGED', 'f'*40, 'reviewer', 'CONFLICTING'))
        self.assertEqual(self.calls()[-1]['argv'][-3:], ['--method', 'GET', 'repos/acme/widget/pulls/7'])
        closed = normalize_github_pull(github_pull(state='closed', mergeable=True), REPO)
        self.assertEqual((closed['state'], closed['merged_commit_sha'], closed['mergeability']), ('CLOSED', None, 'CLEAN'))
        fork = normalize_github_pull(dict(github_pull(), head=dict(ref='x', sha='c'*40, repo=None)), REPO)
        self.assertIsNone(fork['head_repository'])

    def test_failures_are_classified_bounded_and_redacted(self):
        self.respond('', 1, 'gh: Bad credentials ghp_supersecrettoken (HTTP 401)\n')
        with self.assertRaises(PullRequestProviderError) as ctx:
            self.provider.get(REPO, 7)
        self.assertEqual(ctx.exception.reason, 'authentication_required')
        self.assertNotIn('ghp_supersecrettoken', ctx.exception.diagnostic)
        for stderr, reason in (('gh: Validation Failed (HTTP 422)', 'provider_rejected'), ('error connecting to api.github.com', 'provider_unavailable'),
                               ('gh: Server Error (HTTP 502)', 'provider_unavailable')):
            self.respond('', 1, stderr)
            with self.assertRaises(PullRequestProviderError) as ctx:
                self.provider.get(REPO, 7)
            self.assertEqual(ctx.exception.reason, reason)
        for bad in ('not json', [], dict(github_pull(), number='7'), dict(github_pull(), html_url='https://evil.example/x'),
                    dict(github_pull(), head=dict(ref='x', sha='nope'))):
            self.respond(bad)
            with self.assertRaises(PullRequestProviderError) as ctx:
                self.provider.get(REPO, 7)
            self.assertEqual(ctx.exception.reason, 'malformed_response')
        self.assertEqual(GitHubPullRequestProvider(str(self.dir/'missing')).repository('git@github.com:a/b'), 'github.com/a/b')
        with self.assertRaises(PullRequestProviderError) as ctx:
            GitHubPullRequestProvider(str(self.dir/'missing'), self.env).get(REPO, 1)
        self.assertEqual(ctx.exception.reason, 'provider_unavailable')


class CliTests(PullRequestBase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch('switchyard.pull_request.default_provider', return_value=self.provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['work', *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_creates_and_observes(self):
        c = self.ready()
        code, out, err = self.run_cli('pr-create', str(self.work), '--remote', 'origin', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(out)['head_sha'], json.loads(out)['pull_request']['number']), (c, 1))
        code, out, _ = self.run_cli('pr-create', str(self.work), '--remote', 'origin')
        self.assertEqual(out.splitlines()[:2], ['Pull request OPEN #1 (switchyard)', 'URL: https://github.com/acme/widget/pull/1'])
        self.provider.pulls[0].update(state='MERGED', merged_commit_sha='f'*40)
        code, out, _ = self.run_cli('pr-status', str(self.work))
        self.assertEqual((code, out.splitlines()[0], out.splitlines()[-1]), (0, 'Pull request MERGED #1', 'Merged commit: '+'f'*40))
        self.assertEqual(json.loads(self.run_cli('pr-status', str(self.work), '--json')[1])['state'], 'MERGED')

    def test_cli_refuses_a_remote_no_provider_hosts(self):
        self.ready()
        out = subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'pr-create', str(self.work), '--remote', 'origin',
                              '--gh-executable', '/nonexistent/gh'], cwd=ROOT, capture_output=True, text=True, env={**self.config.environment})
        self.assertEqual(out.returncode, 2)
        self.assertIn('PR_BLOCKED: unsupported_pr_provider', out.stderr.splitlines()[0])
        self.assertIsNone(self.remote_head())


if __name__ == '__main__':
    unittest.main()

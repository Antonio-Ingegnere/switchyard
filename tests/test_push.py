"""INTEGRATION (#9) slice 4: lease-protected push of a local integration to a remote. Local bare remotes only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from switchyard import integration, publication
from switchyard.publication import push_v2_work, remote_identity, remote_push_status, sanitize
from switchyard.recovery import recovery_status

from test_continuity import ROOT
from test_delivery_isolation import Crash
from test_integrate import IntegrateBase

P = 'REMOTE_PUSH_BLOCKED'


class PushBase(IntegrateBase):
    def setUp(self):
        super().setUp()
        self.bare = self.root/'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.bare)], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.bare))

    def remote_main(self, bare=None):
        out = self.git('ls-remote', str(bare or self.bare), 'refs/heads/main')
        return out.split('\t')[0] if out else None

    def integrated(self):
        """Remote main = T; local main integrated T -> M (checked-out source checkout, normal layout)."""
        self.c = self.accepted()
        self.t = self.advance(write={'keep.txt': 'upstream\n'})
        self.git('push', '-q', 'origin', 'main')
        self.assess(); self.n = self.integrate()['integrated_commit_sha']
        self.assertEqual(self.remote_main(), self.t)  # Accept and integrate never touch a remote.
        return self.n

    def remote_commit(self, parent, message='Q'):
        """Someone else's commit on the remote main (made locally, pushed with plain Git)."""
        q = self.git('commit-tree', parent+'^{tree}', '-p', parent, '-m', message)
        self.git('push', '-q', '--force', 'origin', q+':refs/heads/main')
        return q

    def push(self, remote='origin', target=None):
        return push_v2_work(self.work, remote, target)

    def pushes(self):
        return [r['phase'] for r in publication._history(publication._records(self.work))]

    def integration_store(self):
        return integration._history(integration._records(self.work))


class PushTests(PushBase):
    # 1-4, 15-18, 20
    def test_the_integrated_commit_is_published_by_lease_and_recorded(self):
        n = self.integrated(); calls = self.model_calls(); local = self.integration_store()
        r = self.push()
        self.assertEqual(self.remote_main(), n)
        self.assertEqual((r['phase'], r['pushed_by'], r['remote'], r['remote_identity'], r['remote_ref'], r['remote_before_sha'],
                          r['remote_after_sha'], r['local_integrated_commit_sha'], r['strategy'], r['lease']),
                         ('pushed', 'switchyard', 'origin', 'file://'+str(self.bare), 'refs/heads/main', self.t, n, n,
                          'force_with_lease_cas', 'refs/heads/main:'+self.t))
        self.assertEqual((r['integration_record_version'], self.pushes()), (2, ['intent', 'pushed']))
        self.assertEqual(self.integration_store(), local)  # The integration record is consumed, never rewritten.
        self.assertEqual((self.git('rev-parse', 'switchyard/work'), self.model_calls()), (self.c, calls))
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['integration']['state'], status['remote_integration']['state'],
                          status['remote_integration']['pushed_commit_sha']), ('ACCEPTED', 'INTEGRATED', 'PUSHED', n))
        self.assertEqual((self.push(), self.pushes()), (r, ['intent', 'pushed']))  # Idempotent.
        q = self.remote_commit(n)  # Development continues on the remote.
        self.assertEqual((self.push(), remote_push_status(self.work)['pushed_commit_sha'], self.remote_main()), (r, n, q))

    def test_remote_already_at_the_integrated_commit_is_recorded_without_pushing(self):
        n = self.integrated(); self.git('push', '-q', 'origin', 'main')  # Someone pushed the very same commit.
        with patch('switchyard.publication._push', side_effect=AssertionError('must not push')):
            r = self.push()
        self.assertEqual((r['pushed_by'], r['remote_before_sha'], r['remote_after_sha']), ('already_present', n, n))


class MovementTests(PushBase):
    # 5
    def test_a_moved_remote_blocks_before_anything_is_recorded(self):
        self.integrated(); q = self.remote_commit(self.t)
        e = self.blocked(self.push)
        self.assertEqual((e.terminal, e.reason, e.push), (P, 'remote_moved', dict(expected_remote_sha=self.t, actual_remote_sha=q)))
        self.assertEqual((self.remote_main(), (self.work/'publication').exists()), (q, False))

    # 6
    def test_a_moved_local_target_blocks(self):
        n = self.integrated(); later = self.advance(write={'later.txt': 'L\n'}, message='L')
        e = self.blocked(self.push)
        self.assertEqual((e.reason, e.push), ('local_target_moved', dict(expected_local_sha=n, actual_local_sha=later)))
        self.assertEqual(self.remote_main(), self.t)


class CrashTests(PushBase):
    # 7, 9
    def test_crash_after_the_remote_accepted_finalizes_without_a_second_push(self):
        n = self.integrated(); original = publication._put
        def put(records, record):
            if record['phase'] == 'pushed':
                raise Crash()
            return original(records, record)
        with patch('switchyard.publication._put', put), self.assertRaises(Crash):
            self.push()
        self.assertEqual((self.remote_main(), self.pushes(), remote_push_status(self.work)['state']), (n, ['intent'], 'PUSH_PENDING'))
        with patch('switchyard.publication._push', side_effect=AssertionError('must not push again')):
            r = self.push()
        self.assertEqual((r['pushed_by'], r['remote_before_sha'], r['remote_after_sha'], self.pushes()),
                         ('switchyard', self.t, n, ['intent', 'pushed']))

    def test_crash_before_the_push_retries_with_the_same_lease(self):
        n = self.integrated()
        with patch('switchyard.publication._push', side_effect=Crash()), self.assertRaises(Crash):
            self.push()
        self.assertEqual((self.remote_main(), self.pushes()), (self.t, ['intent']))
        self.assertEqual(self.push()['remote_after_sha'], n)
        self.assertEqual((self.remote_main(), self.pushes()), (n, ['intent', 'pushed']))

    # 8
    def test_an_unexpected_remote_after_a_crash_is_ambiguous_and_never_reset(self):
        self.integrated()
        with patch('switchyard.publication._push', side_effect=Crash()), self.assertRaises(Crash):
            self.push()
        q = self.remote_commit(self.t)
        e = self.blocked(self.push)
        self.assertEqual((e.reason, e.push['actual_remote_sha']), ('push_reconciliation_ambiguous', q))
        self.assertEqual((self.remote_main(), self.pushes()), (q, ['intent']))


class RefusalTests(PushBase):
    # 10-12
    def test_missing_or_invalid_remotes_targets_and_products_are_refused(self):
        c = self.accepted()
        self.assertEqual(self.blocked(self.push).reason, 'not_integrated: run work integrate first')
        self.git('push', '-q', 'origin', 'main'); self.assess(); self.integrate()
        for remote, reason in (('nope', 'remote_not_configured'), ('-oops', 'remote_invalid'), ('a b', 'remote_invalid')):
            self.assertEqual(self.blocked(lambda: self.push(remote)).reason, reason)
        self.assertEqual(self.blocked(lambda: self.push(target='nope')).reason, 'target_not_found')
        self.assertEqual(self.blocked(lambda: self.push(target=self.base)).reason, 'target_not_a_branch')
        empty = self.root/'empty.git'; subprocess.run(['git', 'init', '-q', '--bare', str(empty)], check=True)
        self.git('remote', 'add', 'empty', str(empty))
        e = self.blocked(lambda: self.push('empty'))
        self.assertEqual((e.reason, e.paths), ('remote_target_missing', ('empty refs/heads/main',)))
        self.assertEqual(self.git('ls-remote', str(empty)), '')  # Never creates the branch.
        self.assertFalse((self.work/'publication').exists())

    # 19
    def test_a_server_rejection_leaves_the_remote_unchanged(self):
        n = self.integrated()
        hook = self.bare/'hooks/pre-receive'; hook.write_text('#!/bin/sh\necho "protected branch: pull request required" >&2\nexit 1\n'); hook.chmod(0o755)
        e = self.blocked(self.push)
        self.assertEqual(e.reason, 'remote_rejected'); self.assertIn('pull request required', e.push['diagnostic'])
        self.assertEqual((self.remote_main(), self.pushes()), (self.t, ['intent', 'failed']))
        self.assertEqual(remote_push_status(self.work), dict(state='NOT_PUSHED', last_attempt=dict(remote='origin', ref='refs/heads/main', reason='remote_rejected')))
        hook.unlink()
        self.assertEqual((self.push()['remote_after_sha'], self.remote_main()), (n, n))


class CredentialTests(PushBase):
    # 13, 14
    def test_remote_identities_never_carry_credentials(self):
        self.assertEqual(remote_identity('https://user:s3cret@github.com/owner/repo.git?x=1#f'), 'https://github.com/owner/repo.git')
        self.assertEqual(remote_identity('git@github.com:owner/repo.git'), 'ssh://github.com/owner/repo.git')
        self.assertEqual(remote_identity('ssh://git@host.example:2222/owner/repo.git'), 'ssh://host.example:2222/owner/repo.git')
        self.assertEqual(remote_identity('HTTPS://Token@Example.com/r'), 'https://example.com/r')
        self.assertEqual(remote_identity(str(self.bare)), 'file://'+str(self.bare))
        self.assertEqual(sanitize("fatal: unable to access 'https://u:tok@h.example/r.git/': x"), "fatal: unable to access 'https://h.example/r.git/': x")

    def test_a_credential_push_url_is_published_through_and_never_persisted(self):
        n = self.integrated(); url = 'file://user:s3cret@localhost'+str(self.bare)
        self.git('remote', 'add', 'secure', str(self.root/'nowhere.git')); self.git('config', 'remote.secure.pushurl', url)
        r = self.push('secure')
        self.assertEqual((r['remote_identity'], self.remote_main()), ('file://localhost'+str(self.bare), n))
        self.assertEqual([p for p in self.work.rglob('*') if p.is_file() and b's3cret' in p.read_bytes()], [])
        self.git('remote', 'add', 'down', 'https://user:t0ken@127.0.0.1:9/owner/repo.git')
        e = self.blocked(lambda: self.push('down'))
        self.assertEqual(e.reason, 'remote_unavailable')
        self.assertNotIn('t0ken', str(e) + json.dumps(e.push))


class PushUrlTests(PushBase):
    """Provenance names the endpoint `git push <remote>` writes to, as Git resolves it."""
    def bare_repo(self, name):
        path = self.root/name; subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(path)], check=True)
        return path

    # 1
    def test_without_pushurl_the_identity_is_the_remote_url(self):
        self.integrated()
        self.assertEqual(self.push()['remote_identity'], 'file://'+str(self.bare))

    # 2, 4, 5
    def test_a_pushurl_is_the_recorded_and_the_actual_destination(self):
        n = self.integrated(); fetch = self.bare_repo('fetch.git')
        self.git('push', '-q', str(fetch), self.t+':refs/heads/main')
        self.git('config', 'remote.origin.url', str(fetch)); self.git('config', 'remote.origin.pushurl', str(self.bare))
        r = self.push()
        self.assertEqual((r['remote_identity'], r['remote_before_sha'], self.remote_main(), self.remote_main(fetch)),
                         ('file://'+str(self.bare), self.t, n, self.t))  # Push endpoint moved; the fetch repository did not.

    def test_the_remote_check_reads_the_push_endpoint_not_the_fetch_url(self):
        n = self.integrated(); fetch = self.bare_repo('fetch.git')
        self.git('push', '-q', str(fetch), n+':refs/heads/main')  # The fetch URL already "has" N ...
        self.git('config', 'remote.origin.url', str(fetch)); self.git('config', 'remote.origin.pushurl', str(self.bare))
        r = self.push()  # ... but the push endpoint is at T: a real push, not `already_present`.
        self.assertEqual((r['pushed_by'], r['remote_before_sha'], self.remote_main()), ('switchyard', self.t, n))

    # 6
    def test_push_url_rewriting_is_resolved_as_git_push_resolves_it(self):
        n = self.integrated(); alias = 'https://user:s3cret@example.invalid/owner/repo.git'
        self.git('config', 'remote.origin.url', alias); self.git('config', 'url.%s.pushInsteadOf' % self.bare, alias)
        r = self.push()  # pushInsteadOf rewrites `url` for pushes: the physical endpoint is recorded, not the alias.
        self.assertEqual((r['remote_identity'], self.remote_main()), ('file://'+str(self.bare), n))
        self.assertEqual([p for p in self.work.rglob('*') if p.is_file() and b's3cret' in p.read_bytes()], [])

    # 7
    def test_several_push_urls_are_an_ambiguous_destination(self):
        self.integrated(); second = self.bare_repo('second.git')
        self.git('config', 'remote.origin.pushurl', str(self.bare)); self.git('config', '--add', 'remote.origin.pushurl', str(second))
        e = self.blocked(self.push)
        self.assertEqual((e.terminal, e.reason, e.paths), (P, 'ambiguous_push_destination', tuple(sorted(('file://'+str(self.bare), 'file://'+str(second))))))
        self.assertEqual((self.remote_main(), self.remote_main(second), (self.work/'publication').exists()), (self.t, None, False))


class CliTests(PushBase):
    def test_cli_pushes_and_reports_remote_movement(self):
        n = self.integrated(); env = {**self.config.environment}
        cli = lambda *a: subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'push', str(self.work), *a], cwd=ROOT,
                                        capture_output=True, text=True, env=env)
        self.assertNotEqual(cli().returncode, 0)  # --remote is required: no remote is guessed.
        out = cli('--remote', 'origin', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        record = json.loads(out.stdout)
        self.assertEqual((record['remote'], record['remote_ref'], record['remote_before_sha'], record['remote_after_sha'], record['phase']),
                         ('origin', 'refs/heads/main', self.t, n, 'pushed'))
        self.assertEqual(cli('--remote', 'origin').stdout.splitlines()[0], 'Push PUSHED')


if __name__ == '__main__':
    unittest.main()

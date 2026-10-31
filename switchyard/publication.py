"""INTEGRATION (#9) slice 4: publish a local integration to a remote branch by compare-and-swap.

Lifecycle: ACCEPT (delivery commit C, never touches the target) != INTEGRATE (local target T -> N) != PUSH
(this module: remote T -> N) != PR (later). `push_v2_work` is the only operation that mutates a remote ref.

- Product: the durable local integration record for the branch -- `integrated_by: switchyard`, N =
  `integrated_commit_sha` -- re-proven before anything is sent (C re-proven; N exists, contains C and T, and
  for a merge is exactly the deterministic M the record describes). The local branch must still be exactly N
  (`local_target_moved` otherwise); its current head is never pushed instead.
- Remote: named explicitly; the single endpoint `git push <name>` writes to (`git remote get-url --push`:
  `pushurl` over `url`, push URL rewriting applied; several push URLs are `ambiguous_push_destination`) is
  recorded only as a sanitized identity (no userinfo, query or fragment). That endpoint's branch is read live
  (`git ls-remote <push url>`, nothing fetched) and must be exactly the T the
  local integration started from: R == T -> push; R == N -> already published (recorded, nothing sent);
  missing -> `remote_target_missing`; anything else -> `remote_moved`. All of this precedes any record.
- Push: `git push --porcelain --force-with-lease=<ref>:T <remote> N:<ref>`. The lease is the compare-and-swap
  (the remote must still be T); N descends from T, so the update is a fast-forward and never rewrites history.
  No `--force`, no `--no-verify`; normal Git authentication (credential helper, SSH agent), never prompted
  (`GIT_TERMINAL_PROMPT=0`); nothing secret is stored or echoed.
- Durable order: `intent` -> push -> `pushed`. A definite server answer is recorded as `failed`
  (`remote_moved`, `remote_rejected`); a transport failure (`remote_unavailable`) keeps the intent, because the
  remote may have applied it. A retry with a pending intent reads the remote: T -> push again with the same
  lease; N -> finalize without pushing; else `push_reconciliation_ambiguous` (the remote is never reset).
- Status (`remote_push_status`, `recovery_status()['remote_integration']`) is local only; it never contacts
  the remote.
"""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit

from . import integration
from .integration import _accepted_work, _ancestor, _branch, _commit, _merge_commit, _prove, _repo, _work_id
from .recovery import RecoveryBlocked, _lock
from .work_contract import ArtifactSpec, WorkWorkspace

PUSH_SCHEMA = 1
P = 'REMOTE_PUSH_BLOCKED'
CATALOG = {'request': ArtifactSpec('text', source='request'), 'push_record': ArtifactSpec('json')}
STRATEGY = 'force_with_lease_cas'
_REMOTE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
_USERINFO = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@')


def _blocked(reason, paths=(), **details):
    error = RecoveryBlocked(P, reason, paths)
    if details:
        error.push = details
    return error


def sanitize(text):
    """Diagnostics may quote URLs: drop any `scheme://userinfo@` credentials before showing or storing them."""
    return _USERINFO.sub(r'\1', text)


def remote_identity(url):
    """Safe canonical identity of a configured remote URL: scheme, host, port and path only."""
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', url):
        parts = urlsplit(url)
        host = parts.hostname or ''
        netloc = host + (':%d' % parts.port if parts.port else '')
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, '', ''))
    scp = re.match(r'^(?:[^@/:]+@)?([^/:]+):(?!//)(.+)$', url)
    if scp and not os.path.isabs(url):
        return 'ssh://%s/%s' % (scp.group(1), scp.group(2).lstrip('/'))
    return 'file://' + str(Path(url).expanduser().resolve())


def _git(repo, *args, timeout=300):
    env = {**repo.env, 'GIT_TERMINAL_PROMPT': '0'}
    try:
        result = subprocess.run([repo.git_executable, *args], cwd=repo.workspace, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, b'', b'timed out'
    except OSError:
        return None, b'', b'git unavailable'
    return result.returncode, result.stdout, result.stderr


def _diagnostic(raw):
    return sanitize(raw.decode('utf-8', 'replace')).strip()[-1024:]


def _remote(repo, name):
    """(push URL, sanitized identity) of the ONE endpoint `git push <name>` writes to, as Git itself resolves
    it (`remote get-url --push --all`: `pushurl` over `url`; `pushInsteadOf`/`insteadOf` expanded the way push
    applies them). Several push URLs mean Git pushes to all of them: refused, never recorded as one. The URL
    is used only to read that endpoint and is never stored."""
    if not name or not _REMOTE_NAME.fullmatch(name) or name.endswith('.'):
        raise _blocked('remote_invalid', [str(name)])
    code, out, _ = _git(repo, 'remote', 'get-url', '--push', '--all', name, timeout=30)
    urls = [u for u in out.decode('utf-8', 'replace').splitlines() if u.strip()] if code == 0 else []
    if not urls:
        raise _blocked('remote_not_configured', [name])
    if len(urls) > 1:
        raise _blocked('ambiguous_push_destination', sorted({remote_identity(u) for u in urls}))
    if urls[0].startswith('-'):
        raise _blocked('remote_invalid', [name])
    return urls[0], remote_identity(urls[0])


def _remote_sha(repo, url, ref):
    """The push endpoint's current commit for `ref` (None if it does not exist), read live from the same URL
    the push writes to (`git ls-remote <remote>` would read the fetch URL); nothing is fetched."""
    code, out, err = _git(repo, 'ls-remote', url, ref)
    if code != 0:
        raise _blocked('remote_unavailable', diagnostic=_diagnostic(err))
    for line in out.decode('utf-8', 'replace').splitlines():
        sha, _, name = line.partition('\t')
        if name == ref:
            return sha
    return None


def _push(repo, remote, ref, new, expected):
    """One lease-protected push. Returns None on success, else (reason, diagnostic, definite)."""
    code, out, err = _git(repo, 'push', '--porcelain', '--force-with-lease=%s:%s' % (ref, expected), remote, '%s:%s' % (new, ref))
    if code == 0:
        return None
    status = [line for line in out.decode('utf-8', 'replace').splitlines() if line.startswith('!\t') and ref in line]
    if status and '[remote rejected]' in status[0]:
        return 'remote_rejected', _diagnostic(out+err), True
    if status and '[rejected]' in status[0]:  # stale info / fetch first: the lease did not hold.
        return 'remote_moved', _diagnostic(out+err), True
    return 'remote_unavailable', _diagnostic(out+err), False


def _records(root, create=False, work_id=None):
    path = Path(root)/'publication'
    if (path/'manifest.jsonl').is_file():
        return WorkWorkspace.open(path, CATALOG)
    if not create:
        return None
    records = WorkWorkspace.open(path, CATALOG)
    records.initialize_request('Remote publication records of work %s.\n' % work_id)
    return records


def _history(records, remote=None, ref=None):
    if records is None:
        return []
    found = [json.loads(records.read_artifact('push_record', v.version)) for v in records.versions('push_record')]
    return [r for r in found if (remote is None or r['remote'] == remote) and (ref is None or r['remote_ref'] == ref)]


def _put(records, record):
    records.write_artifact('push_record', json.dumps(record, sort_keys=True, indent=2)+'\n')
    return record


def _integration(root, repo, full, c):
    """(integration record, its version, its digest) of this branch, with N re-proven from durable facts."""
    store = integration._records(root)
    found = None
    for version in store.versions(integration.INTEGRATION_RECORD) if store else ():
        record = json.loads(store.read_artifact(integration.INTEGRATION_RECORD, version.version))
        if record['target_ref_full'] == full:
            found = (record, version)
    if found is None:
        raise _blocked('not_integrated: run work integrate first', [full])
    record, version = found
    if record['phase'] == 'intent':
        raise _blocked('integration_pending: finish work integrate first', [full])
    if record['phase'] != 'integrated':
        raise _blocked('not_integrated: run work integrate first', [full])
    if record['integrated_by'] != 'switchyard' or not record.get('integrated_commit_sha'):
        raise _blocked('no_integrated_commit: the target already contained the delivery; nothing to publish', [full])
    n, t = record['integrated_commit_sha'], record['target_before_sha']
    valid = record['accepted_commit_sha'] == c and _commit(repo, n) == n and _ancestor(repo, c, n) and _ancestor(repo, t, n)
    if valid and record['strategy'] == 'fast_forward':
        valid = n == c
    elif valid:
        valid = _merge_commit(repo, record, write=False) == n
    if not valid:
        raise _blocked('integration_invalid: the integrated commit is not provably this work\'s integration', [n])
    return record, version.version, version.sha256


def push_v2_work(work_root, remote, target_ref=None):
    """Publish this work's local integration of `target_ref` (default: the source branch recorded when the
    delivery started) to the same branch on `remote`. Returns the push record; refusals are
    `REMOTE_PUSH_BLOCKED`."""
    root = Path(work_root).resolve()
    with _lock(root):
        try:
            store, iso, op = _accepted_work(root)
            repo = _repo(iso)
            c, _ = _prove(store, iso, op, repo)
        except RecoveryBlocked as exc:
            raise _blocked(exc.reason, exc.paths) from None
        requested = target_ref or iso['source_branch']
        full = _branch(repo, requested)
        if full is None:
            raise _blocked('target_not_found' if _commit(repo, requested) is None else 'target_not_a_branch', [requested])
        push_url, identity = _remote(repo, remote)
        record, version, digest = _integration(root, repo, full, c)
        n, t = record['integrated_commit_sha'], record['target_before_sha']
        records = _records(root)
        history = _history(records, remote, full)
        last = history[-1] if history else None
        if last and last['phase'] == 'pushed' and last['local_integrated_commit_sha'] == n:
            return last  # Idempotent: this exact product was already published to this remote branch.
        local = _commit(repo, full)
        if local != n:
            raise _blocked('local_target_moved', [full], expected_local_sha=n, actual_local_sha=local)
        intent = dict(schema=PUSH_SCHEMA, phase='intent', work_id=_work_id(iso), remote=remote, remote_identity=identity,
                      remote_ref=full, expected_remote_sha=t, local_integrated_commit_sha=n, accepted_commit_sha=c,
                      integration_record_version=version, integration_record_sha256=digest, strategy=STRATEGY,
                      lease='%s:%s' % (full, t))
        pending = last if last and last['phase'] == 'intent' else None
        if pending is not None and {k: pending[k] for k in intent if k != 'phase'} != {k: intent[k] for k in intent if k != 'phase'}:
            raise _blocked('push_reconciliation_ambiguous: the pending push is for another product or remote')
        current = _remote_sha(repo, push_url, full)
        if current not in (t, n):
            if pending is not None:
                raise _blocked('push_reconciliation_ambiguous', expected_remote_sha=t, planned_remote_sha=n, actual_remote_sha=current)
            if current is None:
                raise _blocked('remote_target_missing', [remote+' '+full])
            raise _blocked('remote_moved', expected_remote_sha=t, actual_remote_sha=current)
        records = records or _records(root, create=True, work_id=_work_id(iso))  # Only now is anything recorded.
        if current == n:  # Already there: a crashed push of ours, or someone published the same commit.
            return _put(records, dict(intent, phase='pushed', pushed_by='switchyard' if pending else 'already_present',
                                      remote_before_sha=t if pending else n, remote_after_sha=n))
        if pending is None:
            _put(records, intent)
        failure = _push(repo, remote, full, n, t)
        if failure is None:
            return _put(records, dict(intent, phase='pushed', pushed_by='switchyard', remote_before_sha=t, remote_after_sha=n))
        reason, diagnostic, definite = failure
        if definite:
            _put(records, dict(intent, phase='failed', reason=reason, diagnostic=diagnostic))
        raise _blocked(reason, diagnostic=diagnostic)


def remote_push_status(work_root):
    """Read-only, local only (never contacts a remote): the compact publication state, or None."""
    history = _history(_records(Path(work_root).resolve()))
    if not history:
        return None
    pushed = [r for r in history if r['phase'] == 'pushed']
    last = history[-1]
    out = {}
    if pushed:
        p = pushed[-1]
        out = dict(state='PUSHED', remote=p['remote'], remote_identity=p['remote_identity'], ref=p['remote_ref'],
                   pushed_commit_sha=p['remote_after_sha'], remote_before_sha=p['remote_before_sha'], pushed_by=p['pushed_by'])
    if last['phase'] == 'intent':
        out.update(state='PUSH_PENDING', remote=last['remote'], ref=last['remote_ref'], planned_commit_sha=last['local_integrated_commit_sha'])
    elif last['phase'] == 'failed':
        out.setdefault('state', 'NOT_PUSHED')
        out['last_attempt'] = dict(remote=last['remote'], ref=last['remote_ref'], reason=last['reason'])
    return out

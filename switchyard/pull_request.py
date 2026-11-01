"""INTEGRATION (#9) slice 5: the review path. Publish the accepted commit C as a review branch, open a pull
request C -> target, observe it. Never merges, closes, edits or updates a pull request.

Lifecycle: ACCEPT C -> readiness against T -> either DIRECT (integrate -> N, push N) or PR (this module). The PR
path needs neither `integrate` nor `push`, never calls them, and never publishes N: the PR head is C itself.

- Provider boundary: `PullRequestProvider` (repository identity of a push URL; find / create / get, all returning
  the normalized `pull_request` dict below). `GitHubPullRequestProvider` is a thin, non-interactive `gh api`
  adapter: `gh` owns authentication, nothing secret is passed, stored or echoed, output is bounded, GitHub JSON
  never leaves the adapter. Only github.com and same-repository heads; anything else is `unsupported_pr_provider`.
- Remote: the endpoint `git push <remote>` writes to (publication's resolution: `get-url --push`, one URL); the
  PR repository is derived from that same URL, so the repository receiving the head is the PR's repository.
- Gate (before any record or remote change): C re-proven; the latest assessment is this product's, READY or
  READY_WITH_DRIFT, and fresh (the local target ref still names T); the remote target branch is exactly T
  (`target_moved` otherwise, nothing fetched or reassessed); a remote target that already contains C (durable
  direct-push provenance, or C an ancestor of it) is `already_integrated`.
- Head: `refs/heads/switchyard/<work-id>` (the durable delivery branch name) <- C, never the local branch head.
  Missing -> created with `--force-with-lease=<head>:` (must still not exist); C -> reused; else `pr_head_moved`.
- Durable order (`<work>/pull_request/`): `intent` (frozen title/body, C, A, T, assessment, head, repository)
  -> head push -> `head_published` -> search, then create only if nothing matches -> `created`. A retry with a
  pending intent completes that intent: it never re-gates, re-titles or pushes another ref, and always searches
  the provider before creating (a lost create response is adopted, never duplicated); several matching PRs, or
  one whose head is not C, are `pr_reconciliation_ambiguous`.
- Observation (`pr_status_v2_work`): read-only towards the provider and the remote; normalized to
  OPEN / CLOSED / MERGED (+ `draft`, + `mergeability` CLEAN / CONFLICTING / UNKNOWN, never part of identity).
  A materially changed observation appends one `observed` record; head drift is reported, never repaired; base
  movement is normal. MERGED records the provider's merged commit P, which need not be or contain C.
- `pull_request_summary` (and `recovery_status()['pull_request']`) is local only.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from . import integration, publication
from .integration import _accepted_work, _ancestor, _branch, _canonical, _commit, _prove, _repo, _work_id, _IDENTITY, INTEGRABLE
from .recovery import RecoveryBlocked, _lock
from .work_contract import ArtifactSpec, WorkWorkspace

PR_SCHEMA = 1
B = 'PR_BLOCKED'
CATALOG = {'request': ArtifactSpec('text', source='request'), 'pull_request_record': ArtifactSpec('json')}
OPEN, CLOSED, MERGED = 'OPEN', 'CLOSED', 'MERGED'
MAX_TITLE = 256
_SHA = re.compile(r'[0-9a-f]{40}')
# Material facts of an observation: a new record only when one of these changes. Mergeability is provider-
# computed and unstable, so it is reported live but never by itself a reason to record.
_MATERIAL = ('state', 'draft', 'head_observed_sha', 'remote_head_sha', 'head_drift', 'base_sha', 'merged_commit_sha',
             'merged_at', 'closed_at', 'merged_by')


def _blocked(reason, paths=(), **details):
    error = RecoveryBlocked(B, reason, paths)
    if details:
        error.pull_request = details
    return error


def _as_pr(exc):
    """A lower-level refusal (integration, publication) as a PR refusal, same reason and details."""
    error = _blocked(exc.reason, exc.paths, **(getattr(exc, 'push', None) or getattr(exc, 'integration', None) or {}))
    return error


# --- Provider boundary -----------------------------------------------------------------------------------
class PullRequestProviderError(Exception):
    """reason: provider_unavailable | provider_rejected | authentication_required | malformed_response."""
    def __init__(self, reason, diagnostic=''):
        super().__init__(reason)
        self.reason, self.diagnostic = reason, diagnostic


class PullRequestProvider:
    """Normalized pull request: provider, repository, number, provider_id, url, state (OPEN/CLOSED/MERGED), draft,
    mergeability, head_ref, head_sha, head_repository, base_ref, base_sha, merged_commit_sha, merged_at,
    closed_at, merged_by. Refs are full (`refs/heads/...`)."""
    name = None
    identity = None  # Provider/create schema version, frozen into every intent.

    def repository(self, push_url):
        """Canonical repository identity the push URL denotes, or None when this provider does not host it."""
        raise NotImplementedError

    def find(self, repository, head_ref, base_ref):
        """All pull requests (any state) whose head is `head_ref` in `repository` and whose base is `base_ref`."""
        raise NotImplementedError

    def create(self, repository, head_ref, base_ref, title, body):
        raise NotImplementedError

    def get(self, repository, number):
        raise NotImplementedError


_GITHUB_URL = re.compile(r'(?:https://(?:[^/@\s]*@)?github\.com(?::443)?/|ssh://(?:[^/@\s]*@)?github\.com(?::22)?/|'
                         r'(?:[A-Za-z0-9_.-]+@)?github\.com:(?!//))'
                         r'([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?', re.I)
_GH_OUTPUT = 1048576
_GH_TIMEOUT = 60


class GitHubPullRequestProvider(PullRequestProvider):
    """`gh api` adapter (REST pulls API on github.com). `gh` authenticates (its own login or GH_TOKEN); prompts
    are disabled and stdin is closed, so a missing login fails instead of waiting."""
    name = 'github'
    identity = 'github-pulls/1'

    def __init__(self, executable='gh', environment=None, timeout=_GH_TIMEOUT):
        self.executable, self.timeout = executable or 'gh', timeout
        self.environment = dict(os.environ if environment is None else environment)

    def repository(self, push_url):
        match = _GITHUB_URL.fullmatch(push_url or '')
        if not match or match.group(2) in ('.', '..'):
            return None
        return 'github.com/%s/%s' % (match.group(1).lower(), match.group(2).lower())

    def _redact(self, text):
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN'):
            if len(self.environment.get(name) or '') >= 4:
                text = text.replace(self.environment[name], '[REDACTED]')
        return publication.sanitize(text).strip()[-1024:]

    def _api(self, *args):
        env = {**self.environment, 'GH_PROMPT_DISABLED': '1', 'GH_NO_UPDATE_NOTIFIER': '1', 'NO_COLOR': '1',
               'GH_PAGER': 'cat', 'GIT_TERMINAL_PROMPT': '0'}
        command = [self.executable, 'api', '--hostname', 'github.com', '-H', 'Accept: application/vnd.github+json',
                   '-H', 'X-GitHub-Api-Version: 2022-11-28', *args]
        try:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise PullRequestProviderError('provider_unavailable', 'gh timed out') from None
        except OSError:
            raise PullRequestProviderError('provider_unavailable', 'gh is not available') from None
        if result.returncode != 0:
            diagnostic = self._redact(result.stderr.decode('utf-8', 'replace')[-4096:])
            status = re.search(r'\(HTTP (\d{3})\)', diagnostic)
            code = int(status.group(1)) if status else None
            reason = ('authentication_required' if code == 401 or 'gh auth login' in diagnostic else
                      'provider_rejected' if code and 400 <= code < 500 else 'provider_unavailable')
            raise PullRequestProviderError(reason, diagnostic)
        if len(result.stdout) > _GH_OUTPUT:
            raise PullRequestProviderError('malformed_response', 'response exceeds the bounded read limit')
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError, RecursionError):
            raise PullRequestProviderError('malformed_response', 'gh returned invalid JSON') from None

    @staticmethod
    def _path(repository, suffix=''):
        owner, name = repository.split('/')[1:]
        return 'repos/%s/%s/pulls%s' % (owner, name, suffix)

    def find(self, repository, head_ref, base_ref):
        owner = repository.split('/')[1]
        found = self._api('--method', 'GET', self._path(repository), '-f', 'head=%s:%s' % (owner, _short(head_ref)),
                          '-f', 'base='+_short(base_ref), '-f', 'state=all', '-f', 'per_page=100')
        if type(found) is not list:
            raise PullRequestProviderError('malformed_response', 'pull request list is not a list')
        return [normalize_github_pull(p, repository) for p in found]

    def create(self, repository, head_ref, base_ref, title, body):
        return normalize_github_pull(self._api('--method', 'POST', self._path(repository), '-f', 'title='+title,
                                               '-f', 'head='+_short(head_ref), '-f', 'base='+_short(base_ref),
                                               '-f', 'body='+body), repository)

    def get(self, repository, number):
        return normalize_github_pull(self._api('--method', 'GET', self._path(repository, '/%d' % int(number))), repository)


def _short(ref):
    return ref[len('refs/heads/'):] if ref.startswith('refs/heads/') else ref


def normalize_github_pull(payload, repository):
    """GitHub REST pull -> the provider-neutral pull request contract. Anything malformed is refused."""
    def text(value, limit, nullable=False):
        if value is None and nullable:
            return None
        if type(value) is not str or not value or len(value) > limit or any(ord(ch) < 32 for ch in value):
            raise PullRequestProviderError('malformed_response', 'invalid pull request field')
        return value

    def sha(value, nullable=False):
        if value is None and nullable:
            return None
        if type(value) is not str or not _SHA.fullmatch(value):
            raise PullRequestProviderError('malformed_response', 'invalid commit id')
        return value

    if type(payload) is not dict or type(payload.get('number')) is not int or payload['number'] <= 0:
        raise PullRequestProviderError('malformed_response', 'invalid pull request identity')
    head, base = payload.get('head'), payload.get('base')
    if type(head) is not dict or type(base) is not dict:
        raise PullRequestProviderError('malformed_response', 'invalid pull request head or base')
    url = text(payload.get('html_url'), 512)
    if not url.lower().startswith('https://github.com/'):
        raise PullRequestProviderError('malformed_response', 'invalid pull request URL')
    state = text(payload.get('state'), 16)
    if state not in ('open', 'closed'):
        raise PullRequestProviderError('malformed_response', 'invalid pull request state')
    merged_at = text(payload.get('merged_at'), 64, nullable=True)
    merged = payload.get('merged') is True or merged_at is not None
    lifecycle = MERGED if merged else OPEN if state == 'open' else CLOSED
    head_repo = head.get('repo')
    head_repository = ('github.com/'+text(head_repo.get('full_name'), 201).lower()) if type(head_repo) is dict else None
    mergeable = payload.get('mergeable')
    merged_by = payload.get('merged_by')
    return dict(provider='github', repository=repository, number=payload['number'],
                provider_id=text(payload.get('node_id'), 128, nullable=True), url=url, state=lifecycle,
                draft=payload.get('draft') is True,
                mergeability='CLEAN' if mergeable is True else 'CONFLICTING' if mergeable is False else 'UNKNOWN',
                head_ref='refs/heads/'+text(head.get('ref'), 255), head_sha=sha(head.get('sha')), head_repository=head_repository,
                base_ref='refs/heads/'+text(base.get('ref'), 255), base_sha=sha(base.get('sha'), nullable=True),
                merged_commit_sha=sha(payload.get('merge_commit_sha'), nullable=True) if merged else None,
                merged_at=merged_at, closed_at=text(payload.get('closed_at'), 64, nullable=True),
                merged_by=text(merged_by.get('login'), 100) if merged and type(merged_by) is dict else None)


def default_provider(executable=None):
    return GitHubPullRequestProvider(executable or 'gh')


# --- Durable store --------------------------------------------------------------------------------------
def _records(root, create=False, work_id=None):
    path = Path(root)/'pull_request'
    if (path/'manifest.jsonl').is_file():
        return WorkWorkspace.open(path, CATALOG)
    if not create:
        return None
    records = WorkWorkspace.open(path, CATALOG)
    records.initialize_request('Pull request records of work %s.\n' % work_id)
    return records


def _history(records):
    if records is None:
        return []
    return [json.loads(records.read_artifact('pull_request_record', v.version)) for v in records.versions('pull_request_record')]


def _put(records, record):
    records.write_artifact('pull_request_record', json.dumps(record, sort_keys=True, indent=2)+'\n')
    return record


def _created(history):
    found = [r for r in history if r['phase'] == 'created']
    return found[-1] if found else None


def _pending(history):
    """The latest intent not yet followed by its `created` record, or None."""
    pending = None
    for record in history:
        if record['phase'] == 'intent':
            pending = record
        elif record['phase'] == 'created':
            pending = None
    return pending


# --- Create ---------------------------------------------------------------------------------------------
def _title(title, work_id):
    if title is None:
        return 'Switchyard delivery '+work_id
    title = ' '.join(title.split())
    if not title or len(title) > MAX_TITLE or any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
        raise _blocked('title_invalid: one line, 1-%d characters' % MAX_TITLE)
    return title


def _body(work_id, c, a, assessment):
    """Compact provenance only: no request text, QA feedback, prompts or runtime context."""
    return ('Switchyard-Work: %s\nSwitchyard-Accepted-Commit: %s\nSwitchyard-Base: %s\nSwitchyard-Assessment: %s\n'
            'Switchyard-Target: %s %s\n') % (work_id, c, a, assessment['assessment_id'], assessment['target_ref_full'],
                                             assessment['target_sha'])


def _assessment(root, repo, requested, work_id, c, a):
    """The latest assessment: valid, this product's, integrable and fresh. Returns it with `target_ref_full`."""
    records = integration._records(root)
    latest = records.latest_artifact('integration_assessment') if records else None
    if latest is None:
        raise _blocked('assessment_missing: run work integration-status first')
    assessment = json.loads(records.read_artifact('integration_assessment'))
    if hashlib.sha256(_canonical({k: assessment[k] for k in _IDENTITY})).hexdigest() != assessment['assessment_id']:
        raise _blocked('assessment_invalid')
    if (assessment['work_id'], assessment['accepted_commit_sha'], assessment['delivery_base_sha']) != (work_id, c, a):
        raise _blocked('assessment_product_changed')
    full = _branch(repo, assessment['target_ref'])
    if full is None:
        raise _blocked('target_not_a_branch: the assessment target is not a local branch', [assessment['target_ref']])
    if requested is not None and _branch(repo, requested) != full:
        raise _blocked('assessment_target_mismatch', [requested])
    if assessment['readiness'] == integration.ALREADY_INTEGRATED:
        raise _blocked('already_integrated', target_ref=full, target_sha=assessment['target_sha'])
    if assessment['readiness'] not in INTEGRABLE:
        raise _blocked('assessment_not_ready: '+assessment['readiness'])
    current = _commit(repo, full)
    if current != assessment['target_sha']:
        raise _blocked('assessment_stale: run work integration-status again', expected_target_sha=assessment['target_sha'],
                       actual_target_sha=current)
    return dict(assessment, target_ref_full=full, assessment_version=latest.version)


def _remote(repo, remote, provider):
    try:
        push_url, identity = publication._remote(repo, remote)
    except RecoveryBlocked as exc:
        raise _as_pr(exc) from None
    repository = provider.repository(push_url)
    if repository is None:
        raise _blocked('unsupported_pr_provider', [identity])
    return push_url, identity, repository


def _remote_sha(repo, url, ref):
    try:
        return publication._remote_sha(repo, url, ref)
    except RecoveryBlocked as exc:
        raise _as_pr(exc) from None


def _directly_published(root, repo, push_url, identity):
    """Durable direct-push provenance, checked before the readiness gate (a direct integration makes the
    assessment stale): this work's integration N was pushed to this endpoint and the remote branch still holds
    it (is N, or descends from it as far as local objects can prove)."""
    for record in reversed(publication._history(publication._records(root))):
        if record['phase'] == 'pushed' and record['remote_identity'] == identity:
            actual, n = _remote_sha(repo, push_url, record['remote_ref']), record['remote_after_sha']
            if actual == n or (actual and _commit(repo, actual) == actual and _ancestor(repo, n, actual)):
                return dict(target_ref=record['remote_ref'], remote_target_sha=actual, pushed_commit_sha=n)
    return None


def _call(provider, method, *args):
    try:
        return getattr(provider, method)(*args)
    except PullRequestProviderError as exc:
        raise _blocked(exc.reason, diagnostic=exc.diagnostic) from None


def _compatible(pull, intent):
    return (pull['repository'] == intent['repository'] and pull['head_repository'] == intent['repository']
            and pull['head_ref'] == intent['head_ref'] and pull['base_ref'] == intent['target_ref'])


def _create(repo, records, intent, provider, push_url, head_before=None):
    """From a durable intent: head at C (published if missing), then exactly one PR (found or created).
    `head_before`: the remote head read just before a fresh intent (None on a retry: the head is ours)."""
    head, c = intent['head_ref'], intent['head_sha']
    current = _remote_sha(repo, push_url, head)
    if current is None:
        failure = publication._push(repo, intent['remote'], head, c, '')  # Lease `<head>:`: must still not exist.
        if failure is not None:
            reason, diagnostic, definite = failure
            if definite and reason == 'remote_moved':
                raise _blocked('pr_head_moved', expected=c, actual=_remote_sha(repo, push_url, head))
            raise _blocked(reason, diagnostic=diagnostic)
        _put(records, dict(intent, phase='head_published', head_published_by='switchyard'))
    elif current != c:
        raise _blocked('pr_head_moved', expected=c, actual=current)
    elif not any(r['phase'] == 'head_published' and r['head_sha'] == c for r in _history(records)):
        _put(records, dict(intent, phase='head_published', head_published_by='already_present' if head_before == c else 'switchyard'))
    # Search before every create: a crash or a lost response may have left the PR on the provider already.
    candidates = [p for p in _call(provider, 'find', intent['repository'], head, intent['target_ref']) if _compatible(p, intent)]
    if len(candidates) > 1:
        raise _blocked('pr_reconciliation_ambiguous', numbers=sorted(p['number'] for p in candidates))
    if candidates:
        pull, created_by = candidates[0], 'reconciled'
    else:
        pull, created_by = _call(provider, 'create', intent['repository'], head, intent['target_ref'], intent['title'],
                                 intent['body']), 'switchyard'
    if not _compatible(pull, intent) or pull['head_sha'] != c:
        raise _blocked('pr_reconciliation_ambiguous', number=pull['number'], head_sha=pull['head_sha'], expected_head_sha=c)
    return _put(records, dict(intent, phase='created', created_by=created_by, pull_request=_identity(pull),
                              observed=_observed(pull, intent, c)))


def _identity(pull):
    return {k: pull[k] for k in ('provider', 'repository', 'number', 'provider_id', 'url', 'head_ref', 'base_ref')}


def pr_create_v2_work(work_root, remote, target_ref=None, title=None, provider=None):
    """Open (or find) the pull request C -> target for an accepted work. Returns the `created` record; refusals
    are `PR_BLOCKED`. Never integrates, pushes the target, merges or invokes a model provider."""
    provider = provider or default_provider()
    root = Path(work_root).resolve()
    with _lock(root):
        try:
            store, iso, op = _accepted_work(root)
            repo = _repo(iso)
            c, a = _prove(store, iso, op, repo)
        except RecoveryBlocked as exc:
            raise _as_pr(exc) from None
        work_id = _work_id(iso)
        push_url, identity, repository = _remote(repo, remote, provider)
        records = _records(root)
        history = _history(records)
        created = _created(history)
        if created is not None:  # Idempotent: one durable PR per work, returned without any remote call.
            if (created['remote_identity'], created['repository']) != (identity, repository) or (
                    target_ref is not None and _branch(repo, target_ref) != created['target_ref']):
                raise _blocked('pull_request_exists', number=created['pull_request']['number'], url=created['pull_request']['url'])
            return created
        pending = _pending(history)
        if pending is not None:  # Finish the recorded intent exactly as frozen.
            if pending['accepted_commit_sha'] != c or (pending['remote_identity'], pending['repository']) != (identity, repository):
                raise _blocked('pr_reconciliation_ambiguous: the pending pull request is for another product or remote')
            return _create(repo, records, pending, provider, push_url)
        published = _directly_published(root, repo, push_url, identity)
        if published is not None:
            raise _blocked('already_integrated', **published)
        assessment = _assessment(root, repo, target_ref, work_id, c, a)
        full, t = assessment['target_ref_full'], assessment['target_sha']
        actual = _remote_sha(repo, push_url, full)
        if actual is not None and _commit(repo, actual) == actual and _ancestor(repo, c, actual):
            raise _blocked('already_integrated', target_ref=full, remote_target_sha=actual)
        if actual is None:
            raise _blocked('remote_target_missing', [remote+' '+full])
        if actual != t:
            raise _blocked('target_moved', expected_target_sha=t, actual_target_sha=actual)
        head = iso['delivery_branch']
        remote_head = _remote_sha(repo, push_url, head)
        if remote_head not in (None, c):
            raise _blocked('pr_head_moved', expected=c, actual=remote_head)
        body = _body(work_id, c, a, assessment)
        intent = dict(schema=PR_SCHEMA, phase='intent', work_id=work_id, provider=provider.name, provider_schema=provider.identity,
                      repository=repository, remote=remote, remote_identity=identity, accepted_commit_sha=c, delivery_base_sha=a,
                      assessment_id=assessment['assessment_id'], assessment_version=assessment['assessment_version'],
                      readiness=assessment['readiness'], target_ref=full, target_sha=t, head_ref=head, head_sha=c,
                      title=_title(title, work_id), body=body, body_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest())
        records = records or _records(root, create=True, work_id=work_id)  # Only now is anything recorded.
        _put(records, intent)
        return _create(repo, records, intent, provider, push_url, remote_head)


# --- Observe --------------------------------------------------------------------------------------------
def _observed(pull, created, remote_head):
    """The normalized, material view of one provider reading against the durable identity."""
    c = created['head_sha']
    deleted_after_merge = pull['state'] == MERGED and remote_head is None  # Normal head-branch clean-up.
    drift = pull['head_sha'] != c or (remote_head != c and not deleted_after_merge)
    return dict(number=pull['number'], state=pull['state'], draft=pull['draft'], mergeability=pull['mergeability'],
                head_ref=pull['head_ref'], head_expected_sha=c, head_observed_sha=pull['head_sha'], remote_head_sha=remote_head,
                head_drift=drift, base_ref=pull['base_ref'], base_sha=pull['base_sha'],
                base_moved=pull['base_sha'] is not None and pull['base_sha'] != created['target_sha'],
                merged_commit_sha=pull['merged_commit_sha'], merged_at=pull['merged_at'], closed_at=pull['closed_at'],
                merged_by=pull['merged_by'])


def pr_status_v2_work(work_root, provider=None):
    """Read the provider's current view of this work's pull request (and its head on the remote) and record it
    when it materially changed. Never merges, closes, reopens, edits or pushes anything."""
    provider = provider or default_provider()
    root = Path(work_root).resolve()
    with _lock(root):
        records = _records(root)
        history = _history(records)
        created = _created(history)
        if created is None:
            raise _blocked('pr_create_pending: retry work pr-create' if _pending(history) else 'no_pull_request: run work pr-create first')
        try:
            repo = _repo(_accepted_work(root)[1])
        except RecoveryBlocked as exc:
            raise _as_pr(exc) from None
        push_url, identity, repository = _remote(repo, created['remote'], provider)
        if (identity, repository) != (created['remote_identity'], created['repository']):
            raise _blocked('remote_changed: the remote no longer names the pull request repository', [created['remote']])
        pull = _call(provider, 'get', repository, created['pull_request']['number'])
        if (pull['repository'], pull['number'], pull['head_ref']) != (repository, created['pull_request']['number'], created['head_ref']):
            raise _blocked('pr_identity_mismatch', number=pull['number'])
        observed = _observed(pull, created, _remote_sha(repo, push_url, created['head_ref']))
        last = [r['observed'] for r in history if r['phase'] in ('created', 'observed')][-1]
        changed = {k: observed[k] for k in _MATERIAL} != {k: last.get(k) for k in _MATERIAL}
        if changed:
            _put(records, dict(schema=PR_SCHEMA, phase='observed', work_id=created['work_id'], provider=created['provider'],
                               repository=repository, pull_request=created['pull_request'],
                               accepted_commit_sha=created['accepted_commit_sha'], observed=observed))
        return dict(observed, provider=created['provider'], repository=repository, url=created['pull_request']['url'],
                    accepted_commit_sha=created['accepted_commit_sha'], target_ref=created['target_ref'],
                    created_target_sha=created['target_sha'], recorded=changed)


def pull_request_summary(work_root):
    """Read-only, local only (never contacts the provider or a remote): the compact PR state, or None."""
    history = _history(_records(Path(work_root).resolve()))
    if not history:
        return None
    created = _created(history)
    if created is None:
        pending = _pending(history)
        return dict(state='PR_PENDING', provider=pending['provider'], head_ref=pending['head_ref'], head_sha=pending['head_sha'],
                    head_published=any(r['phase'] == 'head_published' for r in history), target_ref=pending['target_ref'])
    observed = [r['observed'] for r in history if r['phase'] in ('created', 'observed')][-1]
    return dict(state=observed['state'], provider=created['provider'], repository=created['repository'],
                number=created['pull_request']['number'], url=created['pull_request']['url'], head_ref=created['head_ref'],
                head_sha=created['head_sha'], accepted_commit_sha=created['accepted_commit_sha'], target_ref=created['target_ref'],
                target_sha=created['target_sha'], head_drift=observed['head_drift'], merged_commit_sha=observed['merged_commit_sha'])

"""INTEGRATION (#9): deterministic integration readiness of an accepted delivery (slice 1, read-only) and the
compare-and-swap integration that consumes it (slice 2, `integrate_v2_work`, below).

Question: can the accepted delivery commit C (base A) be integrated into the target T a local ref names?

- Eligible: an ISOLATED work whose operation is `accepted` (terminal ACCEPTED). Anything else is
  `INTEGRATION_BLOCKED: work_not_accepted`. Acceptance itself never looks at the target.
- C is re-proven from the durable acceptance contract before anything is assessed (commit exists, single
  parent = the accepted candidate's base head, which descends from A; Switchyard's acceptance message; exactly
  the candidate's paths, bytes and modes; the QA decision names C). Broken provenance is
  `INTEGRATION_BLOCKED: accepted_commit_invalid` and nothing is repaired. The delivery branch is observed only:
  C, not the branch's current head, is the integration product.
- The target ref is resolved once to an exact commit T; every decision is about T. Relationship of A, C, T:
    T == A                  READY               drift none      strategy fast_forward
    C ancestor of T         ALREADY_INTEGRATED  drift integrated
    A ancestor of T, clean  READY_WITH_DRIFT    drift clean     strategy three_way (result tree)
    A ancestor of T, else   BLOCKED_CONFLICT    drift conflicting  (normalized conflicts)
    otherwise               BLOCKED_LINEAGE     drift unsupported_lineage
  The 3-way check is continuity's engine (`continuity.three_way`, BASE A, OURS T, THEIRS C), so transplant and
  integration report the same conflicts for the same change. No provider, no model, no fetch.
- Read-only for the repository and the source checkout: no ref, HEAD, index, worktree or file changes
  (merge-tree only adds unreferenced objects to the object store). The source checkout's branch or dirt is
  irrelevant: analysis runs against the shared repository.
- Durable: each distinct assessment is an immutable, versioned record in the work's own `integration/` store,
  identified by the SHA-256 of its canonical inputs and outcome (no timestamps); an unchanged re-assessment
  reproduces the latest identity and writes nothing. `integration_status` re-resolves the target ref and marks
  the latest record stale when the ref no longer names its T.
"""
import hashlib
import json
import os
from pathlib import Path

from .continuity import three_way, _plumb
from .qa_lifecycle import LifecycleError, _load as _load_candidate
from .recovery import RecoveryBlocked, _Repo, _acceptance_message, _lock, _read, _store, _worktrees
from .work_contract import ArtifactSpec, WorkWorkspace

INTEGRATION_SCHEMA = 1
I = 'INTEGRATION_BLOCKED'
READY, READY_WITH_DRIFT, ALREADY_INTEGRATED = 'READY', 'READY_WITH_DRIFT', 'ALREADY_INTEGRATED'
BLOCKED_CONFLICT, BLOCKED_LINEAGE = 'BLOCKED_CONFLICT', 'BLOCKED_LINEAGE'
INTEGRABLE = (READY, READY_WITH_DRIFT)
CATALOG = {'request': ArtifactSpec('text', source='request'), 'integration_assessment': ArtifactSpec('json'),
           'integration_record': ArtifactSpec('json')}
DELIVERY_BRANCH_PREFIX = 'refs/heads/switchyard/'
_IDENTITY = ('schema', 'work_id', 'accepted_commit_sha', 'delivery_base_sha', 'target_ref', 'target_sha',
             'readiness', 'drift', 'strategy', 'result_tree', 'conflicts')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _work_id(iso):
    """Durable work id: frozen into the delivery branch name when the delivery started, never the current
    directory name of the work root."""
    return iso['delivery_branch'][len(DELIVERY_BRANCH_PREFIX):]


def _invalid(detail):
    raise RecoveryBlocked(I, 'accepted_commit_invalid: '+detail)


def _accepted_work(root):
    """(store, isolation, operation) of an accepted isolated work, read without reconciling anything."""
    try:
        store = _store(root); base = _read(store, 'baseline'); op = _read(store, 'operation')
    except (RecoveryBlocked, ValueError, OSError):
        raise RecoveryBlocked(I, 'work_not_accepted: no recovery record') from None
    iso = base.get('isolation')
    if iso is None:
        raise RecoveryBlocked(I, 'work_not_accepted: not an isolated delivery')
    if op.get('phase') != 'accepted' or op.get('terminal') != 'ACCEPTED':
        raise RecoveryBlocked(I, 'work_not_accepted: '+str(op.get('terminal') or op.get('phase')))
    return store, iso, op


def _repo(iso):
    if not Path(iso['repository']).is_dir():
        raise RecoveryBlocked(I, 'repository_unavailable', [iso['repository']])
    return _Repo(iso['repository'])


def _commit(repo, name):
    """Exact commit a revision names, or None. Option-like names are never passed to Git as options."""
    if not name or name.startswith('-'):
        return None
    code, out = repo.probe('rev-parse', '--verify', '--quiet', '--end-of-options', name+'^{commit}')
    return out.decode().strip() if code == 0 else None


def _ancestor(repo, older, newer):
    return repo.probe('merge-base', '--is-ancestor', older, newer)[0] == 0


def _prove(store, iso, op, repo):
    """Re-prove C from durable records and Git objects alone; never trusts the stored SHA by itself."""
    c, a = op.get('accepted_commit_sha'), op.get('base_sha')
    if not c or (a, op.get('delivery_branch')) != (iso['base_sha'], iso['delivery_branch']):
        _invalid('acceptance record disagrees with the delivery isolation')
    try:
        ref = _read(store, 'qa_reference')
        qa, candidate, identity = _load_candidate(ref['workspace'])
        decision = _read(qa, 'qa_decision') if qa.versions('qa_decision') else None
    except (RecoveryBlocked, LifecycleError, ValueError, OSError):
        _invalid('accepted candidate unavailable')
    if identity != ref['identity'] or identity != op.get('accepted_candidate_identity'):
        _invalid('accepted candidate identity differs')
    if not decision or decision.get('terminal') != 'ACCEPTED' or decision.get('committed_head') != c:
        _invalid('QA decision does not name the accepted commit')
    if candidate['branch'] != iso['delivery_branch']:
        _invalid('candidate not on the delivery branch')
    if _commit(repo, c) != c:
        _invalid('commit missing')
    parent = candidate['base_head']
    if repo.probe('rev-list', '--parents', '-n', '1', c)[1].decode().split() != [c, parent] or not _ancestor(repo, a, parent):
        _invalid('commit is not a single commit on the delivery base')
    code, raw = repo.probe('cat-file', 'commit', c)
    if code or raw.split(b'\n\n', 1)[-1] != _acceptance_message(_work_id(iso), identity, candidate).encode('utf-8'):
        _invalid('commit message is not the acceptance of this work')
    code, names = repo.probe('diff-tree', '-r', '--no-renames', '--name-only', '-z', parent, c)
    if code or sorted(n.decode('utf-8', 'surrogateescape') for n in names.split(b'\0') if n) != sorted(candidate['files']):
        _invalid('commit paths differ from the accepted candidate')
    code, listing = repo.probe('ls-tree', '-r', '-z', c)
    entries = {}
    for entry in listing.split(b'\0') if code == 0 else ():
        if entry:
            meta, path = entry.split(b'\t', 1); mode, kind, oid = meta.decode().split()
            entries[path.decode('utf-8', 'surrogateescape')] = (mode, kind, oid)
    for path, fingerprint in candidate['files'].items():
        found = entries.get(path)
        if fingerprint['kind'] == 'deleted':
            if found is not None:
                _invalid('commit content differs from the accepted candidate')
            continue
        blob = repo.probe('cat-file', 'blob', found[2]) if found and found[1] == 'blob' else (1, b'')
        if found is None or found[0] != fingerprint['mode'] or blob[0] or hashlib.sha256(blob[1]).hexdigest() != fingerprint['sha256']:
            _invalid('commit content differs from the accepted candidate')
    return c, a


def _delivery_branch(repo, branch, c):
    """Observation only: where the delivery branch is now relative to C (never reset, never authoritative)."""
    head = _commit(repo, branch)
    state = ('missing' if head is None else 'at_accepted' if head == c else 'advanced' if _ancestor(repo, c, head)
             else 'behind' if _ancestor(repo, head, c) else 'diverged')
    return dict(delivery_branch=branch, delivery_branch_sha=head, delivery_branch_state=state)


def _classify(repo, a, c, t):
    if t == a:
        tree = repo.probe('rev-parse', '--verify', '--quiet', c+'^{tree}')[1].decode().strip()
        return dict(readiness=READY, drift='none', strategy='fast_forward', result_tree=tree, conflicts=[])
    if _ancestor(repo, c, t):
        return dict(readiness=ALREADY_INTEGRATED, drift='integrated', strategy=None, result_tree=None, conflicts=[])
    if not _ancestor(repo, a, t):
        return dict(readiness=BLOCKED_LINEAGE, drift='unsupported_lineage', strategy=None, result_tree=None, conflicts=[])
    tree, conflicts = three_way(repo, a, t, c, terminal=I, failure='integration_assessment_failed')
    return dict(readiness=BLOCKED_CONFLICT if conflicts else READY_WITH_DRIFT, drift='conflicting' if conflicts else 'clean',
                strategy='three_way', result_tree=tree, conflicts=conflicts)


def _records(root):
    """The work's integration store, or None before the first assessment (opening never creates it)."""
    path = Path(root)/'integration'
    return WorkWorkspace.open(path, CATALOG) if (path/'manifest.jsonl').is_file() else None


def integration_readiness(work_root, target_ref=None):
    """Assess (and durably record) whether the accepted commit C integrates into `target_ref` -- a local
    branch, ref or commit; default: the source branch recorded when the delivery started. Returns the
    assessment; `INTEGRATION_BLOCKED` only for an ineligible work, broken acceptance or an unknown target."""
    root = Path(work_root).resolve()
    with _lock(root):  # Excludes this work's controllers; the lock file exists for every work that ran.
        store, iso, op = _accepted_work(root)
        repo = _repo(iso)
        c, a = _prove(store, iso, op, repo)
        target_ref = target_ref or iso['source_branch']
        t = _commit(repo, target_ref)
        if t is None:
            raise RecoveryBlocked(I, 'target_not_found', [target_ref])
        record = dict(schema=INTEGRATION_SCHEMA, work_id=_work_id(iso), accepted_commit_sha=c, delivery_base_sha=a,
                      target_ref=target_ref, target_sha=t, **_classify(repo, a, c, t))
        record['assessment_id'] = hashlib.sha256(_canonical({k: record[k] for k in _IDENTITY})).hexdigest()
        records = _records(root)
        latest = records.latest_artifact('integration_assessment') if records else None
        if latest is None or json.loads(records.read_artifact('integration_assessment'))['assessment_id'] != record['assessment_id']:
            if records is None:
                records = WorkWorkspace.open(root/'integration', CATALOG)
                records.initialize_request('Integration records of work %s.\n' % _work_id(iso))
            latest = records.write_artifact('integration_assessment', json.dumps(record, sort_keys=True, indent=2)+'\n')
        return dict(record, assessment_version=latest.version, current_target_sha=t, stale=False,
                    **_delivery_branch(repo, iso['delivery_branch'], c))


def integration_status(work_root):
    """Read-only: the latest recorded assessment and whether it still describes its target (None if never
    assessed). `stale` is true when the target ref no longer resolves to the assessed T."""
    root = Path(work_root).resolve()
    records = _records(root)
    if records is None or records.latest_artifact('integration_assessment') is None:
        return None
    latest = records.latest_artifact('integration_assessment')
    record = json.loads(records.read_artifact('integration_assessment'))
    try:
        current = _commit(_repo(_read(_store(root), 'baseline')['isolation']), record['target_ref'])
    except (RecoveryBlocked, ValueError, OSError, KeyError, TypeError):
        current = None
    return dict(record, assessment_version=latest.version, current_target_sha=current, stale=current != record['target_sha'])


# --- Integration (#9 slice 2): the first mutation ------------------------------------------------------
# Consumes the latest FRESH assessment for the requested branch; never reassesses. Policy (C is never
# rewritten): READY -> fast-forward the target A -> C; READY_WITH_DRIFT -> one deterministic two-parent merge
# commit M (parents T, C; tree exactly the assessment's result tree; fixed identity and epoch; message
# naming the work, C and the assessment), built as raw bytes so the same inputs always give the same M;
# ALREADY_INTEGRATED -> recorded as already present, nothing created. The target must be a local branch that
# no worktree has checked out. The ref moves only by compare-and-swap from exactly T (`update-ref NEW T`,
# hooks disabled). Durable order: M object -> `intent` record -> CAS -> `integrated` record. A retry with a
# pending intent recomputes M from the intent alone: target == T -> CAS again; target == M -> finalize; anything
# else is `integration_reconciliation_ambiguous` and nothing is reset. A lost CAS race records `aborted`.
INTEGRATION_RECORD = 'integration_record'
_IDENT = 'Switchyard <switchyard@invalid> 0 +0000'


def _blocked(reason, paths=(), **details):
    error = RecoveryBlocked(I, reason, paths)
    if details:
        error.integration = details
    return error


def _branch(repo, name):
    """Full name of the local branch `name` denotes, or None (commits, tags, remotes and HEAD are not branches)."""
    if not name or name.startswith('-'):
        return None
    code, out = repo.probe('rev-parse', '--verify', '--quiet', '--symbolic-full-name', '--end-of-options', name)
    full = out.decode().strip() if code == 0 else ''
    return full if full.startswith('refs/heads/') else None


def _merge_bytes(intent):
    message = ('switchyard: integrate work %s into %s\n\nSwitchyard-Work: %s\nSwitchyard-Accepted-Commit: %s\n'
               'Switchyard-Assessment: %s\n') % (intent['work_id'], intent['target_ref_full'], intent['work_id'],
                                                  intent['accepted_commit_sha'], intent['consumed_assessment_id'])
    return ('tree %s\nparent %s\nparent %s\nauthor %s\ncommitter %s\n\n%s' % (
        intent['result_tree'], intent['expected_target_sha'], intent['accepted_commit_sha'], _IDENT, _IDENT, message)).encode('utf-8')


def _merge_commit(repo, intent, write):
    """The deterministic integration commit M of an intent (written to the object store when `write`)."""
    code, out = _plumb(repo, 'hash-object', '-t', 'commit', *(['-w'] if write else []), '--stdin',
                       input=_merge_bytes(intent), terminal=I, failure='integration_failed')
    if code:
        raise _blocked('integration_failed: merge commit')
    return out.decode().strip()


# --- Checked-out target (#9 slice 3) ----------------------------------------------------------------
# A target branch checked out in exactly one clean worktree is integrated by installing the predetermined N
# (C or M) there; never by `git merge`. Ownership boundary: the checkout is proven clean at exactly T (HEAD on
# the branch, index == worktree == T, no untracked or ignored file where N adds a path, no Git operation in
# progress) and the intent naming it (path + Git admin dir) is durable; only then does anything change. Order:
# intent -> CAS branch T -> N (the race decision: a lost CAS touches no file) -> `read-tree -m -u T N` in the
# checkout (Git's own two-tree switch: refuses to overwrite untracked files) -> verified clean at N -> final
# record. Between the CAS and the install the checkout shows N's HEAD with T's files; a concurrent Git command
# in that checkout during that window is outside the supported concurrency contract.
def _target_worktree(repo, full):
    """The one worktree with `full` checked out, or None. More than one is never guessed between."""
    owners = sorted(path for path, branch in _worktrees(repo).items() if branch == full)
    if len(owners) > 1:
        raise _blocked('target_worktree_ambiguous', owners)
    return owners[0] if owners else None


def _checkout(repo, path, git_dir=None):
    """(worktree repo, identity) of a checkout of this repository; with `git_dir`, it must be that same one."""
    p = Path(path)
    if p.is_symlink() or not p.is_dir():
        raise _blocked('target_worktree_unavailable', [str(p)])
    wt = _Repo(p)
    code, top = wt.probe('rev-parse', '--show-toplevel')
    code2, admin = wt.probe('rev-parse', '--absolute-git-dir')
    if code or code2 or Path(os.fsdecode(top.strip())).resolve() != p.resolve() or wt.common_dir() != repo.common_dir():
        raise _blocked('target_worktree_unavailable', [str(p)])
    identity = dict(target_worktree=str(p.resolve()), target_worktree_git_dir=str(Path(os.fsdecode(admin.strip())).resolve()))
    if git_dir is not None and identity['target_worktree_git_dir'] != git_dir:
        raise _blocked('integration_reconciliation_ambiguous: target worktree identity changed', [str(p)])
    return wt, identity


def _entries(repo, rev):
    entries = {}
    for raw in repo.git('ls-tree', '-r', '-z', rev).split(b'\0'):
        if raw:
            meta, path = raw.split(b'\t', 1); mode, _, oid = meta.decode().split()
            entries[os.fsdecode(path)] = (mode, oid)
    return entries


def _lines(raw):
    return sorted({os.fsdecode(p) for p in raw.split(b'\0') if p})


def _clean_at(wt, full, t, n):
    """The ownership precondition: the checkout is on `full` at exactly T with nothing local to lose."""
    if wt.probe('symbolic-ref', '--quiet', 'HEAD')[1].decode().strip() != full:
        raise _blocked('target_moved: target worktree left the branch', [str(wt.workspace)])
    head = _commit(wt, 'HEAD')
    if head != t:
        raise _blocked('target_moved', [str(wt.workspace)], expected_target_sha=t, actual_target_sha=head)
    if Path(str(wt.index_path())+'.lock').exists():
        raise _blocked('target_worktree_locked: another Git process may be running', [str(wt.workspace)])
    try:
        wt.check_repository()  # Merge/rebase/cherry-pick/revert/bisect markers, unmerged index, submodules.
    except (ValueError, RecoveryBlocked) as exc:
        raise _blocked('target_worktree_dirty: '+str(exc)[:200], [str(wt.workspace)]) from None
    dirty = wt.git('status', '--porcelain=v1', '-z', '--no-renames', '--untracked-files=all')
    if dirty:
        raise _blocked('target_worktree_dirty', [os.fsdecode(e[3:]) for e in dirty.split(b'\0') if e][:50])
    before, after = _entries(wt, t), _entries(wt, n)
    collisions = [p for p in sorted(set(after)-set(before)) if os.path.lexists(wt.workspace/p)]
    if collisions:  # Ignored files in N's way would be overwritten silently by the switch.
        raise _blocked('target_worktree_dirty: ignored files where the integration adds paths', collisions)


def _checkout_state(wt, t, n):
    """'N' / 'T' (index == worktree == that tree, nothing untracked), 'partial' (index T; every other
    difference is a path T -> N changes holding exactly N's content: an interrupted install), else None."""
    wt.probe('update-index', '-q', '--refresh')  # Owned checkout: stat refresh only, no content change.
    untracked = _lines(wt.git('ls-files', '--others', '--exclude-standard', '-z'))
    files_clean = wt.probe('diff-files', '--quiet')[0] == 0
    for tree, name in ((n, 'N'), (t, 'T')):
        if files_clean and not untracked and wt.probe('diff-index', '--cached', '--quiet', tree, '--')[0] == 0:
            return name
    if wt.probe('diff-index', '--cached', '--quiet', t, '--')[0] != 0:
        return None
    before, after = _entries(wt, t), _entries(wt, n)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    for path in _lines(wt.git('diff-files', '--name-only', '-z')) + untracked:
        target = wt.workspace/path
        if path not in changed:
            return None
        if path not in after:
            if os.path.lexists(target):
                return None
            continue
        mode, oid = after[path]
        if not os.path.lexists(target):
            return None
        if mode == '120000':
            code, out = _plumb(wt, 'hash-object', '--stdin', input=os.fsencode(os.readlink(target)), terminal=I) if target.is_symlink() else (1, b'')
        else:
            code, out = wt.probe('hash-object', '--no-filters', '--', path) if target.is_file() and not target.is_symlink() else (1, b'')
            if code == 0 and (mode == '100755') != bool(target.stat().st_mode & 0o100):
                return None
        if code or out.decode().strip() != oid:
            return None
    return 'partial'


def _install(wt, t, n, state):
    """Install N into the owned checkout from the proven state, then require it clean at N."""
    if state == 'T':
        before, after = _entries(wt, t), _entries(wt, n)
        collisions = [p for p in sorted(set(after)-set(before)) if os.path.lexists(wt.workspace/p)]
        if collisions:
            raise _blocked('integration_reconciliation_ambiguous: files appeared where the integration adds paths', collisions)
        wt.git('read-tree', '-m', '-u', t, n)  # Two-tree switch; refuses to clobber untracked files.
    elif state == 'partial':
        wt.git('read-tree', '--reset', '-u', n)  # Every deviation from T was proven to be N's own bytes.
    if _checkout_state(wt, t, n) != 'N':
        raise _blocked('integration_failed: target worktree is not clean at the integrated commit', [str(wt.workspace)])


def _not_checked_out(repo, full):
    owners = sorted(path for path, branch in _worktrees(repo).items() if branch == full)
    if owners:
        raise _blocked('target_checked_out', owners)


def _cas(repo, full, new, expected, work_id):
    """Atomic compare-and-swap of the target branch: succeeds only while it still points at `expected`."""
    code, _ = repo.probe('-c', 'core.hooksPath='+os.devnull, 'update-ref', '-m', 'switchyard: integrate work '+work_id,
                         '--end-of-options', full, new, expected)
    return code == 0


def _put(records, record):
    records.write_artifact(INTEGRATION_RECORD, json.dumps(record, sort_keys=True, indent=2)+'\n')
    return record


def _history(records, full=None):
    if records is None:
        return []
    found = [json.loads(records.read_artifact(INTEGRATION_RECORD, v.version)) for v in records.versions(INTEGRATION_RECORD)]
    return [r for r in found if full is None or r['target_ref_full'] == full]


def _finalize(records, intent):
    record = {k: v for k, v in intent.items() if k != 'planned_commit_sha'}
    record.update(phase='integrated', integrated_by='switchyard', target_before_sha=intent['expected_target_sha'],
                  target_after_sha=intent['planned_commit_sha'], integrated_commit_sha=intent['planned_commit_sha'],
                  target_worktree_updated=intent.get('mode') == 'checked_out_worktree')
    return _put(records, record)


def _abort(records, intent, reason, error):
    _put(records, dict(intent, phase='aborted', reason=reason))
    raise error


def _complete(repo, records, intent):
    """From the old state (ref == T, checkout at T if any): CAS, install, finalize."""
    full, t, new = intent['target_ref_full'], intent['expected_target_sha'], intent['planned_commit_sha']
    wt = None
    if intent.get('mode') == 'checked_out_worktree':
        wt, _ = _checkout(repo, intent['target_worktree'], intent['target_worktree_git_dir'])
        try:
            _clean_at(wt, full, t, new)
        except RecoveryBlocked as exc:
            _abort(records, intent, exc.reason, exc)  # Still before any change: give the operation up.
    else:
        try:
            _not_checked_out(repo, full)
        except RecoveryBlocked as exc:
            _abort(records, intent, exc.reason, exc)
    if not _cas(repo, full, new, t, intent['work_id']):
        actual = _commit(repo, full)
        _abort(records, intent, 'target_moved', _blocked('target_moved', expected_target_sha=t, actual_target_sha=actual))
    if wt is not None:
        _install(wt, t, new, 'T')
    return _finalize(records, intent)


def _reconcile(repo, records, intent):
    """A pending intent (crash between intent and final record): prove and finish it, never redo it."""
    full, t, new = intent['target_ref_full'], intent['expected_target_sha'], intent['planned_commit_sha']
    if intent['strategy'] == 'merge' and _merge_commit(repo, intent, write=False) != new:
        raise _blocked('integration_reconciliation_ambiguous: recorded merge commit is not the intent\'s')
    current = _commit(repo, full)
    ambiguous = lambda detail='': _blocked('integration_reconciliation_ambiguous'+detail, expected_target_sha=t,
                                           planned_commit_sha=new, actual_target_sha=current)
    if current not in (t, new):
        raise ambiguous()
    if intent.get('mode') != 'checked_out_worktree':
        if current == new:
            return _finalize(records, intent)  # The ref moved before the crash: same M, no second commit.
        if intent['strategy'] == 'merge':
            _merge_commit(repo, intent, write=True)  # Same bytes, same object: reused, never a second M.
        return _complete(repo, records, intent)
    wt, _ = _checkout(repo, intent['target_worktree'], intent['target_worktree_git_dir'])
    if wt.probe('symbolic-ref', '--quiet', 'HEAD')[1].decode().strip() != full:
        raise ambiguous(': target worktree left the branch')
    lock = Path(str(wt.index_path())+'.lock')
    if current == t:
        if lock.exists() or _checkout_state(wt, t, new) != 'T':
            raise ambiguous(': checkout changed although the branch was never moved')
        if intent['strategy'] == 'merge':
            _merge_commit(repo, intent, write=True)
        return _complete(repo, records, intent)
    # current == N: Switchyard's own CAS happened; the checkout is ours until it is clean at N. A Git lock is
    # never ours to remove: nothing proves which process holds it now, so recovery waits for a human.
    if lock.exists():
        raise _blocked('target_worktree_locked: remove the stale Git index lock only after verifying no Git process '
                       'uses this checkout, then retry', [str(wt.workspace)])
    state = _checkout_state(wt, t, new)
    if state is None:
        raise ambiguous(': checkout holds changes that are not part of this integration')
    if state != 'N':
        _install(wt, t, new, state)
    return _finalize(records, intent)


def integrate_v2_work(work_root, target_ref=None):
    """Move the target branch to the accepted delivery by consuming its latest fresh readiness assessment.
    Default target: the source branch recorded when the delivery started. Returns the integration record;
    refusals are `INTEGRATION_BLOCKED`. Never reassesses, rebases, pushes or invokes a provider."""
    root = Path(work_root).resolve()
    with _lock(root):
        store, iso, op = _accepted_work(root)
        repo = _repo(iso); work_id = _work_id(iso)
        c, a = _prove(store, iso, op, repo)
        requested = target_ref or iso['source_branch']
        full = _branch(repo, requested)
        if full is None:
            raise _blocked('target_not_found' if _commit(repo, requested) is None else 'target_not_a_branch', [requested])
        records = _records(root)
        history = _history(records, full)
        if history and history[-1]['phase'] == 'integrated':
            return history[-1]  # Idempotent: already done for this target.
        if history and history[-1]['phase'] == 'intent':
            if history[-1]['accepted_commit_sha'] != c:
                _invalid('accepted commit differs from the pending integration')
            return _reconcile(repo, records, history[-1])
        latest = records.latest_artifact('integration_assessment') if records else None
        if latest is None:
            raise _blocked('assessment_missing: run work integration-status first')
        assessment = json.loads(records.read_artifact('integration_assessment'))
        if hashlib.sha256(_canonical({k: assessment[k] for k in _IDENTITY})).hexdigest() != assessment['assessment_id']:
            raise _blocked('assessment_invalid')
        if _branch(repo, assessment['target_ref']) != full:
            raise _blocked('assessment_target_mismatch', [assessment['target_ref']])
        if (assessment['work_id'], assessment['accepted_commit_sha'], assessment['delivery_base_sha']) != (work_id, c, a):
            raise _blocked('assessment_product_changed')
        t = assessment['target_sha']; current = _commit(repo, full)
        if current != t:
            raise _blocked('target_moved', expected_target_sha=t, actual_target_sha=current)
        base = dict(schema=INTEGRATION_SCHEMA, work_id=work_id, target_ref=requested, target_ref_full=full,
                    expected_target_sha=t, accepted_commit_sha=c, delivery_base_sha=a, consumed_assessment_id=assessment['assessment_id'],
                    consumed_assessment_version=latest.version, result_tree=assessment['result_tree'])
        readiness = assessment['readiness']
        owner = _target_worktree(repo, full)
        if readiness == ALREADY_INTEGRATED:
            if not _ancestor(repo, c, t):
                raise _blocked('assessment_invalid')
            observed = {}
            if owner is not None:  # Observed, never repaired: an inconsistent checkout is reported, not reset.
                wt, identity = _checkout(repo, owner)
                try:
                    _clean_at(wt, full, t, t); observed = dict(identity, target_worktree_clean=True)
                except RecoveryBlocked as exc:
                    observed = dict(identity, target_worktree_clean=False, target_worktree_problem=exc.reason)
            return _put(records, dict(base, phase='integrated', integrated_by='already_present', strategy=None, mode=None,
                                      target_before_sha=t, target_after_sha=t, integrated_commit_sha=None, already_integrated_at=t,
                                      target_worktree_updated=False, **observed))
        if readiness not in INTEGRABLE:
            raise _blocked('assessment_not_ready: '+readiness)
        if readiness == READY:
            if t != a or repo.probe('rev-parse', '--verify', '--quiet', c+'^{tree}')[1].decode().strip() != assessment['result_tree']:
                raise _blocked('assessment_invalid')
            intent = dict(base, phase='intent', strategy='fast_forward', planned_commit_sha=c)
        else:
            if repo.probe('cat-file', '-t', assessment['result_tree'] or '-')[1] != b'tree\n':
                raise _blocked('assessment_invalid: result tree unavailable')
            intent = dict(base, phase='intent', strategy='merge')
            intent['planned_commit_sha'] = _merge_commit(repo, intent, write=True)  # M before the intent and the ref.
        if owner is None:
            intent['mode'] = 'ref_only'
        else:
            wt, identity = _checkout(repo, owner)
            _clean_at(wt, full, t, intent['planned_commit_sha'])  # Before the intent: nothing owned, nothing changed.
            intent.update(mode='checked_out_worktree', **identity)
        _put(records, intent)  # Ownership boundary: from here the clean target checkout belongs to this operation.
        return _complete(repo, records, intent)


def integration_record(work_root):
    """Read-only: the latest integration record (`intent`, `integrated` or `aborted`), or None."""
    history = _history(_records(Path(work_root).resolve()))
    return history[-1] if history else None


def integration_summary(work_root):
    """Compact status view (for `recovery_status`), or None: an integration if there is one, else the latest
    assessment with its freshness. Delivery stays ACCEPTED either way: integration is a separate lifecycle."""
    record = integration_record(work_root)
    if record is not None and record['phase'] in ('integrated', 'intent'):
        try:
            repo = _repo(_read(_store(Path(work_root).resolve()), 'baseline')['isolation'])
            current = _commit(repo, record['target_ref_full'])
        except (RecoveryBlocked, ValueError, OSError, KeyError, TypeError):
            repo = current = None
        if record['phase'] == 'intent':
            return dict(state='INTEGRATION_PENDING', target_ref=record['target_ref_full'], target_before_sha=record['expected_target_sha'],
                        planned_commit_sha=record['planned_commit_sha'], strategy=record['strategy'], current_target_sha=current)
        landed = record['integrated_commit_sha'] or record['target_after_sha']
        return dict(state='INTEGRATED', integrated_by=record['integrated_by'], target_ref=record['target_ref_full'],
                    target_before_sha=record['target_before_sha'], integrated_commit_sha=record['integrated_commit_sha'],
                    strategy=record['strategy'], consumed_assessment_id=record['consumed_assessment_id'], current_target_sha=current,
                    target_contains_integration=bool(repo and current and _ancestor(repo, landed, current)),
                    target_worktree_updated=record.get('target_worktree_updated', False),
                    **({'target_worktree': record['target_worktree']} if record.get('target_worktree') else {}),
                    **({'already_integrated_at': record['already_integrated_at']} if record['integrated_by'] == 'already_present' else {}))
    status = integration_status(work_root)
    if status is None:
        return None
    return dict(state='ASSESSED', target_ref=status['target_ref'], target_sha=status['target_sha'], readiness=status['readiness'],
                drift=status['drift'], conflicts=len(status['conflicts']), current_target_sha=status['current_target_sha'],
                stale=status['stale'], assessment_id=status['assessment_id'],
                **({'last_integration_attempt': 'aborted: '+record['reason']} if record is not None else {}))

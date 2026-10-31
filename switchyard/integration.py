"""INTEGRATION (#9) slice 1: deterministic integration readiness of an accepted delivery. Nothing is merged.

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
from pathlib import Path

from .continuity import three_way
from .qa_lifecycle import LifecycleError, _load as _load_candidate
from .recovery import RecoveryBlocked, _Repo, _acceptance_message, _lock, _read, _store
from .work_contract import ArtifactSpec, WorkWorkspace

INTEGRATION_SCHEMA = 1
I = 'INTEGRATION_BLOCKED'
READY, READY_WITH_DRIFT, ALREADY_INTEGRATED = 'READY', 'READY_WITH_DRIFT', 'ALREADY_INTEGRATED'
BLOCKED_CONFLICT, BLOCKED_LINEAGE = 'BLOCKED_CONFLICT', 'BLOCKED_LINEAGE'
INTEGRABLE = (READY, READY_WITH_DRIFT)
CATALOG = {'request': ArtifactSpec('text', source='request'), 'integration_assessment': ArtifactSpec('json')}
_IDENTITY = ('schema', 'work_id', 'accepted_commit_sha', 'delivery_base_sha', 'target_ref', 'target_sha',
             'readiness', 'drift', 'strategy', 'result_tree', 'conflicts')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


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


def _prove(root, store, iso, op, repo):
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
    if code or raw.split(b'\n\n', 1)[-1] != _acceptance_message(root.name, identity, candidate).encode('utf-8'):
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
        c, a = _prove(root, store, iso, op, repo)
        target_ref = target_ref or iso['source_branch']
        t = _commit(repo, target_ref)
        if t is None:
            raise RecoveryBlocked(I, 'target_not_found', [target_ref])
        record = dict(schema=INTEGRATION_SCHEMA, work_id=root.name, accepted_commit_sha=c, delivery_base_sha=a,
                      target_ref=target_ref, target_sha=t, **_classify(repo, a, c, t))
        record['assessment_id'] = hashlib.sha256(_canonical({k: record[k] for k in _IDENTITY})).hexdigest()
        records = _records(root)
        latest = records.latest_artifact('integration_assessment') if records else None
        if latest is None or json.loads(records.read_artifact('integration_assessment'))['assessment_id'] != record['assessment_id']:
            if records is None:
                records = WorkWorkspace.open(root/'integration', CATALOG)
                records.initialize_request('Integration readiness assessments of work %s.\n' % root.name)
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


def integration_summary(work_root):
    """Compact status view of the latest assessment (for `recovery_status`), or None."""
    status = integration_status(work_root)
    if status is None:
        return None
    return dict(target_ref=status['target_ref'], target_sha=status['target_sha'], readiness=status['readiness'],
                drift=status['drift'], conflicts=len(status['conflicts']), current_target_sha=status['current_target_sha'],
                stale=status['stale'], assessment_id=status['assessment_id'])

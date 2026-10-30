"""DELIVERY-SUSPEND (#4): shelve an isolated delivery worktree durably, record SUSPENDED, then remove it.

The shelf is Switchyard-owned data under `<work_root>/suspend/<shelf-id>/`: `manifest.json` describes
the COMPLETE product state of the delivery worktree (the same Git-visible files the recovery snapshot
and candidates use) relative to the delivery branch HEAD; `blobs/<sha256>` holds every byte not already
in that commit. No stash, no commits. `resume` restores it (see `restore_suspended`).

Durable order: eligibility (no writes) -> operation `suspending` (intent: shelf id, reason, previous
operation) -> build `.<shelf-id>.<random>` -> verify -> atomic rename to `<shelf-id>` -> operation
`suspended` (shelf id + manifest digest) -> `git worktree remove` -> operation `suspended` with
`worktree_removed`. SUSPENDED therefore always references a complete, verified shelf.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from .recovery import (RecoveryBlocked, _lock, _store, _read, _operation, _operations, _check_isolation,
                       _Repo, _worktrees, _branch_head, _sha, _json, recovery_status)
from .work_contract import WorkWorkspace

SHELF_SCHEMA = 1
SUSPENDABLE = ('interrupted', 'delivery_done', 'waiting_for_human', 'reopened')
_CORE = ('repository', 'delivery_branch', 'base_sha', 'head', 'files', 'deleted', 'index')


def _fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _describe(repo, iso):
    """(manifest core, {sha256: bytes}) of the worktree as it is now; refuses what cannot be represented."""
    try:
        snap, tree = repo.snapshot()
    except RecoveryBlocked as exc:
        raise RecoveryBlocked('SUSPEND_BLOCKED', exc.reason, exc.paths) from None
    except ValueError as exc:  # check_repository: merge/rebase in progress, submodules, sparse index, ...
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'unsupported_git_state: '+str(exc)) from None
    files, blobs = {}, {}
    for name, fingerprint in snap['files'].items():
        data, mode = repo.content(name)
        if data is None or _sha(data) != fingerprint['sha256'] or mode != fingerprint['mode']:
            raise RecoveryBlocked('SUSPEND_BLOCKED', 'worktree_moved_during_suspend', [name])
        entry = dict(type='symlink' if mode == '120000' else 'file', mode=mode, permissions=fingerprint['permissions'],
                     sha256=fingerprint['sha256'], size=len(data), tracked=name in tree)
        base = tree.get(name)
        if base and base['mode'] == mode and repo.git('cat-file', 'blob', base['oid']) == data:
            entry['git_blob'] = base['oid']  # Unchanged: the bytes stay reachable from the kept branch.
        else:
            entry['blob'] = fingerprint['sha256']; blobs[fingerprint['sha256']] = data
        files[name] = entry
    head_entries = {name: (t['mode'], t['oid']) for name, t in tree.items()}
    staged = {}
    for raw in repo.git('ls-files', '--stage', '-z').split(b'\0'):
        if raw:
            meta, path = raw.split(b'\t', 1); mode, oid, _ = meta.decode().split()
            staged[os.fsdecode(path)] = (mode, oid)
    if staged == head_entries:
        index = dict(matches_head=True)
    else:
        # Staging is not meaningful to candidates, but an agent with a shell can stage: never flatten it.
        entries = []
        for name, (mode, oid) in sorted(staged.items()):
            if head_entries.get(name) == (mode, oid):
                entries.append(dict(path=name, mode=mode, git_blob=oid))
            else:
                data = repo.git('cat-file', 'blob', oid); blobs[_sha(data)] = data
                entries.append(dict(path=name, mode=mode, blob=_sha(data)))
        index = dict(matches_head=False, entries=entries)
    core = dict(repository=iso['repository'], delivery_branch=iso['delivery_branch'], base_sha=iso['base_sha'],
                head=snap['identity']['head'], files=files, deleted=sorted(set(tree)-set(files)), index=index)
    return core, blobs, snap


def _manifest_digest(path):
    return _sha((path/'manifest.json').read_bytes())


def verify_shelf(path, repo=None, terminal='SUSPEND_BLOCKED', manifest_sha256=None):
    """Integrity of a shelf: the manifest (and its recorded digest), every shelf blob, and, given the
    repository, every referenced Git blob exists and matches. Returns the manifest; never repairs."""
    path = Path(path)
    try:
        raw = (path/'manifest.json').read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        raise RecoveryBlocked(terminal, 'shelf_missing_or_corrupt', [str(path)]) from None
    if manifest_sha256 is not None and _sha(raw) != manifest_sha256:
        raise RecoveryBlocked(terminal, 'shelf_manifest_digest_mismatch', [str(path)])
    if type(manifest) is not dict or manifest.get('schema') != SHELF_SCHEMA:
        raise RecoveryBlocked(terminal, 'unsupported_shelf_schema')
    refs = [(name, e) for name, e in manifest['files'].items()]
    refs += [(e['path'], e) for e in manifest['index'].get('entries', ())]
    for name, entry in refs:
        if 'blob' in entry:
            digest = entry['blob']
            try:
                ok = len(digest) == 64 and _sha((path/'blobs'/digest).read_bytes()) == digest
            except OSError:
                ok = False
            if not ok:
                raise RecoveryBlocked(terminal, 'shelf_blob_corrupt', [name])
        elif repo is not None:
            code, data = repo.probe('cat-file', 'blob', entry['git_blob'])
            if code:
                raise RecoveryBlocked(terminal, 'shelf_git_blob_missing', [name])
            if 'sha256' in entry and _sha(data) != entry['sha256']:
                raise RecoveryBlocked(terminal, 'shelf_git_blob_mismatch', [name])
    return manifest


def _write_blob(directory, digest, data):
    with (directory/digest).open('xb') as out:
        out.write(data); out.flush(); os.fsync(out.fileno())


def _publish(root, shelf_id, work_id, core, blobs, repo, snap):
    """Build in a private temporary directory, verify, then atomically name it `shelf_id`."""
    parent = Path(root)/'suspend'; parent.mkdir(exist_ok=True)
    building = parent/f'.{shelf_id}.{uuid.uuid4().hex}'
    (building/'blobs').mkdir(parents=True)
    for digest, data in sorted(blobs.items()):
        _write_blob(building/'blobs', digest, data)
    manifest = dict(schema=SHELF_SCHEMA, shelf=shelf_id, work_id=work_id, **core)
    with (building/'manifest.json').open('xb') as out:
        out.write(_json(manifest).encode('utf-8')); out.flush(); os.fsync(out.fileno())
    _fsync(building/'blobs'); _fsync(building)
    verify_shelf(building, repo)
    if repo.snapshot()[0] != snap:
        shutil.rmtree(building)
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'worktree_moved_during_suspend')
    os.rename(building, parent/shelf_id)
    _fsync(parent)


def _eligible(root, store, op):
    phase = op['phase']
    if phase == 'running' or op.get('provider_pid') is not None:
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'delivery_running')  # No asynchronous cancellation.
    if phase not in SUSPENDABLE:
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'work_not_suspendable: '+phase)
    if store.versions('qa_reference'):
        qa = WorkWorkspace.open(_read(store, 'qa_reference')['workspace'])
        if qa.versions('qa_decision') and _read(qa, 'qa_decision')['terminal'] == 'ACCEPTED':
            raise RecoveryBlocked('SUSPEND_BLOCKED', 'work_not_suspendable: accepted')
        if qa.versions('qa_intent') and not qa.versions('qa_decision'):
            raise RecoveryBlocked('SUSPEND_BLOCKED', 'qa_decision_in_progress')


def _remove_worktree(iso):
    """Git owns worktree removal; the delivery branch is kept. Run from the shared Git dir, never the source."""
    _Repo(iso['repository']).git('worktree', 'remove', '--force', iso['delivery_worktree'])


def _cleanup(root, store, iso, op):
    """SUSPENDED is durable; remove the worktree only while it still equals the shelf. Idempotent."""
    if op.get('worktree_removed'):
        return recovery_status(root)
    path = Path(iso['delivery_worktree'])
    listed = str(path) in _worktrees(_Repo(iso['repository']))
    if listed:
        repo = _check_isolation(iso, 'SUSPEND_BLOCKED')
        core, _, _ = _describe(repo, iso)
        shelf = verify_shelf(Path(root)/'suspend'/op['shelf']['id'])
        if core != {k: shelf[k] for k in _CORE}:
            raise RecoveryBlocked('SUSPEND_BLOCKED', 'workspace_cleanup_pending: worktree_changed_since_shelf')
        try:
            _remove_worktree(iso)
        except RecoveryBlocked:
            raise RecoveryBlocked('SUSPEND_BLOCKED', 'workspace_cleanup_pending: worktree_remove_failed') from None
    elif path.exists() or path.is_symlink():
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'workspace_cleanup_pending: unregistered_path_at_delivery_worktree', [str(path)])
    fields = {k: v for k, v in op.items() if k not in ('phase', 'timestamp')}
    _operation(store, 'suspended', **{**fields, 'worktree_removed': True})
    return recovery_status(root)


def suspend_v2_work(work_root, reason=''):
    """Human SUSPEND of an isolated, non-running, non-final work; retry converges on one shelf."""
    reason = '' if reason is None else reason
    if type(reason) is not str or len(reason.encode('utf-8')) > 4096:
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'reason_too_large')
    root = Path(work_root).resolve()
    with _lock(root):
        store = _store(root); base = _read(store, 'baseline'); op = _read(store, 'operation')
        iso = base.get('isolation')
        if iso is None:
            raise RecoveryBlocked('SUSPEND_BLOCKED', 'legacy_work_not_isolated')
        if op['phase'] == 'suspended':
            return _cleanup(root, store, iso, op)  # Already durable: only finish pending cleanup.
        if op['phase'] == 'suspending':
            intent = op
        else:
            _eligible(root, store, op)
            # Refuse before the intent: a worktree that is not ours or cannot be represented.
            _describe(_check_isolation(iso, 'SUSPEND_BLOCKED'), iso)
            count = len({o['shelf']['id'] for o in _operations(store) if o['phase'] == 'suspended'})
            intent = _operation(store, 'suspending', shelf_id=f'shelf-{count+1}', reason=reason, previous_operation=op)
        repo = _check_isolation(iso, 'SUSPEND_BLOCKED')
        shelf_id = intent['shelf_id']; final = root/'suspend'/shelf_id
        for stale in (root/'suspend').glob(f'.{shelf_id}.*'):
            shutil.rmtree(stale)  # Unpublished, Switchyard-owned build directory of a crashed attempt.
        core, blobs, snap = _describe(repo, iso)
        if final.exists():
            shelf = verify_shelf(final, repo)  # Published before a crash: reuse only if it still is this worktree.
            if core != {k: shelf[k] for k in _CORE}:
                raise RecoveryBlocked('SUSPEND_BLOCKED', 'worktree_changed_since_shelf')
        else:
            _publish(root, shelf_id, root.name, core, blobs, repo, snap)
        op = _operation(store, 'suspended', terminal='SUSPENDED', reason=intent['reason'],
                        previous_operation=intent['previous_operation'], worktree_removed=False,
                        shelf=dict(id=shelf_id, path='suspend/'+shelf_id, manifest_sha256=_manifest_digest(final)))
        return _cleanup(root, store, iso, op)


# --- Restore (#4 slice 2): SUSPENDED -> RESTORING -> the suspended operation, same work ------------------
# Durable order: shelf + lineage verified (no writes) -> operation `restoring` (shelf, the suspended
# operation) -> worktree registered at the recorded path on the delivery branch (admin name reconciled)
# -> restore-owned rebuild: Git resets the worktree to the shelf HEAD, then every shelf entry, deletion and
# the index are applied -> the restored worktree is re-described and must equal the shelf exactly ->
# the suspended operation is re-instated (with `restored_shelf`). Until then status is RESTORING.
RESTORE = 'RESUME_BLOCKED'


def _admin_dir(repo):
    return os.fsdecode(repo.git('rev-parse', '--absolute-git-dir').strip())


def _add_worktree(iso):
    """Git owns registration; hooks of the user's repository never run for Switchyard's checkout."""
    _Repo(iso['repository']).git('-c', 'core.hooksPath='+os.devnull, 'worktree', 'add', iso['delivery_worktree'],
                                 iso['delivery_branch'][len('refs/heads/'):])


def _reconcile_admin_name(iso, recorded):
    """Git names a worktree's admin dir after its basename (+N if taken). Recorded identities (checkpoint,
    candidates) name the ORIGINAL admin dir, so a recreated worktree takes that name back if it is free."""
    path = Path(iso['delivery_worktree']); current = _admin_dir(_Repo(path))
    if current == recorded:
        return  # The identity string recorded by the baseline/checkpoints/candidates.
    current, recorded = Path(current).resolve(), Path(recorded).resolve()
    if current == recorded:
        return
    if recorded.exists() or recorded.parent != current.parent:
        raise RecoveryBlocked(RESTORE, 'delivery_restore_worktree_conflict: recorded worktree name in use', [str(recorded)])
    os.rename(current, recorded)
    with (path/'.git').open('w') as out:  # Both links of a worktree: <worktree>/.git and <admin>/gitdir.
        out.write('gitdir: '+str(recorded)+'\n'); out.flush(); os.fsync(out.fileno())
    if Path(_admin_dir(_Repo(path))).resolve() != recorded:
        raise RecoveryBlocked(RESTORE, 'delivery_restore_worktree_conflict: admin rename failed', [str(recorded)])


def _write_entry(repo, shelf, name, entry):
    data = (shelf/'blobs'/entry['blob']).read_bytes() if 'blob' in entry else repo.git('cat-file', 'blob', entry['git_blob'])
    if _sha(data) != entry['sha256']:
        raise RecoveryBlocked(RESTORE, 'shelf_blob_corrupt', [name])
    path = repo.path(name)  # Repository-relative, no symlinked parents.
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise RecoveryBlocked(RESTORE, 'restore_path_type_conflict', [name])
    path.parent.mkdir(parents=True, exist_ok=True)
    if entry['type'] == 'symlink':
        os.symlink(os.fsdecode(data), path)
        try:
            os.chmod(path, entry['permissions'], follow_symlinks=False)
        except (NotImplementedError, OSError):
            pass  # Platforms without lchmod: link permissions are not settable (and not meaningful).
    else:
        with path.open('xb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        path.chmod(entry['permissions'])


def _rebuild(repo, shelf, manifest):
    """Restore-owned worktree only: Git state of the shelf HEAD, then the shelf applied over it."""
    lock = Path(str(repo.index_path())+'.lock'); lock.unlink(missing_ok=True)  # Left by a dead restore.
    repo.git('-c', 'core.hooksPath='+os.devnull, 'read-tree', '--reset', '-u', manifest['head'])
    repo.git('clean', '-ffdxq')
    for name in manifest['deleted']:
        path = repo.path(name)
        if path.is_symlink() or path.is_file():
            path.unlink()
    for name, entry in sorted(manifest['files'].items()):
        _write_entry(repo, shelf, name, entry)
    index = manifest['index']
    if not index['matches_head']:
        lines = []
        for entry in index['entries']:
            if 'git_blob' in entry:
                oid = entry['git_blob']
            else:
                oid = repo.git('hash-object', '-w', '--no-filters', '--stdin',
                               input=(shelf/'blobs'/entry['blob']).read_bytes()).decode().strip()
            lines.append(f"{entry['mode']} {oid}\t{entry['path']}\0")
        repo.git('read-tree', '--empty')
        repo.git('update-index', '-z', '--index-info', input=''.join(lines).encode('utf-8', 'surrogateescape'))
    repo.probe('update-index', '-q', '--refresh')  # Stat cache only.


def _restored_equals(repo, iso, manifest):
    try:
        core, _, _ = _describe(repo, iso)
    except RecoveryBlocked:
        return False
    return core == {k: manifest[k] for k in _CORE}


def _finalize(store, intent):
    """Re-instate the suspended operation: the work is active again exactly where it was suspended."""
    previous = intent['previous_operation']
    fields = {k: v for k, v in previous.items() if k not in ('phase', 'timestamp')}
    return _operation(store, previous['phase'], **{**fields, 'restored_shelf': intent['shelf']['id']})


def restore_suspended(root, store, base, op):
    """Called by `resume` under the work lock for a `suspended` or `restoring` work."""
    iso = base['isolation']
    shelf_ref = op['shelf']; shelf = Path(root)/shelf_ref['path']
    common = _Repo(iso['repository'])
    manifest = verify_shelf(shelf, common, RESTORE, shelf_ref['manifest_sha256'])
    if any(manifest.get(k) != iso[k] for k in ('repository', 'delivery_branch', 'base_sha')) or manifest.get('work_id') != Path(root).name:
        raise RecoveryBlocked(RESTORE, 'shelf_provenance_mismatch')
    head = _branch_head(common, iso['delivery_branch'])
    if head is None:
        raise RecoveryBlocked(RESTORE, 'delivery_branch_missing', [iso['delivery_branch']])
    if head != manifest['head']:
        raise RecoveryBlocked(RESTORE, 'delivery_branch_moved', [iso['delivery_branch']])  # Never reset/rebase.
    if common.probe('merge-base', '--is-ancestor', iso['base_sha'], head)[0] != 0:
        raise RecoveryBlocked(RESTORE, 'delivery_base_mismatch', [iso['base_sha']])
    path = Path(iso['delivery_worktree'])
    listed = _worktrees(common)
    owner = listed.get(str(path), False)
    if owner is False and iso['delivery_branch'] in listed.values():
        raise RecoveryBlocked(RESTORE, 'delivery_restore_worktree_conflict: branch checked out elsewhere', [iso['delivery_branch']])
    if owner is not False and owner != iso['delivery_branch']:
        raise RecoveryBlocked(RESTORE, 'delivery_restore_worktree_conflict', [str(path)])
    if owner is False and (path.exists() or path.is_symlink()):
        raise RecoveryBlocked(RESTORE, 'delivery_restore_path_conflict', [str(path)])
    if op['phase'] == 'suspended':
        if owner is not False:
            # Cleanup never finished: the suspended worktree itself. Adopt it only if it still IS the shelf.
            repo = _check_isolation(iso, RESTORE)
            if not _restored_equals(repo, iso, manifest):
                raise RecoveryBlocked(RESTORE, 'worktree_changed_since_shelf')
            return _finalize(store, op)
        op = _operation(store, 'restoring', shelf=shelf_ref, reason=op.get('reason'),
                        previous_operation=op['previous_operation'], suspended_at=op['timestamp'])
    if owner is False:
        _add_worktree(iso)
    _reconcile_admin_name(iso, base['snapshot']['identity']['git_dir'])
    repo = _check_isolation(iso, RESTORE)
    if not _restored_equals(repo, iso, manifest):  # Fresh, partial or stale: rebuild from HEAD + shelf.
        _rebuild(repo, shelf, manifest)
        if not _restored_equals(repo, iso, manifest):
            raise RecoveryBlocked(RESTORE, 'restore_verification_failed')
    return _finalize(store, op)

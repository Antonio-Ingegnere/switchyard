"""DELIVERY-SUSPEND (#4): shelve an isolated delivery worktree durably, record SUSPENDED, then remove it.

The shelf is Switchyard-owned data under `<work_root>/suspend/<shelf-id>/`: `manifest.json` describes
the COMPLETE product state of the delivery worktree (the same Git-visible files the recovery snapshot
and candidates use) relative to the delivery branch HEAD; `blobs/<sha256>` holds every byte not already
in that commit. No stash, no commits. Restoring is a later slice.

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
                       _Repo, _worktrees, _sha, _json, recovery_status)
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


def verify_shelf(path, repo=None):
    """Integrity of a shelf: every referenced byte exists and matches its SHA-256. Returns the manifest."""
    path = Path(path)
    manifest = json.loads((path/'manifest.json').read_bytes())
    if manifest.get('schema') != SHELF_SCHEMA:
        raise RecoveryBlocked('SUSPEND_BLOCKED', 'unsupported_shelf_schema')
    refs = [(name, e) for name, e in manifest['files'].items()]
    refs += [(e['path'], e) for e in manifest['index'].get('entries', ())]
    for name, entry in refs:
        if 'blob' in entry:
            digest = entry['blob']
            if len(digest) != 64 or _sha((path/'blobs'/digest).read_bytes()) != digest:
                raise RecoveryBlocked('SUSPEND_BLOCKED', 'shelf_blob_corrupt', [name])
        elif repo is not None:
            data = repo.git('cat-file', 'blob', entry['git_blob'])
            if 'sha256' in entry and _sha(data) != entry['sha256']:
                raise RecoveryBlocked('SUSPEND_BLOCKED', 'shelf_git_blob_mismatch', [name])
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

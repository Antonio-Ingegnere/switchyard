"""CONTINUITY (#5) slice 1: adopt an old work's durable product into a NEW work on the same base.

The old work (W1) is only read. The new work (W2) is an ordinary isolated delivery under the current
configuration (own work id, branch, worktree, budgets and history) whose worktree starts from the same
base commit plus W1's adoptable implementation, and whose run starts at the deterministic verifier.

Implementation source, by W1's durable state (exactly one applies):
  1. ACCEPTED         -> `accepted_commit:<sha>`  the tree diff base..C, bytes from Git.
  2. SUSPENDED        -> `shelf:<id>`             the verified shelf (staging is not carried over).
  3. READY_FOR_MANUAL_QA with an undecided current candidate and no open QA round
                      -> `candidate:<sequence>`   W1's worktree, proven equal to the candidate first.
Anything else is refused: `implementation_uncertain` (interrupted, reopened, waiting, suspend/restore in
progress, a QA decision in progress) or `no_reusable_implementation`.

Durable order: W1 read and every byte verified (no writes) -> source preflight (same base, clean) ->
W2 recovery store: request, adoption blobs, operation `provisioning` (isolation + adoption plan) ->
sources copied -> branch at base -> worktree -> rebuilt from base + adoption blobs and verified ->
baseline (with `adoption`) -> reused artifacts -> `running` -> run from the verifier.
"""
from dataclasses import replace
import os
from pathlib import Path
import shutil
import uuid

from .recovery import (RecoveryBlocked, CATALOG, QA_READY, _lock, _store, _read, _operation, _Repo, _blob, _bytes,
                       _sha, _source_isolation, _provision_preflight, _provision, _baseline, _drift, _drive,
                       _open_round, _Projection)
from .qa_lifecycle import revalidate_candidate, LifecycleError, _load as _load_candidate
from .simple_config import simple_config_from_text
from .source_resolution import SOURCE_CATALOG
from .suspend import verify_shelf
from .work_contract import WorkWorkspace
from .workflow_compiler import compile_workflow

ADOPTION_SCHEMA = 1
B = 'ADOPTION_BLOCKED'


def _git_blob(repo, oid, name):
    code, data = repo.probe('cat-file', 'blob', oid)
    if code:
        raise RecoveryBlocked(B, 'source_git_blob_missing', [name])
    return data


def _tree(repo, commit):
    """{path: (mode, oid)} of a commit; submodules are not adoptable."""
    tree = {}
    for raw in repo.git('ls-tree', '-r', '-z', commit).split(b'\0'):
        if raw:
            meta, path = raw.split(b'\t', 1); mode, kind, oid = meta.decode().split()
            if kind != 'blob':
                raise RecoveryBlocked(B, 'unsupported_source_entry', [os.fsdecode(path)])
            tree[os.fsdecode(path)] = (mode, oid)
    return tree


def _from_commit(repo, base, commit):
    if repo.probe('rev-list', '--parents', '-n', '1', commit)[1].decode().split() != [commit, base]:
        raise RecoveryBlocked(B, 'implementation_uncertain: accepted commit is not a single commit on the base')
    before, after = _tree(repo, base), _tree(repo, commit)
    changes, data = {}, {}
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        if name not in after:
            changes[name] = dict(kind='deleted'); continue
        mode, oid = after[name]; content = _git_blob(repo, oid, name)
        changes[name] = dict(kind='symlink' if mode == '120000' else 'file', mode=mode, sha256=_sha(content)); data[name] = content
    return changes, data


def _from_shelf(repo, root, op, base):
    shelf = Path(root)/op['shelf']['path']
    manifest = verify_shelf(shelf, repo, B, op['shelf']['manifest_sha256'])
    if manifest['head'] != base:
        raise RecoveryBlocked(B, 'implementation_uncertain: shelf is not on the base')
    tree = _tree(repo, base); changes, data = {}, {}
    for name, entry in sorted(manifest['files'].items()):
        if tree.get(name) == (entry['mode'], entry.get('git_blob')):
            continue  # Unchanged from the base.
        content = (shelf/'blobs'/entry['blob']).read_bytes() if 'blob' in entry else _git_blob(repo, entry['git_blob'], name)
        if _sha(content) != entry['sha256']:
            raise RecoveryBlocked(B, 'shelf_blob_corrupt', [name])
        changes[name] = dict(kind=entry['type'], mode=entry['mode'], sha256=entry['sha256'], permissions=entry['permissions'])
        data[name] = content
    for name in manifest['deleted']:
        changes[name] = dict(kind='deleted')
    return changes, data


def _from_candidate(root, store, iso):
    ref = _read(store, 'qa_reference')
    try:
        qa, candidate, identity = _load_candidate(ref['workspace'])
    except (LifecycleError, OSError):
        raise RecoveryBlocked(B, 'no_reusable_implementation: candidate unavailable') from None
    if identity != ref['identity'] or qa.versions('qa_intent') or qa.versions('qa_decision'):
        raise RecoveryBlocked(B, 'implementation_uncertain: candidate decided or being decided')
    if _open_round(WorkWorkspace.open(root)) is not None:
        raise RecoveryBlocked(B, 'implementation_uncertain: QA round open')
    if (candidate['target'], candidate['branch'], candidate['base_head']) != (iso['delivery_worktree'], iso['delivery_branch'], iso['base_sha']):
        raise RecoveryBlocked(B, 'implementation_uncertain: candidate not on the delivery base')
    try:
        revalidate_candidate(iso['delivery_worktree'], ref['workspace'])  # Read-only proof: W1 worktree == candidate.
    except LifecycleError as exc:
        raise RecoveryBlocked(B, 'implementation_uncertain: worktree differs from candidate: '+str(exc)) from None
    changes, data = {}, {}
    for name, fingerprint in sorted(candidate['files'].items()):
        if fingerprint['kind'] == 'deleted':
            changes[name] = dict(kind='deleted'); continue
        content = (Path(iso['delivery_worktree'])/name).read_bytes()
        if _sha(content) != fingerprint['sha256']:
            raise RecoveryBlocked(B, 'implementation_uncertain: candidate drift', [name])
        changes[name] = dict(kind='file', mode=fingerprint['mode'], sha256=fingerprint['sha256']); data[name] = content
    return changes, data, candidate.get('sequence', 1), identity


def _implementation(old, store, base, op):
    """(source label, source identity, changes, bytes) by the precedence in the module docstring."""
    iso = base['isolation']; repo = _Repo(iso['repository']); phase = op['phase']
    qa = None
    if store.versions('qa_reference'):
        qa = WorkWorkspace.open(_read(store, 'qa_reference')['workspace'])
    decision = _read(qa, 'qa_decision') if qa is not None and qa.versions('qa_decision') else None
    if phase == 'accepted' or (decision and decision['terminal'] == 'ACCEPTED' and decision.get('committed_head')):
        commit = op['accepted_commit_sha'] if phase == 'accepted' else decision['committed_head']
        changes, data = _from_commit(repo, iso['base_sha'], commit)
        return 'accepted_commit:'+commit, commit, changes, data
    if phase == 'suspended':
        changes, data = _from_shelf(repo, old, op, iso['base_sha'])
        return 'shelf:'+op['shelf']['id'].split('-')[-1], op['shelf']['manifest_sha256'], changes, data
    if phase == 'delivery_done' and op.get('terminal') == QA_READY and qa is not None:
        changes, data, sequence, identity = _from_candidate(old, store, iso)
        return 'candidate:%d' % sequence, identity, changes, data
    if phase in ('interrupted', 'reopened', 'running', 'waiting_for_human', 'suspending', 'restoring') or \
            (qa is not None and qa.versions('qa_intent') and decision is None):
        raise RecoveryBlocked(B, 'implementation_uncertain: '+phase)
    raise RecoveryBlocked(B, 'no_reusable_implementation')


def _entry_state(project, flow):
    """The deterministic verifier: the one command state that routes to READY_FOR_MANUAL_QA."""
    spec = project.bundle.flow(flow)
    compiled = compile_workflow(project.bundle, flow, lambda b: _Projection())
    entries = [name for name, state in spec.states.items() if QA_READY in state.on.values()
               and getattr(compiled.plan[compiled.entry_states[name]].binding, 'provider', None) == 'command']
    if len(entries) != 1:
        raise RecoveryBlocked(B, 'no_adoption_entry: need exactly one command verifier routing to '+QA_READY)
    return entries[0]


def _upstream(spec, entry):
    """Artifacts the entry state needs, followed back through their producing states (request excluded)."""
    producers = {a: name for name, s in spec.states.items() for a in s.produces}
    needed, todo = [], list(spec.states[entry].consumes)
    while todo:
        artifact = todo.pop(0)
        if artifact == 'request' or artifact in needed:
            continue
        needed.append(artifact)
        if artifact in producers:
            todo.extend(spec.states[producers[artifact]].consumes)
    return needed


def _plan(old, project, flow):
    """Read-only: everything W2 needs from W1, every byte verified."""
    old = Path(old).resolve()
    try:
        store = _store(old); base = _read(store, 'baseline'); op = _read(store, 'operation')
    except (RecoveryBlocked, ValueError, OSError):
        raise RecoveryBlocked(B, 'unsupported_source_schema: no recovery baseline') from None
    if base.get('lifecycle_contract_version') != 2:
        raise RecoveryBlocked(B, 'unsupported_source_schema: lifecycle contract %r' % base.get('lifecycle_contract_version'))
    if 'isolation' not in base:
        raise RecoveryBlocked(B, 'unsupported_source_schema: legacy in-place work')
    flow = flow or base['flow']
    source, identity, changes, data = _implementation(old, store, base, op)
    work = WorkWorkspace.open(old)
    spec = project.bundle.flow(flow); entry = _entry_state(project, flow)
    artifacts = {}
    for name in _upstream(spec, entry):
        record = work.latest_artifact(name) if name in work.artifacts else None
        if record is None:
            raise RecoveryBlocked(B, 'no_reusable_implementation: missing artifact', [name])
        if work.artifacts[name] != project.bundle.artifacts.get(name):
            raise RecoveryBlocked(B, 'unsupported_source_schema: artifact contract changed', [name])
        artifacts[name] = (record, work.read_artifact(name, record.version))
    sources = old/'sources'
    if (sources/'manifest.jsonl').is_file():
        WorkWorkspace.open(sources, SOURCE_CATALOG).read_artifact('resolved_sources')  # Verified, never re-fetched.
    return dict(old=old, base=base, flow=flow, entry=entry, request=work.read_artifact('request'), source=source,
                identity=identity, changes=changes, data=data, artifacts=artifacts, sources=(sources/'manifest.jsonl').is_file(),
                baseline_sha256=store.latest_artifact('baseline').sha256)


def _record(plan, store):
    """Durable adoption plan: every byte in W2's own content-addressed blobs (W1 is never needed again)."""
    for content in plan['data'].values():
        _blob(store, content)
    artifacts = {}
    for name, (record, content) in plan['artifacts'].items():
        artifacts[name] = dict(version=record.version, sha256=record.sha256, blob=_blob(store, content.encode('utf-8')))
    project_spec = plan['base']['isolation']
    return dict(schema=ADOPTION_SCHEMA, from_work_id=plan['old'].name, from_work_root=str(plan['old']),
                from_baseline_sha256=plan['baseline_sha256'], from_base_sha=project_spec['base_sha'],
                from_repository=project_spec['repository'], from_flow=plan['base']['flow'],
                from_lifecycle_contract_version=plan['base']['lifecycle_contract_version'],
                implementation_source=plan['source'], implementation_identity=plan['identity'], changes=plan['changes'],
                reused_artifacts=artifacts, reused_sources=plan['sources'], entry_state=plan['entry'],
                reused_specification=any(name == 'microspec' for name in artifacts), lifecycle_contract_version=2)


def _expected(repo, base, changes):
    expected = {name: (mode, _sha(_git_blob(repo, oid, name))) for name, (mode, oid) in _tree(repo, base).items()}
    for name, change in changes.items():
        if change['kind'] == 'deleted':
            expected.pop(name, None)
        else:
            expected[name] = (change['mode'], change['sha256'])
    return expected


def _materialized(repo, base, changes):
    """Worktree (bytes, modes, symlinks, permissions where recorded) == base + changes, index == base."""
    try:
        snap, tree = repo.snapshot()
    except (RecoveryBlocked, ValueError):
        return False
    if {name: (f['mode'], f['sha256']) for name, f in snap['files'].items()} != _expected(repo, base, changes):
        return False
    if any('permissions' in c and snap['files'][n]['permissions'] != c['permissions'] for n, c in changes.items() if c['kind'] != 'deleted'):
        return False
    staged = {}
    for raw in repo.git('ls-files', '--stage', '-z').split(b'\0'):
        if raw:
            meta, path = raw.split(b'\t', 1); mode, oid, _ = meta.decode().split(); staged[os.fsdecode(path)] = (mode, oid)
    return staged == {name: (t['mode'], t['oid']) for name, t in tree.items()}


def _write_change(repo, store, name, change):
    path = repo.path(name)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise RecoveryBlocked(B, 'adoption_path_type_conflict', [name])
    if change['kind'] == 'deleted':
        return
    content = _bytes(store, change['sha256'])
    path.parent.mkdir(parents=True, exist_ok=True)
    if change['kind'] == 'symlink':
        os.symlink(os.fsdecode(content), path)
        return
    with path.open('xb') as out:
        out.write(content); out.flush(); os.fsync(out.fileno())
    path.chmod(change.get('permissions', 0o755 if change['mode'] == '100755' else 0o644))


def _materialize(repo, store, base, changes):
    """W2's own worktree before its baseline: always rebuilt from base + adoption blobs, then proven."""
    if _materialized(repo, base, changes):
        return
    Path(str(repo.index_path())+'.lock').unlink(missing_ok=True)  # Left by a dead adoption.
    repo.git('-c', 'core.hooksPath='+os.devnull, 'read-tree', '--reset', '-u', base)
    repo.git('clean', '-ffdxq')
    for name, change in sorted(changes.items(), key=lambda item: item[1]['kind'] != 'deleted'):
        _write_change(repo, store, name, change)
    repo.probe('update-index', '-q', '--refresh')
    if not _materialized(repo, base, changes):
        raise RecoveryBlocked(B, 'adoption_verification_failed')


def _copy_sources(old, new):
    target = new/'sources'
    if not (target/'manifest.jsonl').is_file():
        building = new/f'.sources.{uuid.uuid4().hex}'
        shutil.copytree(old/'sources', building, symlinks=True)
        os.rename(building, target)
    WorkWorkspace.open(target, SOURCE_CATALOG).read_artifact('resolved_sources')


def adopt_v2_work(old_work_root, new_work_root, config_text, provider_runtime_config, *, flow=None):
    """New isolated work W2 reusing W1's request, specification chain and implementation on the same base.
    `provider_runtime_config.working_directory` is the clean source checkout W2 is based on."""
    new = Path(new_work_root).resolve(); old = Path(old_work_root).resolve()
    if new == old or new.is_relative_to(old) or old.is_relative_to(new):
        raise RecoveryBlocked(B, 'new_work_root_overlaps_old_work')
    project = simple_config_from_text(config_text)
    with _lock(new):
        if (new/'events.jsonl').exists():
            raise RecoveryBlocked('RECOVERY_BLOCKED', 'workspace_not_fresh')
        if (new/'recovery/manifest.jsonl').exists():
            store = WorkWorkspace.open(new/'recovery', CATALOG)
            op = _read(store, 'operation') if store.versions('operation') else {}
            if op.get('phase') != 'provisioning' or 'adoption' not in op:
                raise RecoveryBlocked('RECOVERY_BLOCKED', 'workspace_not_fresh')
            iso, adoption = op['isolation'], op['adoption']
            if adoption['from_work_root'] != str(old):
                raise RecoveryBlocked(B, 'adoption_in_progress_from_another_work')
            request = store.read_artifact('request')
        else:
            if (new/'manifest.jsonl').exists():
                raise RecoveryBlocked('RECOVERY_BLOCKED', 'workspace_not_fresh')
            with _lock(old):  # Excludes W1's own controllers while it is read; W1 is never written.
                plan = _plan(old, project, flow)
            iso = _source_isolation(provider_runtime_config.working_directory, new)
            if iso['base_sha'] != plan['base']['isolation']['base_sha']:
                raise RecoveryBlocked(B, 'base_mismatch', [plan['base']['isolation']['base_sha'], iso['base_sha']])
            if iso['repository'] != plan['base']['isolation']['repository']:
                raise RecoveryBlocked(B, 'repository_mismatch')
            _provision_preflight(iso)
            store = WorkWorkspace.open(new/'recovery', CATALOG)
            request = plan['request']; store.initialize_request(request)
            adoption = _record(plan, store)
            _operation(store, 'provisioning', isolation=iso, adoption=adoption)
        flow_name = flow or adoption['from_flow']
        if adoption['reused_sources']:
            _copy_sources(old, new)
        repo = _provision(iso, pristine=False)
        if store.versions('baseline'):
            baseline = _read(store, 'baseline')
            if baseline.get('adoption') != adoption or baseline['flow'] != flow_name:
                raise RecoveryBlocked('RECOVERY_BLOCKED', 'adoption_start_differs')
            _drift(baseline['snapshot'], repo.snapshot()[0], B)
        else:
            _materialize(repo, store, iso['base_sha'], adoption['changes'])
            _baseline(repo, store, config_text, flow_name, iso, extra=dict(adoption=adoption))
        workspace = WorkWorkspace.open(new, project.bundle.artifacts)
        workspace.initialize_request(request)
        for name, reused in adoption['reused_artifacts'].items():
            content = _bytes(store, reused['blob']).decode('utf-8')
            existing = workspace.latest_artifact(name)
            if existing is None:
                workspace.write_artifact(name, content)  # Reused product: no producer invocation in W2.
            elif workspace.read_artifact(name) != content:
                raise RecoveryBlocked(B, 'reused_artifact_differs', [name])
        _operation(store, 'running', adopted_from=adoption['from_work_id'])
        config = replace(provider_runtime_config, working_directory=iso['delivery_worktree'])
        return _drive(project, flow_name, request, new, config, store, repo, start=adoption['entry_state'])

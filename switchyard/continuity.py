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

`adopt_v2_work` and the explicit `transplant_v2_work` (#8: A -> B, A != B, `TRANSPLANT_BLOCKED`) are two
intents over this one path (`_derive`); the intent is recorded in W2's adoption record.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import uuid

from contextlib import contextmanager
import fcntl

from .recovery import (RecoveryBlocked, CATALOG, QA_READY, _lock, _store, _read, _operation, _Repo, _blob, _bytes,
                       _sha, _source_isolation, _provision_preflight, _provision, _baseline, _drift, _drive,
                       _open_round, _Projection)
from .qa_lifecycle import revalidate_candidate, LifecycleError, _load as _load_candidate
from .simple_config import simple_config_from_text
from .source_resolution import SOURCE_CATALOG
from .suspend import verify_shelf
from .versioning import CURRENT_WORK_SCHEMA, SchemaError, runtime_version, work_schema
from .work_contract import WorkWorkspace
from .workflow_compiler import compile_workflow

ADOPTION_SCHEMA = 1
B = 'ADOPTION_BLOCKED'
# Derivation intents (#8). Both are one operation over one engine: `adopt` reuses W1's durable product on
# whatever base the target checkout has (same base, or a transplant when it descends from W1's base);
# `transplant` is the explicit intent to move it from base A to a different base B, and refuses A == B.
ADOPT, TRANSPLANT = 'adopt', 'transplant'
TRANSPLANT_BLOCKED = 'TRANSPLANT_BLOCKED'


@contextmanager
def _source_lock(old):
    """Exclude W1's own controllers while it is read, without creating anything in W1: a controller always
    creates `.recovery.lock` before it writes, so a work without one has no active controller."""
    path = Path(old)/'.recovery.lock'
    if not path.is_file():
        yield; return
    with path.open('rb') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryBlocked(B, 'source_work_active') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


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
    return _diff_trees(repo, base, commit)


def _diff_trees(repo, before_tree, after_tree):
    """Canonical change set (kind, mode, sha256 + bytes) turning one tree into another."""
    before, after = _tree(repo, before_tree), _tree(repo, after_tree)
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


# --- Historical readers (#5 slice 4): one per supported work schema --------------------------------------
# Each turns a W1 of that schema into the canonical inputs everything below consumes: repository and base
# provenance plus one implementation (label, identity, change set, bytes). Planning, transplant and W2
# provisioning never look at a schema version. W1 is only read; nothing is migrated in place.

def _isolated_provenance(base):
    return base['isolation']['repository'], base['isolation']['base_sha']


def _in_place_provenance(base):
    """Schema 2 (release 2.3.0 / `isolation=False`): the caller's checkout was the workspace."""
    identity = base['snapshot']['identity']
    return _Repo(identity['git_dir']).common_dir(), identity['head']


def _in_place_implementation(old, store, base, op, repository, base_sha):
    """Only an accepted commit is durable product here: a candidate's bytes live in the caller's own
    (mutable) checkout, which this runtime will not treat as authoritative."""
    qa = WorkWorkspace.open(_read(store, 'qa_reference')['workspace']) if store.versions('qa_reference') else None
    decision = _read(qa, 'qa_decision') if qa is not None and qa.versions('qa_decision') else None
    if decision and decision['terminal'] == 'ACCEPTED' and decision.get('committed_head'):
        changes, data = _from_commit(_Repo(repository), base_sha, decision['committed_head'])
        return 'accepted_commit:'+decision['committed_head'], decision['committed_head'], changes, data
    if qa is not None and decision is None:
        raise RecoveryBlocked(B, 'implementation_uncertain: work schema 2 candidate bytes live only in the caller checkout')
    if op['phase'] in ('interrupted', 'reopened', 'running', 'waiting_for_human'):
        raise RecoveryBlocked(B, 'implementation_uncertain: '+op['phase'])
    raise RecoveryBlocked(B, 'no_reusable_implementation')


def _implementation(old, store, base, op, repository=None, base_sha=None):
    """Schema 3 (isolated): (label, identity, changes, bytes) by the precedence in the module docstring."""
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


# Supported historical schemas: work schema -> (reader identity, provenance, implementation source).
READERS = {2: ('work-schema-2-in-place', _in_place_provenance, _in_place_implementation),
           3: ('work-schema-3-isolated', _isolated_provenance, _implementation)}


def _entry_state(project, flow):
    """The deterministic verifier: the one command state that routes to READY_FOR_MANUAL_QA."""
    spec = project.bundle.flow(flow)
    compiled = compile_workflow(project.bundle, flow, lambda b: _Projection())
    entries = [name for name, state in spec.states.items() if QA_READY in state.on.values()
               and getattr(compiled.plan[compiled.entry_states[name]].binding, 'provider', None) == 'command']
    if len(entries) != 1:
        raise RecoveryBlocked(B, 'no_adoption_entry: need exactly one command verifier routing to '+QA_READY)
    return entries[0]


# --- Continuity decisions (#5 slice 2) ----------------------------------------------------------------
# For every product W2 reasons about, one decision: REUSED (W2 consumes W1's durable product; the producing
# stage is NOT executed and NOT recorded in W2's event log), RERUN (W2 executes it under the current
# runtime) or INVALIDATED (W1 has the product but a deterministic rule refuses it). Inputs are durable only:
# W1's artifact records and contracts, the current configuration's contracts, the same base. The plan is
# written once, before W2 runs, and is never updated by execution outcomes.
CONTINUITY_SCHEMA = 1
REUSED, RERUN, INVALIDATED = 'REUSED', 'RERUN', 'INVALIDATED'


def _contract(spec):
    return None if spec is None else dict(format=spec.format, schema=spec.schema, source=spec.source)


def _chain(spec, entry):
    """[(producing state, needed artifacts)] the entry depends on, request side first. Explicit rules only:
    each needed artifact's producer, then that producer's own inputs; `request` is the root."""
    producers = {a: name for name, state in spec.states.items() for a in state.produces}
    order, needed, todo = [], {}, list(spec.states[entry].consumes)
    while todo:
        artifact = todo.pop(0)
        if artifact == 'request' or any(artifact in v for v in needed.values()):
            continue
        state = producers.get(artifact)
        if state is None:
            raise RecoveryBlocked(B, 'no_adoption_entry: artifact without producer', [artifact])
        needed.setdefault(state, []).append(artifact)
        if state not in order:
            order.append(state); todo.extend(spec.states[state].consumes)
    return [(state, tuple(needed[state])) for state in reversed(order)]


def _identity(work, name):
    record = work.latest_artifact(name) if name in work.artifacts else None
    return None if record is None else dict(artifact=name, version=record.version, sha256=record.sha256)


def implementation_digest(changes):
    """Stable identity of an adopted implementation: the canonical change set (paths, kinds, modes, bytes),
    independent of the source form (permission bits, recorded only by shelves, are not part of it)."""
    canonical = {name: {k: c[k] for k in ('kind', 'mode', 'sha256') if k in c} for name, c in changes.items()}
    return _sha(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode('utf-8'))


def _decisions(work, project, spec, entry, sources, implementation):
    """Rules: REQUEST is reused exactly; frozen SOURCE_RESOLUTION is reused; each producing stage on the
    entry's chain is REUSED when its latest artifacts exist with an unchanged contract, otherwise
    INVALIDATED (`artifact_contract_changed`), and every later stage is INVALIDATED (`upstream_invalidated`);
    the stage producing the entry's input carries the implementation; the entry verifier is always RERUN:
    W1's verification is historical evidence, not current truth."""
    decisions = [dict(stage='REQUEST', product='request', action=REUSED, reason='exact_request',
                      artifacts=[_identity(work, 'request')])]
    if sources is not None:
        decisions.append(dict(stage='SOURCE_RESOLUTION', product='resolved_sources', action=REUSED,
                              reason='frozen_sources', artifacts=[sources]))
    chain = _chain(spec, entry)
    implementer = next(state for state, artifacts in chain if set(artifacts) & set(spec.states[entry].consumes))
    invalid = None
    for state, artifacts in chain:
        entries, reason = [], None
        for name in artifacts:
            identity = _identity(work, name)
            if identity is None:
                raise RecoveryBlocked(B, 'no_reusable_implementation: missing artifact', [name])
            identity['contract'] = _contract(work.artifacts[name])
            if work.artifacts[name] != project.bundle.artifacts.get(name):
                identity['current_contract'] = _contract(project.bundle.artifacts.get(name))
                reason = reason or 'artifact_contract_changed'
            entries.append(identity)
        if reason is None and invalid is not None:
            reason = 'upstream_invalidated: '+invalid
        decision = dict(stage=state, product='+'.join(artifacts), action=INVALIDATED if reason else REUSED,
                        reason=reason or 'durable_product_available', artifacts=entries)
        if state == implementer:
            decision.update(product='implementation', implementation=implementation)
        invalid = invalid or (state if reason else None)
        decisions.append(decision)
    historical = [i for i in (_identity(work, name) for name in spec.states[entry].produces) if i]
    decisions.append(dict(stage=entry, product='+'.join(spec.states[entry].produces) or 'verification', action=RERUN,
                          reason='current_runtime_verification_required', artifacts=[], historical=historical))
    return decisions, invalid


def _plan(old, project, flow):
    """Read-only: everything W2 needs from W1, every byte verified, and the continuity decisions."""
    old = Path(old).resolve()
    try:
        store = _store(old); base = _read(store, 'baseline'); op = _read(store, 'operation')
    except (RecoveryBlocked, ValueError, OSError):
        raise RecoveryBlocked(B, 'unsupported_source_schema: no recovery baseline') from None
    try:
        schema = work_schema(base)
    except SchemaError as exc:
        raise RecoveryBlocked(B, exc.reason) from None
    if schema not in READERS:
        raise RecoveryBlocked(B, 'unsupported_source_schema: work schema %d (lifecycle contract 1 artifacts/configuration)' % schema)
    reader, provenance, implementation_reader = READERS[schema]
    repository, source_base = provenance(base)
    flow = flow or base['flow']
    source, identity, changes, data = implementation_reader(old, store, base, op, repository, source_base)
    work = WorkWorkspace.open(old)
    spec = project.bundle.flow(flow); entry = _entry_state(project, flow)
    sources = None
    if (old/'sources/manifest.jsonl').is_file():
        frozen = WorkWorkspace.open(old/'sources', SOURCE_CATALOG)
        frozen.read_artifact('resolved_sources')  # Verified, never re-fetched.
        record = frozen.latest_artifact('resolved_sources')
        sources = dict(artifact='resolved_sources', version=record.version, sha256=record.sha256)
    implementation = dict(source=source, identity=identity, digest=implementation_digest(changes), paths=len(changes))
    decisions, invalid = _decisions(work, project, spec, entry, sources, implementation)
    continuity = dict(schema=CONTINUITY_SCHEMA, source_work_id=old.name, source_baseline_sha256=store.latest_artifact('baseline').sha256,
                      base_sha=source_base, flow=flow, decisions=decisions)
    if invalid is not None:
        # The current workflow cannot regenerate an implementation without rerunning the stages adoption
        # exists to avoid, so this slice blocks, and the plan explains exactly what was invalidated.
        error = RecoveryBlocked(B, 'continuity_invalidated: '+invalid,
                                [a['artifact'] for d in decisions if d['action'] == INVALIDATED for a in d['artifacts'] if 'current_contract' in a])
        error.continuity_plan = continuity
        raise error
    artifacts = {name: (work.latest_artifact(name), work.read_artifact(name)) for d in decisions
                 if d['stage'] not in ('REQUEST', 'SOURCE_RESOLUTION') and d['action'] == REUSED for name in (a['artifact'] for a in d['artifacts'])}
    product = dict(work_schema_version=schema, reader=reader, baseline_sha256=continuity['source_baseline_sha256'],
                   repository_base=source_base, artifacts=[{k: a[k] for k in ('artifact', 'version', 'sha256')}
                                                            for d in decisions for a in d['artifacts']],
                   implementation=implementation)
    origin = dict(work_id=old.name, work_schema_version=schema, runtime_version=base.get('runtime_version'), reader=reader,
                  baseline_sha256=continuity['source_baseline_sha256'],
                  product_digest=_sha(json.dumps(product, sort_keys=True, separators=(',', ':')).encode('utf-8')))
    return dict(old=old, base=base, flow=flow, entry=entry, request=work.read_artifact('request'), source=source,
                repository=repository, source_base=source_base, origin=origin,
                identity=identity, changes=changes, data=data, artifacts=artifacts, sources=sources is not None,
                baseline_sha256=continuity['source_baseline_sha256'], continuity=continuity)


def _record(plan, store, intent):
    """Durable adoption plan: every byte in W2's own content-addressed blobs (W1 is never needed again)."""
    for content in plan['data'].values():
        _blob(store, content)
    artifacts = {}
    for name, (record, content) in plan['artifacts'].items():
        artifacts[name] = dict(version=record.version, sha256=record.sha256, blob=_blob(store, content.encode('utf-8')))
    return dict(schema=ADOPTION_SCHEMA, intent=intent, from_work_id=plan['old'].name, from_work_root=str(plan['old']),
                from_baseline_sha256=plan['baseline_sha256'], from_base_sha=plan['source_base'],
                from_repository=plan['repository'], from_flow=plan['base']['flow'],
                from_lifecycle_contract_version=plan['base']['lifecycle_contract_version'],
                source=plan['origin'], target=dict(work_schema_version=CURRENT_WORK_SCHEMA, runtime_version=runtime_version()),
                implementation_source=plan['source'], implementation_identity=plan['identity'], changes=plan['changes'],
                reused_artifacts=artifacts, reused_sources=plan['sources'], entry_state=plan['entry'],
                reused_specification=any(a.get('schema') == 'microspec' for d in plan['continuity']['decisions']
                                         if d['action'] == REUSED for a in (x.get('contract') or {} for x in d['artifacts'])),
                lifecycle_contract_version=2, continuity=plan['continuity'],
                **({'transplant': plan['transplant']} if plan.get('transplant') else {}))


def _derivation(work_root):
    store = _store(Path(work_root).resolve())
    return _read(store, 'baseline').get('adoption') if store.versions('baseline') else _read(store, 'operation').get('adoption')


def continuity_plan(work_root):
    """Read-only: W2's durable continuity decisions (None for work that was not adopted)."""
    record = _derivation(work_root)
    return None if record is None else record.get('continuity')


def derivation_chain(work_root, limit=64):
    """Read-only: [work, its immediate source, that source's source, ...]. Each derived work records only its
    immediate source (`from_work_root`); the walk stops at a work that was not derived or is unreadable."""
    chain = [Path(work_root).resolve()]
    while len(chain) <= limit:
        try:
            record = _derivation(chain[-1])
        except (RecoveryBlocked, ValueError, OSError):
            break
        if record is None or Path(record['from_work_root']) in chain:
            break
        chain.append(Path(record['from_work_root']))
    return chain


# --- Changed-base transplant (#5 slice 3) --------------------------------------------------------------
# W1's implementation is a change set relative to its base A. To adopt it onto a target base B that
# descends from A, Git's own 3-way merge (`merge-tree --write-tree`, ort) computes B + (A -> I) from the three
# trees alone: base A, ours B, theirs I (a commit of A + the change set with a fixed identity). No worktree,
# no model, no heuristics; any conflict blocks. The clean result becomes a change set relative to B.
TRANSPLANT_STRATEGY = 'git_merge_tree_three_way'
_FIXED = {'GIT_AUTHOR_NAME': 'Switchyard', 'GIT_AUTHOR_EMAIL': 'switchyard@invalid', 'GIT_AUTHOR_DATE': '1970-01-01T00:00:00Z',
          'GIT_COMMITTER_NAME': 'Switchyard', 'GIT_COMMITTER_EMAIL': 'switchyard@invalid', 'GIT_COMMITTER_DATE': '1970-01-01T00:00:00Z'}
_KIND_ORDER = ('binary', 'modify/delete', 'delete/modify', 'add/add', 'file/directory', 'distinct types', 'content')


def _plumb(repo, *args, input=None, env=None, terminal=B, failure='transplant_failed'):
    try:
        result = subprocess.run([repo.git_executable, '-c', 'core.filemode=true', '--literal-pathspecs', *args],
                                cwd=repo.workspace, env={**repo.env, **(env or {})}, input=input, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise RecoveryBlocked(terminal, failure+': git unavailable') from None
    return result.returncode, result.stdout


def _checked(repo, *args, **kwargs):
    code, out = _plumb(repo, *args, **kwargs)
    if code:
        raise RecoveryBlocked(B, 'transplant_failed: '+args[0])
    return out.decode().strip()


def _implementation_commit(repo, base, changes, data):
    """(tree, commit) of A + the change set; deterministic: same bytes -> same objects."""
    with TemporaryDirectory(prefix='switchyard-transplant-') as tmp:
        env = {'GIT_INDEX_FILE': str(Path(tmp)/'index')}
        _checked(repo, 'read-tree', base, env=env)
        lines = []
        for name, change in sorted(changes.items()):
            if change['kind'] == 'deleted':
                lines.append('0 %s\t%s\0' % ('0'*len(base), name))
            else:
                oid = _checked(repo, 'hash-object', '-w', '--no-filters', '--stdin', input=data[name])
                lines.append('%s %s\t%s\0' % (change['mode'], oid, name))
        _checked(repo, 'update-index', '-z', '--index-info', input=''.join(lines).encode('utf-8', 'surrogateescape'), env=env)
        tree = _checked(repo, 'write-tree', env=env)
    commit = _checked(repo, 'commit-tree', tree, '-p', base, '-m', 'switchyard: adopted implementation', env=_FIXED)
    return tree, commit


def _conflict_kind(token, message, target, implementation):
    kind = token.lower()
    if kind == 'contents':
        kind = 'add/add' if message.startswith('CONFLICT (add/add)') else 'content'
    if kind == 'modify/delete':  # Named from the implementation's side: it modified, the target deleted.
        deleted = re.search(r' deleted in ([0-9a-f]+) ', message)
        kind = 'modify/delete' if deleted and deleted.group(1) == target else 'delete/modify'
    return kind


def _conflicts(out, target, implementation):
    """Normalized [{path, kind}] from `merge-tree -z --name-only` informational messages."""
    parts = out.split(b'\0'); i = 1
    names = []
    while i < len(parts) and parts[i]:
        names.append(os.fsdecode(parts[i])); i += 1
    i += 1
    kinds = {}
    while i < len(parts) and parts[i]:
        count = int(parts[i]); paths = [os.fsdecode(p) for p in parts[i+1:i+1+count]]
        token, message = parts[i+1+count].decode(), parts[i+2+count].decode(errors='replace'); i += 3+count
        match = re.fullmatch(r'CONFLICT \(([^)]+)\)', token)
        if match:
            for path in paths:
                kinds.setdefault(path, set()).add(_conflict_kind(match.group(1), message, target, implementation))
    for name in names:
        kinds.setdefault(name, {'content'})
    rank = lambda k: _KIND_ORDER.index(k) if k in _KIND_ORDER else len(_KIND_ORDER)
    return [dict(path=path, kind=min(found, key=lambda k: (rank(k), k))) for path, found in sorted(kinds.items())]


def three_way(repo, base, ours, theirs, *, terminal=B, failure='transplant_failed'):
    """The one merge engine: Git's own 3-way merge (`merge-tree --write-tree`, ort) of base -> theirs onto ours
    from the three commits alone -- no worktree, index or ref. (result_tree, []) when clean, else
    (None, normalized [{path, kind}]). Transplant (#5/#8) and integration readiness (#9) both use it."""
    code, out = _plumb(repo, 'merge-tree', '--write-tree', '-z', '--name-only', '--merge-base='+base, ours, theirs,
                       terminal=terminal, failure=failure)
    if code not in (0, 1):
        raise RecoveryBlocked(terminal, failure+': merge-tree')
    if code == 1:
        return None, _conflicts(out, ours, theirs)
    return out.split(b'\0')[0].decode(), []


def _transplant(plan, iso):
    """Same base: nothing to do. Target descends from the base: 3-way transplant or an explicit block."""
    a, b = plan['source_base'], iso['base_sha']
    if iso['repository'] != plan['repository']:
        raise RecoveryBlocked(B, 'repository_mismatch')
    if a == b:
        return plan
    repo = _Repo(iso['repository'])
    if repo.probe('merge-base', '--is-ancestor', a, b)[0] != 0:
        raise RecoveryBlocked(B, 'incompatible_base_lineage', [a, b])
    source_tree, commit = _implementation_commit(repo, a, plan['changes'], plan['data'])
    result_tree, conflicts = three_way(repo, a, b, commit)
    implement = next(d for d in plan['continuity']['decisions'] if d.get('product') == 'implementation')
    transplant = dict(strategy=TRANSPLANT_STRATEGY, from_base=a, to_base=b, source_tree=source_tree)
    continuity = json.loads(json.dumps(plan['continuity'])); continuity['target_base_sha'] = b
    decision = next(d for d in continuity['decisions'] if d.get('product') == 'implementation')
    if conflicts:
        transplant['conflicts'] = conflicts
        decision.update(action=INVALIDATED, reason='transplant_conflict', transplant=transplant)
        error = RecoveryBlocked(B, 'transplant_conflict', [c['path'] for c in transplant['conflicts']])
        # Read-only structured conflict result (source work, bases, implementation identity, conflicts).
        error.transplant = dict(transplant, source_work_id=plan['old'].name, implementation=implement['implementation'])
        error.continuity_plan = continuity
        raise error  # Before anything of W2 exists: no partial or conflicted W2.
    changes, data = _diff_trees(repo, b, result_tree)
    transplant.update(result_tree=result_tree, result_digest=implementation_digest(changes), result_paths=len(changes))
    decision.update(reason='deterministic_three_way_transplant', transplant=transplant)
    return dict(plan, changes=changes, data=data, continuity=continuity, transplant=transplant)


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
    """New isolated work W2 reusing W1's request, specification chain and implementation. W2's base is the
    clean source checkout's HEAD (`provider_runtime_config.working_directory`): W1's base, or a descendant
    of it onto which the implementation is transplanted deterministically."""
    return _derive(old_work_root, new_work_root, config_text, provider_runtime_config, flow, ADOPT)


def transplant_v2_work(old_work_root, new_work_root, config_text, provider_runtime_config, *, flow=None):
    """Explicit TRANSPLANT (#8): new isolated work W2 at the target checkout's HEAD B carrying W1's preserved
    implementation moved from W1's base A (A an ancestor of B, A != B) by the same deterministic 3-way engine
    as changed-base adoption. W1 is only read (a suspended W1 stays suspended); no provider is invoked for
    the transplant. Refusals are `TRANSPLANT_BLOCKED` with the engine's reasons and structured diagnostics."""
    try:
        return _derive(old_work_root, new_work_root, config_text, provider_runtime_config, flow, TRANSPLANT)
    except RecoveryBlocked as exc:
        if exc.terminal != B:
            raise
        error = RecoveryBlocked(TRANSPLANT_BLOCKED, exc.reason, exc.paths)
        for name in ('transplant', 'continuity_plan'):
            if hasattr(exc, name):
                setattr(error, name, getattr(exc, name))
        raise error from None


def default_source(old_work_root, terminal=B):
    """Read-only: the source checkout W1 was started from, from its frozen baseline alone (isolated work: the
    recorded `source_worktree`; schema-2 in-place work: its recorded target). Never reads live status, which
    may reconcile (e.g. mark a dead RUNNING work INTERRUPTED) and so write into W1."""
    try:
        base = _read(_store(Path(old_work_root).resolve()), 'baseline')
        return base['isolation']['source_worktree'] if 'isolation' in base else base['snapshot']['identity']['target']
    except (RecoveryBlocked, ValueError, OSError, KeyError, TypeError):
        raise RecoveryBlocked(terminal, 'unsupported_source_schema: no recovery baseline') from None


def _derive(old_work_root, new_work_root, config_text, provider_runtime_config, flow, intent):
    new = Path(new_work_root).resolve(); old = Path(old_work_root).resolve()
    if new == old or new.is_relative_to(old) or old.is_relative_to(new):
        raise RecoveryBlocked(B, 'new_work_root_overlaps_old_work')
    if new in derivation_chain(old):
        raise RecoveryBlocked(B, 'derivation_cycle')  # W2 would name itself as an ancestor.
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
            if adoption.get('intent', ADOPT) != intent:
                raise RecoveryBlocked(B, 'derivation_in_progress: retry the same '+adoption.get('intent', ADOPT))
            with _source_lock(old):  # Same durable inputs (incl. the RECORDED target base) -> same decisions.
                expected = _transplant(_plan(old, project, flow), iso)
            if expected['origin']['product_digest'] != adoption.get('source', {}).get('product_digest'):
                raise RecoveryBlocked(B, 'source_product_changed')  # W1 no longer canonicalizes to the same product.
            if adoption.get('continuity') != expected['continuity'] or adoption['changes'] != expected['changes']:
                raise RecoveryBlocked(B, 'continuity_plan_differs: retry with the original configuration')
            request = store.read_artifact('request')
        else:
            if (new/'manifest.jsonl').exists():
                raise RecoveryBlocked('RECOVERY_BLOCKED', 'workspace_not_fresh')
            with _source_lock(old):  # Excludes W1's own controllers while it is read; W1 is never written.
                plan = _plan(old, project, flow)
            iso = _source_isolation(provider_runtime_config.working_directory, new)
            plan = _transplant(plan, iso)  # After the continuity rules; refuses before any W2 write.
            if intent == TRANSPLANT and 'transplant' not in plan:
                raise RecoveryBlocked(B, 'transplant_not_required: same_base (use work adopt to reuse it on this base, '
                                         'or work resume)', [plan['source_base']])
            _provision_preflight(iso)
            store = WorkWorkspace.open(new/'recovery', CATALOG)
            request = plan['request']; store.initialize_request(request)
            adoption = _record(plan, store, intent)
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

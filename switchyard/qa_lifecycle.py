"""Human-selected V2 QA operations; no providers, runtime routing, or push."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory

from runtime import RunResult
from .git_safety import GitRepository, ScopeBlocked
from .work_contract import WorkWorkspace, WorkContractError
from .workflow_bundle import ArtifactSpec

CATALOG = {
    'request': ArtifactSpec('text', source='request'),
    'qa_candidate': ArtifactSpec('json'),
    'qa_intent': ArtifactSpec('json'),
    'qa_decision': ArtifactSpec('json'),
    'qa_feedback': ArtifactSpec('text'),
}


class LifecycleError(ValueError):
    """Invalid call, finalized candidate, or unresolved durable operation; no retry."""


class LifecycleRecoveryRequired(LifecycleError):
    """Git publication may be complete but audit publication failed; never retry."""
    def __init__(self, committed_head):
        self.committed_head = committed_head
        super().__init__('decision persistence failed; inspect durable intent and HEAD before recovery')


class _Blocked(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class QaCandidate:
    workspace: str
    identity: str  # SHA-256 of immutable qa_candidate v1.


@dataclass(frozen=True)
class QaDecision:
    candidate_identity: str
    decision: str
    terminal: str
    reason: str | None = None
    committed_head: str | None = None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n'


def _require(ok, reason):
    if not ok:
        raise _Blocked(reason)


@contextmanager
def _exclusive(path):
    # Separate from WorkWorkspace's per-publication lock; spans an entire operation.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise LifecycleError('operation active or stale lock; inspect before recovery') from None
    try:
        yield fd
    finally:
        owned = os.fstat(fd)
        os.close(fd)
        if path.exists() and path.stat().st_ino == owned.st_ino and path.stat().st_dev == owned.st_dev:
            path.unlink()


class _Git(GitRepository):
    def __init__(self, target, executable=None):
        executable = executable or shutil.which('git')
        if not executable:
            raise _Blocked('git_unavailable')
        super().__init__(str(Path(target).absolute()), str(Path(executable).absolute()))
        # Do not inherit alternate repos/indexes, pathspec flags, or implicit identities.
        self.environment = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        self.environment['GIT_OPTIONAL_LOCKS'] = '0'

    def git(self, *args, input=None, index=None):
        env = dict(self.environment)
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
        try:
            result = subprocess.run([self.git_executable, '-c', 'core.filemode=true',
                                     '-c', 'user.useConfigOnly=true', *args], cwd=self.workspace,
                                    env=env, input=input, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise ScopeBlocked('Git inspection or update failed') from None
        if result.returncode:
            raise ScopeBlocked('Git inspection or update failed')
        return result.stdout

    def path(self, name):
        value = Path(os.fsdecode(self.git('rev-parse', '--git-path', name).strip()))
        return (value if value.is_absolute() else self.workspace/value).resolve()

    def identity(self):
        try:
            branch, head = self.check_repository()
        except ScopeBlocked:
            raise _Blocked('git_conflict_state') from None
        return {'target':str(self.workspace), 'git_dir':str(self.path('.')),
                'branch':branch, 'base_head':head}

    def clean_index(self):
        _require(not self.git('diff', '--cached', '--name-only', '-z', 'HEAD', '--'),
                 'unexpected_staged_paths')
        # diff --cached can omit intent-to-add entries. Compare the complete index.
        entries = {}
        for entry in self.git('ls-files', '--stage', '-z').split(b'\0'):
            if entry:
                metadata, path = entry.split(b'\t', 1)
                mode, blob, stage = metadata.split()
                _require(stage == b'0', 'unexpected_staged_paths')
                entries[path] = (mode, blob)
        base = {}
        for entry in self.git('ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
            if entry:
                metadata, path = entry.split(b'\t', 1)
                mode, _, blob = metadata.split()
                base[path] = (mode, blob)
        _require(entries == base, 'unexpected_staged_paths')

    def paths(self):
        raw = self.git('diff', '--no-renames', '--name-only', '-z', 'HEAD', '--')
        raw += self.git('ls-files', '--others', '--exclude-standard', '-z')
        try:
            paths = sorted(set(p.decode('utf-8') for p in raw.split(b'\0') if p))
        except UnicodeError:
            raise _Blocked('unsupported_path') from None
        for path in paths:
            _require(not path.startswith('/') and all(p not in ('', '.', '..') for p in path.split('/')),
                     'unsupported_path')
            _require(path == '.switchyard/config.yaml' or not any(p.lower() in ('.git', '.switchyard') for p in path.split('/')), 'unsupported_path')
        return paths

    def fingerprint(self, relative):
        path = self.workspace/relative
        for parent in (path, *path.parents):
            if parent == self.workspace:
                break
            _require(not parent.is_symlink(), 'unsupported_path')
        if not path.exists():
            return {'kind':'deleted'}
        info = path.stat()
        _require(stat.S_ISREG(info.st_mode), 'unsupported_path')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        return {'kind':'file', 'sha256':digest.hexdigest(),
                'mode':'100755' if info.st_mode & stat.S_IXUSR else '100644'}

    def snapshot(self):
        identity = self.identity()
        self.clean_index()
        paths = self.paths()
        _require(bool(paths), 'empty_candidate')
        return {**identity, 'files':{p:self.fingerprint(p) for p in paths}}

    def revalidate(self, candidate):
        current = self.identity()
        _require(current['base_head'] == candidate['base_head'], 'base_head_changed')
        _require(all(current[k] == candidate[k] for k in ('target','git_dir','branch')), 'worktree_identity_changed')
        self.clean_index()
        paths = self.paths()
        _require(paths == sorted(candidate['files']), 'unexpected_worktree_paths')
        _require({p:self.fingerprint(p) for p in paths} == candidate['files'], 'candidate_drift')

    def staged_paths(self, head, tree):
        return sorted(p.decode('utf-8') for p in self.git('diff-tree', '--no-commit-id', '--no-renames',
                          '--name-only', '-r', '-z', head, tree, '--').split(b'\0') if p)

    def accept(self, candidate, before_publish):
        """Private index and immutable tree; real index is installed only after CAS."""
        self.revalidate(candidate)
        try:
            self.git('var', 'GIT_AUTHOR_IDENT'); self.git('var', 'GIT_COMMITTER_IDENT')
        except ScopeBlocked:
            raise _Blocked('git_identity_missing') from None
        index = self.path('index')
        try:
            with _exclusive(Path(str(index)+'.lock')) as lock_fd, TemporaryDirectory(prefix='switchyard-qa-index-') as tmp:
                self.revalidate(candidate)
                staged = Path(tmp)/'index'
                self.git('read-tree', candidate['base_head'], index=staged)
                for path, fingerprint in candidate['files'].items():
                    if fingerprint['kind'] == 'deleted':
                        line = '0 '+('0'*len(candidate['base_head']))+'\t'+path+'\0'
                    else:
                        data = (self.workspace/path).read_bytes()
                        _require(hashlib.sha256(data).hexdigest() == fingerprint['sha256'], 'candidate_drift')
                        blob = self.git('hash-object', '-w', '--no-filters', '--stdin', input=data).decode().strip()
                        line = fingerprint['mode']+' '+blob+'\t'+path+'\0'
                    self.git('update-index', '-z', '--index-info', input=line.encode(), index=staged)
                tree = self.git('write-tree', index=staged).decode().strip()
                _require(self.staged_paths(candidate['base_head'], tree) == sorted(candidate['files']),
                         'unexpected_staged_paths')
                # Check staged content/mode, not just paths, before any commit object.
                self.verify_tree(candidate, tree)
                self.revalidate(candidate)
                # The tree must also be clean against the worktree under Git's attributes.
                try:
                    self.git('diff', '--quiet', '--', index=staged)
                except ScopeBlocked:
                    raise _Blocked('candidate_drift') from None
                commit = self.commit_tree(tree, candidate['base_head'], candidate['commit_message'])
                _require(self.git('rev-parse', commit+'^').decode().strip() == candidate['base_head'], 'commit_failed')
                # Persist planned commit BEFORE publishing ref; crash/replay cannot double-commit.
                before_publish(commit)
                self.revalidate(candidate)
                # Prepare the new real index while leaving the original index untouched.
                with staged.open('rb') as source:
                    with os.fdopen(os.dup(lock_fd), 'wb') as destination:
                        shutil.copyfileobj(source, destination); destination.flush(); os.fsync(destination.fileno())
                try:
                    self.advance_branch(candidate['branch'], commit, candidate['base_head'])
                except ScopeBlocked:
                    # A subprocess error can be ambiguous about whether publication happened.
                    try:
                        published = self.git('rev-parse', 'HEAD').decode().strip() == commit
                    except ScopeBlocked:
                        raise LifecycleRecoveryRequired(commit) from None
                    if published:
                        return commit, 'post_commit_verification_failed'
                    raise
                # HEAD is now advanced. Any failure from here is explicitly non-rollbackable.
                try:
                    os.replace(str(index)+'.lock', index)
                    _require(self.git('rev-parse', 'HEAD').decode().strip() == commit, 'post_commit_verification_failed')
                    _require(self.git('rev-list', '--count', candidate['base_head']+'..HEAD').strip() == b'1',
                             'post_commit_verification_failed')
                    self.verify_tree(candidate, commit+'^{tree}')
                    _require(self.staged_paths(candidate['base_head'], commit) == sorted(candidate['files']),
                             'post_commit_verification_failed')
                    _require(not self.git('status', '--porcelain=v1', '--untracked-files=all'), 'post_commit_verification_failed')
                except (ScopeBlocked, _Blocked, OSError):
                    return commit, 'post_commit_verification_failed'
                return commit, None
        except LifecycleRecoveryRequired:
            raise
        except LifecycleError:
            raise _Blocked('unexpected_staged_paths') from None

    def verify_tree(self, candidate, tree):
        entries = {}
        for entry in self.git('ls-tree', '-r', '-z', tree).split(b'\0'):
            if entry:
                metadata, path = entry.split(b'\t', 1)
                mode, kind, blob = metadata.decode().split()
                entries[path.decode('utf-8')] = (mode, kind, blob)
        for path, fingerprint in candidate['files'].items():
            if fingerprint['kind'] == 'deleted':
                _require(path not in entries, 'candidate_drift')
            else:
                _require(path in entries, 'candidate_drift')
                mode, kind, blob = entries[path]
                _require(kind == 'blob' and mode == fingerprint['mode'], 'candidate_drift')
                _require(hashlib.sha256(self.git('cat-file', 'blob', blob)).hexdigest() == fingerprint['sha256'], 'candidate_drift')


def create_qa_candidate(target, source_workspace: WorkWorkspace, source_result: RunResult,
                        source_flow: str, work_root, *, git_executable=None) -> QaCandidate:
    """Capture after a QA-ready delivery. work_root is a fresh, external WorkWorkspace."""
    if (not isinstance(source_result, RunResult) or source_result.terminal_state != 'READY_FOR_MANUAL_QA'
            or type(source_flow) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', source_flow)):
        raise LifecycleError('source must be a READY_FOR_MANUAL_QA delivery')
    starts = [e for e in source_result.events if e.kind == 'run_started']
    ends = [e for e in source_result.events if e.kind == 'run_finished']
    # Earlier terminals are allowed only when an explicit recovery continued the same run.
    recoveries = [e for e in source_result.events if e.kind == 'run_resumed' and e.target is not None]
    if (len(starts) != 1 or len(ends) != 1 + len(recoveries) or source_result.events[-1] is not ends[-1]
            or any(e.run_id != starts[0].run_id for e in ends) or ends[-1].state != 'READY_FOR_MANUAL_QA'):
        raise LifecycleError('source run provenance is incomplete')
    root, target = Path(work_root).absolute(), Path(target).resolve()
    if root.exists() or root.resolve().is_relative_to(target):
        raise LifecycleError('candidate workspace must be fresh and outside target worktree')
    try:
        git = _Git(target, git_executable)
        snapshot = git.snapshot()
        request = source_workspace.read_artifact('request')
        candidate = {**snapshot, 'version':1, 'source_flow':source_flow,
                     'source_run_id':starts[0].run_id, 'correlation_id':starts[0].correlation_id,
                     'source_workspace':str(source_workspace.root.resolve()),
                     'source_request_sha256':source_workspace.latest_artifact('request').sha256,
                     'commit_message':f'{source_flow}: accept reviewed V2 work'}
        # A moving worktree must not yield a silently mixed candidate snapshot.
        _require(git.snapshot() == snapshot, 'candidate_drift')
    except (_Blocked, ScopeBlocked, OSError) as exc:
        raise LifecycleError(getattr(exc, 'reason', 'git_inspection_failed')) from None
    workspace = WorkWorkspace.open(root, CATALOG)
    workspace.initialize_request(request)
    saved = workspace.write_artifact('qa_candidate', _json(candidate))
    return QaCandidate(str(root), saved.sha256)


def _load(work_root):
    if not (Path(work_root)/'manifest.jsonl').is_file():
        raise LifecycleError('candidate workspace is unavailable')
    workspace = WorkWorkspace.open(work_root, CATALOG)
    versions = workspace.versions('qa_candidate')
    if len(versions) != 1:
        raise LifecycleError('expected one immutable QA candidate')
    candidate = json.loads(workspace.read_artifact('qa_candidate', 1))
    return workspace, candidate, versions[0].sha256


def _decision(target, work_root, decision, feedback=None, git_executable=None):
    if decision == 'reopen':
        try:
            valid = type(feedback) is str and bool(feedback.strip()) and len(feedback.encode('utf-8')) <= 65536
        except UnicodeError:
            valid = False
        if not valid:
            raise LifecycleError('reopen requires non-empty UTF-8 human feedback, at most 65536 bytes')
    workspace, candidate, identity = _load(work_root)
    with _exclusive(workspace.root/'.lifecycle.lock'):
        if workspace.versions('qa_decision'):
            raise LifecycleError('candidate already finalized')
        if workspace.versions('qa_intent') or workspace.versions('qa_feedback'):
            raise LifecycleError('unresolved lifecycle intent; manual inspection required')
        # Durable reservation precedes any Git mutation. Every decision is terminal, even blocked.
        intent = {'candidate_identity':identity, 'decision':decision, 'phase':'started'}
        workspace.write_artifact('qa_intent', _json(intent))
        commit = None
        prepared_commit = None
        try:
            _require(str(Path(target).resolve()) == candidate['target'], 'worktree_identity_changed')
            if decision == 'reopen':
                workspace.write_artifact('qa_feedback', feedback)
                terminal, reason = 'REOPENED', None
            else:
                git = _Git(target, git_executable)
                def publishing(commit):
                    nonlocal prepared_commit
                    workspace.write_artifact('qa_intent', _json({**intent, 'phase':'prepared', 'commit':commit}))
                    prepared_commit = commit
                commit, reason = git.accept(candidate, publishing)
                terminal = 'COMMIT_BLOCKED' if reason else 'ACCEPTED'
        except _Blocked as exc:
            terminal, reason = 'COMMIT_BLOCKED', exc.reason
        except (ScopeBlocked, OSError):
            terminal, reason = 'COMMIT_BLOCKED', 'commit_failed'
            if prepared_commit is not None:
                try:
                    published = git.git('rev-parse', 'HEAD').decode().strip() == prepared_commit
                except ScopeBlocked:
                    raise LifecycleRecoveryRequired(prepared_commit) from None
                if published:
                    commit, reason = prepared_commit, 'post_commit_verification_failed'
        result = QaDecision(identity, decision, terminal, reason, commit)
        record = {**asdict(result), 'source_run_id':candidate['source_run_id'],
                  'correlation_id':candidate['correlation_id'],
                  'feedback_artifact': 'qa_feedback' if terminal == 'REOPENED' else None}
        try:
            workspace.write_artifact('qa_decision', _json(record))
        except WorkContractError:
            raise LifecycleRecoveryRequired(commit) from None
        return result


def accept_v2_work(target, work_root, *, git_executable=None) -> QaDecision:
    """Explicit human ACCEPT. No model selects or invokes this operation."""
    return _decision(target, work_root, 'accept', git_executable=git_executable)


def reopen_v2_work(target, work_root, feedback: str) -> QaDecision:
    """Explicit human REOPEN; preserve target/index/HEAD and persist rework input."""
    return _decision(target, work_root, 'reopen', feedback)


def withdraw_qa_candidate(work_root, candidate_identity):
    """Permanently retire pre-accept QA for explicit delivery rollback; retain audit."""
    workspace, candidate, identity = _load(work_root)
    with _exclusive(workspace.root/'.lifecycle.lock'):
        if identity != candidate_identity:
            raise LifecycleError('candidate identity differs from rollback reference')
        if workspace.versions('qa_decision'):
            decision = json.loads(workspace.read_artifact('qa_decision'))
            if decision['terminal'] == 'ACCEPTED':
                raise LifecycleError('ACCEPTED history cannot be rolled back; use Git revert or corrective delivery')
            return  # Already permanently finalized (REOPENED/COMMIT_BLOCKED/ROLLED_BACK).
        if workspace.versions('qa_intent'):
            raise LifecycleError('unresolved QA intent; inspect before rollback')
        workspace.write_artifact('qa_intent', _json(dict(candidate_identity=identity, decision='rollback', phase='started')))
        workspace.write_artifact('qa_decision', _json(asdict(QaDecision(identity,'rollback','ROLLED_BACK'))))

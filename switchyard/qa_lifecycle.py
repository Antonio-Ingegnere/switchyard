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
import uuid

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

    def accept(self, candidate, before_publish, message=None):
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
                commit = self.commit_tree(tree, candidate['base_head'], message or candidate['commit_message'])
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

    def published_acceptance(self, candidate, commit, message):
        """Is `commit` provably the acceptance of `candidate`: the branch HEAD, one parent = base_head,
        exactly the candidate's paths/content/modes, and the expected message."""
        try:
            _require(self.git('rev-parse', '--verify', candidate['branch']).decode().strip() == commit, 'accept_reconciliation_ambiguous')
            parents = self.git('rev-list', '--parents', '-n', '1', commit).decode().split()
            _require(parents == [commit, candidate['base_head']], 'accept_reconciliation_ambiguous')
            raw = self.git('cat-file', 'commit', commit)
            _require(raw.split(b'\n\n', 1)[1] == (message or candidate['commit_message']).encode('utf-8'), 'accept_reconciliation_ambiguous')
            _require(self.staged_paths(candidate['base_head'], commit) == sorted(candidate['files']), 'accept_reconciliation_ambiguous')
            self.verify_tree(candidate, commit+'^{tree}')
        except (_Blocked, ScopeBlocked, IndexError):
            raise _Blocked('accept_reconciliation_ambiguous') from None

    def install_accepted_index(self, commit):
        """Finish a published acceptance interrupted before its index was installed; files are untouched."""
        self.git('read-tree', commit)
        self.git('update-index', '-q', '--refresh')
        _require(not self.git('status', '--porcelain=v1', '--untracked-files=all'), 'post_commit_verification_failed')

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
                        source_flow: str, work_root, *, git_executable=None,
                        sequence: int = 1, supersedes: str | None = None) -> QaCandidate:
    """Capture after a QA-ready delivery. work_root is a fresh, external WorkWorkspace.

    `sequence`/`supersedes` mark a corrective candidate of the same work; the first
    candidate keeps its original byte-for-byte shape and therefore its identity.
    """
    if type(sequence) is not int or sequence < 1 or (sequence > 1) != (supersedes is not None):
        raise LifecycleError('corrective candidates require a sequence and the superseded identity')
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
        if sequence > 1:
            # Identity must advance even if a corrective cycle reproduces identical bytes.
            candidate.update(sequence=sequence, supersedes=supersedes)
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


def _decision(target, work_root, decision, feedback=None, git_executable=None, message=None, resume=False):
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
        intent = {'candidate_identity':identity, 'decision':decision, 'phase':'started'}
        if resume:
            # An interrupted ACCEPT: every recorded intent must be this candidate's accept.
            intents = [json.loads(workspace.read_artifact('qa_intent', r.version)) for r in workspace.versions('qa_intent')]
            if (decision != 'accept' or not intents or workspace.versions('qa_feedback')
                    or any({k:i.get(k) for k in intent if k != 'phase'} != {'candidate_identity':identity, 'decision':'accept'}
                           or i.get('phase') not in ('started', 'prepared') for i in intents)):
                raise LifecycleError('unresolved lifecycle intent; manual inspection required')
            pending = next((i['commit'] for i in reversed(intents) if i['phase'] == 'prepared'), None)
        else:
            if workspace.versions('qa_intent') or workspace.versions('qa_feedback'):
                raise LifecycleError('unresolved lifecycle intent; manual inspection required')
            # Durable reservation precedes any Git mutation. Every decision is terminal, even blocked.
            workspace.write_artifact('qa_intent', _json(intent))
            pending = None
        commit = None
        prepared_commit = None
        try:
            _require(str(Path(target).resolve()) == candidate['target'], 'worktree_identity_changed')
            if resume:
                git = _Git(target, git_executable)
                head = git.git('rev-parse', '--verify', candidate['branch']).decode().strip()
            if resume and head != candidate['base_head']:
                # Published before the crash: reuse it only if it provably is this acceptance.
                if pending is None or head != pending:
                    raise LifecycleError('accept_reconciliation_ambiguous: delivery branch moved; never reset')
                try:
                    git.published_acceptance(candidate, pending, message)
                except _Blocked:
                    raise LifecycleError('accept_reconciliation_ambiguous: branch head is not the pending acceptance') from None
                commit, prepared_commit = pending, pending
                try:
                    git.install_accepted_index(pending); reason = None
                except (_Blocked, ScopeBlocked, OSError):
                    reason = 'post_commit_verification_failed'
                terminal = 'COMMIT_BLOCKED' if reason else 'ACCEPTED'
            elif decision == 'reopen':
                workspace.write_artifact('qa_feedback', feedback)
                terminal, reason = 'REOPENED', None
            else:
                git = _Git(target, git_executable)
                def publishing(commit):
                    nonlocal prepared_commit
                    workspace.write_artifact('qa_intent', _json({**intent, 'phase':'prepared', 'commit':commit}))
                    prepared_commit = commit
                # Resumed with nothing published: any earlier commit object stays unreferenced.
                commit, reason = git.accept(candidate, publishing, message)
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


def accept_v2_work(target, work_root, *, git_executable=None, commit_message=None) -> QaDecision:
    """Explicit human ACCEPT. No model selects or invokes this operation."""
    return _decision(target, work_root, 'accept', git_executable=git_executable, message=commit_message)


def finish_accept_v2_work(target, work_root, *, git_executable=None, commit_message=None) -> QaDecision:
    """Converge an ACCEPT interrupted after its durable intent onto exactly one published commit:
    nothing published -> accept now; the prepared commit published -> reuse it; anything else on
    the branch -> refuse (never reset). The caller owns stale lock handling."""
    return _decision(target, work_root, 'accept', git_executable=git_executable, message=commit_message, resume=True)


def reopen_v2_work(target, work_root, feedback: str) -> QaDecision:
    """Explicit human REOPEN; preserve target/index/HEAD and persist rework input."""
    return _decision(target, work_root, 'reopen', feedback)


def finish_reopen_v2_work(target, work_root, feedback: str) -> QaDecision:
    """Idempotent REOPEN for a durably requested same-work QA fix.

    REOPEN never mutates Git, so a reopen interrupted after its intent (and maybe its
    feedback) was written is completed deterministically; an already REOPENED candidate
    with identical feedback is a no-op. Any other finalized/pending state is refused."""
    workspace, candidate, identity = _load(work_root)
    if not workspace.versions('qa_intent') and not workspace.versions('qa_decision'):
        return reopen_v2_work(target, work_root, feedback)
    with _exclusive(workspace.root/'.lifecycle.lock'):
        intents = workspace.versions('qa_intent')
        intent = json.loads(workspace.read_artifact('qa_intent')) if intents else None
        if (len(intents) != 1 or intent != {'candidate_identity':identity, 'decision':'reopen', 'phase':'started'}
                or str(Path(target).resolve()) != candidate['target']):
            raise LifecycleError('unresolved lifecycle intent; manual inspection required')
        if workspace.versions('qa_feedback') and workspace.read_artifact('qa_feedback') != feedback:
            raise LifecycleError('recorded REOPEN feedback differs; manual inspection required')
        result = QaDecision(identity, 'reopen', 'REOPENED')
        if workspace.versions('qa_decision'):
            if json.loads(workspace.read_artifact('qa_decision'))['terminal'] != 'REOPENED' or not workspace.versions('qa_feedback'):
                raise LifecycleError('candidate already finalized')
            return result
        if not workspace.versions('qa_feedback'):
            workspace.write_artifact('qa_feedback', feedback)
        workspace.write_artifact('qa_decision', _json({**asdict(result), 'source_run_id':candidate['source_run_id'],
                                 'correlation_id':candidate['correlation_id'], 'feedback_artifact':'qa_feedback'}))
        return result


def revalidate_candidate(target, work_root, *, git_executable=None):
    """Identity of an existing candidate that still exactly matches the worktree."""
    _, candidate, identity = _load(work_root)
    try:
        _require(str(Path(target).resolve()) == candidate['target'], 'worktree_identity_changed')
        _Git(target, git_executable).revalidate(candidate)
    except (_Blocked, ScopeBlocked, OSError) as exc:
        raise LifecycleError(getattr(exc, 'reason', 'git_inspection_failed')) from None
    return candidate, identity


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


# --- Same-work corrective QA feedback -------------------------------------------------
# One durable free-text record per rejected candidate, held beside the work (like human_input)
# so corrective stages read bounded context instead of a transcript. Routing stays in recovery.
# QA round N = qa_fix_request version N (rejection intent) + qa_fix_resolved version N (the
# candidate it produced). Both are append-only; a resolved round is never rewritten.
FIX_CATALOG = {'request': ArtifactSpec('text', source='request'),
               'qa_fix_request': ArtifactSpec('json'), 'qa_fix_resolved': ArtifactSpec('json')}
MAX_FEEDBACK_BYTES = 8192
MAX_LISTED_FILES = 100
MAX_FINDINGS = 20
FINDING_STATES = ('open', 'fixed', 'verified')


def validate_feedback(text):
    try:
        size = len(text.encode('utf-8')) if type(text) is str and text.strip() else 0
    except UnicodeError:
        size = 0
    if not size:
        raise LifecycleError('qa-fix requires non-empty UTF-8 feedback')
    if size > MAX_FEEDBACK_BYTES:
        raise LifecycleError(f'qa-fix feedback exceeds {MAX_FEEDBACK_BYTES} UTF-8 bytes')


def finding_descriptions(feedback=None, findings=None):
    """One rejection as an ordered tuple of finding texts; `qa-fix "text"` is one finding."""
    if (feedback is None) == (findings is None):
        raise LifecycleError('qa-fix requires feedback text or findings, not both')
    descriptions = (feedback,) if findings is None else tuple(findings)
    if not 1 <= len(descriptions) <= MAX_FINDINGS:
        raise LifecycleError(f'qa-fix requires 1 to {MAX_FINDINGS} findings')
    for text in descriptions:
        validate_feedback(text)
    validate_feedback(combined_feedback(descriptions, 99999))  # Conservative: any realistic round number.
    return descriptions


def combined_feedback(descriptions, round_number):
    """Human text kept on the rejected candidate (`qa_feedback`); a single finding is stored verbatim."""
    if len(descriptions) == 1:
        return descriptions[0]
    return '\n'.join(f'[qa-{round_number}-{i}] {text}' for i, text in enumerate(descriptions, 1))


# --- QA evidence (issue #7): durable attachments of structured findings -----------------
# Stored beside the work, never in the product worktree:
#   <work>/qa/evidence/<evidence-id>/{metadata.json,<safe filename>}
# published by atomic directory rename; bytes and metadata are immutable once published.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_FINDING = 10
MAX_ATTACHMENTS_PER_ROUND = 20
MAX_ROUND_ATTACHMENT_BYTES = 32 * 1024 * 1024
_EVIDENCE_ID = re.compile(r'ev-[0-9a-f]{32}')
# Deterministic, host-independent MIME detection (mimetypes depends on system databases).
_MIME = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.gif':'image/gif', '.webp':'image/webp',
         '.txt':'text/plain', '.log':'text/plain', '.md':'text/markdown', '.csv':'text/csv',
         '.json':'application/json', '.html':'text/html', '.pdf':'application/pdf', '.xml':'application/xml',
         '.js':'application/javascript', '.yaml':'application/yaml', '.yml':'application/yaml'}
# Delivery classes for corrective stages; anything else cannot be consumed and is refused, never dropped.
TEXT_EVIDENCE_TYPES = frozenset({'application/json', 'application/xml', 'application/javascript',
                                 'application/x-yaml', 'application/yaml'})
IMAGE_EVIDENCE_TYPES = frozenset({'image/png', 'image/jpeg', 'image/webp'})


def evidence_kind(mime_type, data):
    """('text'|'image', None) or (None, reason): classification by durable MIME metadata only."""
    if mime_type in IMAGE_EVIDENCE_TYPES:
        return 'image', None
    if mime_type.startswith('text/') or mime_type in TEXT_EVIDENCE_TYPES:
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            return None, 'text evidence is not valid UTF-8'
        return 'text', None
    return None, f'unsupported evidence type {mime_type}'
_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'), (b'GIF8', 'image/gif'), (b'%PDF-', 'application/pdf'))


def _safe_filename(source):
    """Display/storage name only; the destination directory is always chosen by Switchyard."""
    name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(os.fsdecode(source)))[-100:].lstrip('.')
    name = name or 'attachment'
    return '_'+name if name == 'metadata.json' else name


def _mime(filename, data):
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    return _MIME.get(os.path.splitext(filename)[1].lower(), 'application/octet-stream')


def read_attachment(source):
    """Snapshot one untrusted local file into memory; no writes. Later source changes are irrelevant."""
    try:
        with open(source, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise LifecycleError(f'attachment is not a regular file: {_safe_filename(source)}')
            if info.st_size > MAX_ATTACHMENT_BYTES:
                raise LifecycleError(f'attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {_safe_filename(source)}')
            data = stream.read(MAX_ATTACHMENT_BYTES + 1)
    except IsADirectoryError:
        raise LifecycleError(f'attachment is not a regular file: {_safe_filename(source)}') from None
    except OSError as exc:
        raise LifecycleError(f'attachment unreadable: {_safe_filename(source)} ({exc.strerror or "error"})') from None
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise LifecycleError(f'attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {_safe_filename(source)}')
    filename = _safe_filename(source)
    return dict(filename=filename, mime_type=_mime(filename, data), size=len(data),
                sha256=hashlib.sha256(data).hexdigest(), data=data)


def finding_inputs(feedback=None, findings=None, attachments=None):
    """Normalize one rejection to ((description, (attachment, ...)), ...), validating every file first.

    `findings` items are text or {'description': text, 'attachments': [path, ...]}. `attachments`
    is shorthand for the single-finding case and is refused when several findings would be ambiguous."""
    raw = None if findings is None else [f if isinstance(f, dict) else dict(description=f) for f in findings]
    descriptions = finding_descriptions(feedback, None if raw is None else [f.get('description') for f in raw])
    per_finding = [list(f.get('attachments') or ()) for f in raw] if raw is not None else [[]]
    if attachments:
        if len(descriptions) != 1:
            raise LifecycleError('attachments are ambiguous with several findings; attach per finding instead')
        per_finding[0] += list(attachments)
    if any(len(paths) > MAX_ATTACHMENTS_PER_FINDING for paths in per_finding):
        raise LifecycleError(f'at most {MAX_ATTACHMENTS_PER_FINDING} attachments per finding')
    if sum(map(len, per_finding)) > MAX_ATTACHMENTS_PER_ROUND:
        raise LifecycleError(f'at most {MAX_ATTACHMENTS_PER_ROUND} attachments per QA round')
    read = tuple(tuple(read_attachment(p) for p in paths) for paths in per_finding)
    if sum(a['size'] for files in read for a in files) > MAX_ROUND_ATTACHMENT_BYTES:
        raise LifecycleError(f'attachments exceed {MAX_ROUND_ATTACHMENT_BYTES} bytes per QA round')
    return tuple(zip(descriptions, read))


def _evidence_root(work_root):
    return Path(work_root).resolve()/'qa'/'evidence'


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_evidence(work_root, attachment, *, candidate_identity, qa_round, finding_id, index):
    """Idempotent: the ID is derived from content + linkage, so a retry reuses the same object."""
    metadata = dict(schema_version=1, filename=attachment['filename'], mime_type=attachment['mime_type'],
                    size=attachment['size'], sha256=attachment['sha256'], candidate_identity=candidate_identity,
                    qa_round=qa_round, finding_id=finding_id, index=index)
    metadata = dict(metadata, id='ev-'+hashlib.sha256(_json(metadata).encode()).hexdigest()[:32])
    root = _evidence_root(work_root); final = root/metadata['id']
    if final.exists():
        if load_evidence(work_root, metadata['id'], attachment['sha256']) != metadata:
            raise LifecycleError('evidence identity conflict; manual inspection required')
        return metadata
    root.mkdir(parents=True, exist_ok=True)
    building = root/f'.{metadata["id"]}.{uuid.uuid4().hex}'
    building.mkdir()
    for name, data in ((metadata['filename'], attachment['data']), ('metadata.json', _json(metadata).encode())):
        with (building/name).open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    _fsync_dir(building)
    os.rename(building, final)  # Atomic: a published ID is always complete.
    _fsync_dir(root)
    return metadata


def load_evidence(work_root, evidence_id, expected_sha256=None):
    """Verified metadata of one evidence object; the path is derived only from a validated ID."""
    return read_evidence(work_root, evidence_id, expected_sha256)[0]


def read_evidence(work_root, evidence_id, expected_sha256=None):
    """(metadata, bytes) from ONE read, verified against metadata and the referencing record."""
    if type(evidence_id) is not str or not _EVIDENCE_ID.fullmatch(evidence_id):
        raise WorkContractError('invalid evidence reference')
    directory = _evidence_root(work_root)/evidence_id
    try:
        metadata = json.loads((directory/'metadata.json').read_bytes())
        filename = metadata['filename']
        if (metadata.get('id') != evidence_id or type(filename) is not str or filename != _safe_filename(filename)
                or filename == 'metadata.json'):
            raise WorkContractError('invalid evidence metadata')
        data = (directory/filename).read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkContractError(f'evidence unavailable: {evidence_id}') from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != metadata['sha256'] or len(data) != metadata['size'] or expected_sha256 not in (None, digest):
        raise WorkContractError(f'evidence integrity mismatch: {evidence_id}')
    return metadata, data


def evidence_bytes(work_root, evidence_id):
    return read_evidence(work_root, evidence_id)[1]


def open_round_evidence(workspace):
    """Verified evidence of the CURRENT open round only: ((finding, ((metadata, bytes), ...)), ...)."""
    requests, resolved = fix_records(workspace)
    if len(requests) == len(resolved):
        return ()
    return tuple((f, tuple(read_evidence(workspace.root, e['id'], e['sha256']) for e in f.get('evidence', ())))
                 for f in request_findings(requests[-1]))


def evidence_plan(work_root, number, identity, inputs):
    """Publish every attachment of a not-yet-written round; returns per-finding metadata lists."""
    return [[publish_evidence(work_root, a, candidate_identity=identity, qa_round=number, finding_id=f'qa-{number}-{i}', index=k)
             for k, a in enumerate(files, 1)] for i, (_, files) in enumerate(inputs, 1)]


def request_findings(request):
    """Findings defined by a rejection intent; pre-findings records read as one `qa-<round>-1`."""
    if 'findings' in request:
        return tuple(dict(f) for f in request['findings'])
    return (dict(id=f"qa-{request['round']}-1", round=request['round'], description=request['feedback']),)


def fix_store(workspace, create=False):
    path = workspace.root/'qa_fix'
    if not create and not (path/'manifest.jsonl').exists():
        return None
    store = WorkWorkspace.open(path, FIX_CATALOG)
    store.initialize_request(workspace.read_artifact('request'))
    return store


def fix_records(workspace):
    """(requests, resolved) in order; resolved[i] closes requests[i] with the superseding candidate."""
    store = fix_store(workspace)
    if store is None:
        return (), ()
    requests = tuple(json.loads(store.read_artifact('qa_fix_request', r.version)) for r in store.versions('qa_fix_request'))
    resolved = tuple(json.loads(store.read_artifact('qa_fix_resolved', r.version)) for r in store.versions('qa_fix_resolved'))
    if (len(resolved) > len(requests) or any(r['round'] != i+1 for i, r in enumerate(requests))
            or any(r['round'] != i+1 for i, r in enumerate(resolved))):
        raise WorkContractError('inconsistent QA fix history')
    return requests, resolved


def write_fix_request(workspace, *, flow, reopen_to, descriptions, candidate_workspace, identity, candidate, work_id,
                      evidence=None):
    """The round's single durable rejection intent: feedback, findings AND evidence references.
    `evidence[i]` lists already-published evidence metadata of finding i (never unpublished files)."""
    descriptions = finding_descriptions(findings=descriptions)
    requests, resolved = fix_records(workspace)
    if len(requests) != len(resolved):
        raise WorkContractError('a corrective QA cycle is already open')
    # Lineage: round N+1 may only reject the candidate round N produced.
    if resolved and resolved[-1]['superseding_identity'] != identity:
        raise WorkContractError('QA lineage mismatch: rejected candidate is not the latest round result')
    files = sorted(candidate['files'])
    number = len(requests)+1
    # IDs are assigned once, here, scoped to the round; never derived from list position on read.
    findings = [dict(id=f'qa-{number}-{i}', round=number, description=text) for i, text in enumerate(descriptions, 1)]
    if evidence is not None and any(evidence):
        for finding, items in zip(findings, evidence):
            if any((e['qa_round'], e['finding_id'], e['candidate_identity']) != (number, finding['id'], identity) for e in items):
                raise WorkContractError('evidence linkage mismatch')
            for e in items:
                load_evidence(workspace.root, e['id'], e['sha256'])  # Only durable, intact evidence is referenced.
            finding['evidence'] = [dict(id=e['id'], sha256=e['sha256']) for e in items]
    record = dict(schema_version=4 if any('evidence' in f for f in findings) else 3, round=number, work_id=work_id, flow=flow,
                  reopen_to=reopen_to, feedback=combined_feedback(descriptions, number), findings=findings,
                  rejected_candidate=dict(identity=identity, workspace=str(candidate_workspace),
                      sequence=candidate.get('sequence', 1), base_head=candidate['base_head'],
                      branch=candidate['branch'],
                      files={p: candidate['files'][p] for p in files[:MAX_LISTED_FILES]},
                      files_omitted=max(0, len(files)-MAX_LISTED_FILES)))
    fix_store(workspace, create=True).write_artifact('qa_fix_request', _json(record))
    return record


def resolve_fix_request(workspace, superseding_identity, sequence, superseding_workspace):
    requests, resolved = fix_records(workspace)
    if len(requests) != len(resolved)+1:
        raise WorkContractError('no open corrective QA cycle')
    # schema 2 adds the explicit rejected -> produced link; schema-1 records lack both fields.
    fix_store(workspace).write_artifact('qa_fix_resolved', _json(dict(
        schema_version=2, round=len(requests), rejected_identity=requests[-1]['rejected_candidate']['identity'],
        superseding_identity=superseding_identity, sequence=sequence,
        superseding_workspace=str(superseding_workspace))))


def qa_rounds(workspace):
    """Durable round history: which candidate was rejected, by what feedback, and what it produced."""
    requests, resolved = fix_records(workspace)
    rounds = []
    for index, request in enumerate(requests):
        rejected = request['rejected_candidate']
        done = resolved[index] if index < len(resolved) else None
        if done is not None and done.get('rejected_identity', rejected['identity']) != rejected['identity']:
            raise WorkContractError('inconsistent QA fix history')
        rounds.append(dict(round=request['round'], feedback=request['feedback'], open=done is None,
            findings=request_findings(request),
            rejected=dict(identity=rejected['identity'], sequence=rejected['sequence'], workspace=rejected['workspace']),
            produced=None if done is None else dict(identity=done['superseding_identity'], sequence=done['sequence'],
                                                    workspace=done.get('superseding_workspace'))))
    return tuple(rounds)


def finding_states(rounds, accepted_lineage):
    """Pure function of append-only records: open (rejection intent) -> fixed (round resolved:
    its produced candidate is durably current) -> verified (an accepted candidate descends from it)."""
    result = []
    for item in rounds:
        state = 'open' if item['open'] else ('verified' if item['produced']['identity'] in accepted_lineage else 'fixed')
        result.append(dict(item, findings=tuple(dict(f, status=state) for f in item['findings'])))
    return tuple(result)


def qa_fix_snapshots(workspace):
    """Latest open feedback only: rejected-candidate summary plus the human's text."""
    from .context_transport import ArtifactSnapshot
    requests, resolved = fix_records(workspace)
    if len(requests) == len(resolved):
        return ()
    store = fix_store(workspace)
    record = store.versions('qa_fix_request')[len(requests)-1]
    return (ArtifactSnapshot(record, store.read_artifact('qa_fix_request', record.version)),)

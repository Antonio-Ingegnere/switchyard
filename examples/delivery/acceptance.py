"""Conservative Git acceptance: human-approved exact trees, never auto-staging."""
from dataclasses import dataclass
import hashlib
import re
import subprocess

from switchyard.git_safety import GitRepository, ScopeBlocked

from runtime import ExecutorResult, InvocationResponse


@dataclass(frozen=True)
class AcceptanceApproval:
    base_commit: str
    patch_sha256: str
    message: str
    manual_qa_passed: bool

    def __post_init__(self):
        if type(self.base_commit) is not str or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', self.base_commit):
            raise ValueError('approval requires exact base commit ID')
        if type(self.patch_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', self.patch_sha256):
            raise ValueError('approval requires exact patch SHA-256')
        if type(self.message) is not str or not self.message.strip() or '\0' in self.message:
            raise ValueError('commit message must be nonempty without NUL')
        if self.manual_qa_passed is not True:
            raise ValueError('explicit human manual QA approval required')


@dataclass(frozen=True)
class Snapshot:
    branch: str
    head: str
    tree: str
    patch_sha256: str


class GitAcceptance(GitRepository):
    def __init__(self, workspace: str, git_executable: str, approval: AcceptanceApproval | None = None):
        super().__init__(workspace, git_executable)
        self.approval = approval
        self.checked = None
        self.scoped = None

    def inspect(self) -> Snapshot:
        branch, head = self.check_repository()
        self.git('diff', '--quiet', '--ignore-submodules=none', '--')
        if self.git('ls-files', '--others', '--exclude-standard', '-z'):
            raise ScopeBlocked('untracked files require scope review')
        tree = self.git('write-tree').decode().strip()
        paths = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', '-z', head, tree, '--').split(b'\0')
        if not any(paths):
            raise ScopeBlocked('no staged change')
        if any(path == b'.switchyard' or path.startswith(b'.switchyard/') for path in paths):
            raise ScopeBlocked('execution artifacts must not be committed')
        patch = self.git('diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
                         '--no-renames', '--no-color', head, tree, '--')
        return Snapshot(branch, head, tree, hashlib.sha256(patch).hexdigest())

    def check_worktree(self):
        self.checked = self.inspect()

    def check_scope(self):
        current = self.inspect()
        if self.approval is None or self.checked != current:
            raise ScopeBlocked('missing approval or changed worktree')
        if (current.head, current.patch_sha256) != (self.approval.base_commit, self.approval.patch_sha256):
            raise ScopeBlocked('patch differs from human-approved scope')
        self.scoped = current

    def commit(self):
        if self.scoped is None or self.inspect() != self.scoped:
            raise ScopeBlocked('scope changed before commit')
        # Commit the immutable reviewed tree, never a subsequently changed index.
        commit = self.commit_tree(self.scoped.tree, self.scoped.head, self.approval.message)
        self.advance_branch(self.scoped.branch, commit, self.scoped.head)


@dataclass(frozen=True)
class GitStep:
    acceptance: GitAcceptance
    action: str

    def run(self, request):
        if request.diagnostics is not None:
            raise ValueError('Git acceptance does not consume diagnostics')
        try:
            getattr(self.acceptance, self.action)()
        except ScopeBlocked:
            outcome = 'failed'
        except (OSError, subprocess.SubprocessError):
            outcome = 'error'
        else:
            outcome = 'success'
        return InvocationResponse(ExecutorResult(outcome))

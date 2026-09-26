"""Conservative Git acceptance: human-approved exact trees, never auto-staging."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess

from runtime import ExecutorResult, InvocationResponse


class ScopeBlocked(ValueError):
    pass


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


class GitAcceptance:
    def __init__(self, workspace: str, git_executable: str, approval: AcceptanceApproval | None = None):
        for value in (workspace, git_executable):
            if type(value) is not str or not os.path.isabs(value) or '\0' in value:
                raise ValueError('workspace and Git executable must be absolute paths')
        self.workspace = Path(workspace).resolve()
        self.git_executable = git_executable
        self.approval = approval
        self.checked = None
        self.scoped = None

    def git(self, *args, input=None):
        result = subprocess.run([self.git_executable, *args], cwd=self.workspace,
                                input=input, capture_output=True, timeout=30)
        if result.returncode:
            raise ScopeBlocked('Git inspection or update failed')
        return result.stdout

    def inspect(self) -> Snapshot:
        root = Path(os.fsdecode(self.git('rev-parse', '--show-toplevel').strip())).resolve()
        if root != self.workspace:
            raise ScopeBlocked('workspace must be the repository root')
        branch = self.git('symbolic-ref', '--quiet', 'HEAD').decode().strip()
        head = self.git('rev-parse', '--verify', 'HEAD').decode().strip()
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG', 'sequencer'):
            path = Path(os.fsdecode(self.git('rev-parse', '--git-path', marker).strip()))
            if not path.is_absolute():
                path = self.workspace / path
            if path.exists():
                raise ScopeBlocked('Git operation in progress')
        if self.git('ls-files', '--unmerged', '-z'):
            raise ScopeBlocked('unmerged index')
        if any(entry.startswith(b'160000 ') for entry in self.git('ls-files', '--stage', '-z').split(b'\0')):
            raise ScopeBlocked('submodules are outside this acceptance preset')
        flags = self.git('ls-files', '-v', '-z').split(b'\0')
        if any(entry and (entry[:1] == b'S' or entry[:1].islower()) for entry in flags):
            raise ScopeBlocked('sparse or assume-unchanged index entries require review')
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
        commit = self.git('commit-tree', self.scoped.tree, '-p', self.scoped.head,
                          input=self.approval.message.encode('utf-8')).decode().strip()
        self.git('update-ref', '-m', 'Switchyard acceptance', self.scoped.branch, commit, self.scoped.head)


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

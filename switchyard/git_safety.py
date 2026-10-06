"""Shared conservative Git guards and immutable-tree commit primitives."""
import os
from pathlib import Path
import subprocess


class ScopeBlocked(ValueError):
    pass


class GitRepository:
    def __init__(self, workspace, git_executable):
        for value in (workspace, git_executable):
            if type(value) is not str or not os.path.isabs(value) or '\0' in value:
                raise ValueError('workspace and Git executable must be absolute paths')
        self.workspace = Path(workspace).resolve()
        self.git_executable = git_executable

    def git(self, *args, input=None):
        result = subprocess.run([self.git_executable, *args], cwd=self.workspace,
                                input=input, capture_output=True, timeout=30)
        if result.returncode:
            raise ScopeBlocked('Git inspection or update failed')
        return result.stdout

    def check_repository(self):
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
        return branch, head

    def commit_tree(self, tree, head, message):
        return self.git('commit-tree', tree, '-p', head, input=message.encode('utf-8')).decode().strip()

    def advance_branch(self, branch, commit, head):
        self.git('update-ref', '-m', 'Switchyard acceptance', branch, commit, head)

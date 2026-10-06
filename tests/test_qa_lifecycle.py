"""V2-007 real disposable Git boundaries; no provider resolution or invocation."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import Flow, State, WorkUnit, run
from switchyard.qa_lifecycle import (create_qa_candidate, accept_v2_work, reopen_v2_work,
                                     LifecycleError, LifecycleRecoveryRequired, _Git)
from switchyard.git_safety import ScopeBlocked
from switchyard.work_contract import WorkWorkspace, WorkContractError
from switchyard.workflow_bundle import ArtifactSpec


class QaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(); self.repo = self.base/'repo'; self.repo.mkdir()
        self.root = self.base/'qa'
        self.executable = shutil.which('git')
        if self.executable is None: self.skipTest('Git unavailable')
        # Eliminate host identity/config dependence without changing global config.
        env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base/'config'))
        self.env_patch = patch.dict(os.environ, env, clear=True); self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.git('init','-b','main')
        self.git('config','user.name','Offline QA')
        self.git('config','user.email','qa@example.invalid')
        self.git('config','commit.gpgsign','false')
        (self.repo/'source.txt').write_text('baseline\n')
        (self.repo/'tests.txt').write_text('independent tests\n')
        self.git('add','--','source.txt','tests.txt'); self.git('commit','-m','base')
        self.head = self.git('rev-parse','HEAD').strip()
        self.source = WorkWorkspace.open(self.base/'delivery', {'request':ArtifactSpec('text',source='request')})
        self.source.initialize_request('Improve behavior.\n')
        self.result = run(WorkUnit('Improve behavior.'), Flow('READY_FOR_MANUAL_QA',
                         {'READY_FOR_MANUAL_QA':State(terminal=True)}), {})
        (self.repo/'source.txt').write_text('reviewed\n')
        for name in ('switchyard.provider_stages.ProviderStageExecutorResolver.__call__',
                     'switchyard.provider_stages.execute'):
            guard=patch(name,side_effect=AssertionError('provider forbidden')); guard.start(); self.addCleanup(guard.stop)

    def git(self,*args):
        return subprocess.run([self.executable,*args],cwd=self.repo,capture_output=True,check=True).stdout.decode()

    def candidate(self):
        return create_qa_candidate(self.repo,self.source,self.result,'bugfix',self.root)

    def workspace(self):
        return WorkWorkspace.open(self.root)

    def data(self,name):
        return json.loads(self.workspace().read_artifact(name))

    def index(self):
        return (self.repo/'.git/index').read_bytes()

    def accept(self):
        return accept_v2_work(self.repo,self.root)

    def assert_blocked(self,result,reason,head=None,index=None):
        self.assertEqual((result.terminal,result.reason),('COMMIT_BLOCKED',reason))
        self.assertEqual(self.git('rev-parse','HEAD').strip(),head or self.head)
        if index is not None: self.assertEqual(self.index(),index)
        self.assertEqual(self.data('qa_decision')['reason'],reason)

    def test_candidate_exact_fingerprint_and_source_provenance(self):
        c=self.candidate(); data=self.data('qa_candidate')
        self.assertEqual(data['files'],{'source.txt':{'kind':'file','mode':'100644',
                         'sha256':hashlib.sha256(b'reviewed\n').hexdigest()}})
        self.assertEqual(data['base_head'],self.head)
        self.assertEqual(data['source_run_id'],self.result.events[0].run_id)
        self.assertEqual(data['source_request_sha256'],self.source.latest_artifact('request').sha256)
        self.assertEqual(c.identity,self.workspace().latest_artifact('qa_candidate').sha256)
        self.assertNotIn('reviewed\n',self.workspace().read_artifact('qa_candidate'))

    def test_safe_accept_exactly_one_commit_paths_message_and_clean(self):
        self.candidate(); result=self.accept()
        self.assertEqual(result.terminal,'ACCEPTED')
        self.assertEqual(self.git('rev-parse','HEAD').strip(),result.committed_head)
        self.assertEqual(self.git('rev-parse','HEAD^').strip(),self.head)
        self.assertEqual(self.git('rev-list','--count',self.head+'..HEAD').strip(),'1')
        self.assertEqual(self.git('diff-tree','--no-commit-id','--name-only','-r','HEAD').splitlines(),['source.txt'])
        self.assertEqual(self.git('log','-1','--format=%s').strip(),'bugfix: accept reviewed V2 work')
        self.assertEqual(self.git('show','HEAD:source.txt'),'reviewed\n')
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertEqual(self.data('qa_decision')['terminal'],'ACCEPTED')
        self.assertEqual(len(self.workspace().versions('qa_intent')),2)

    def test_added_and_deleted_paths(self):
        (self.repo/'source.txt').unlink()
        (self.repo/'new file.txt').write_bytes(b'new\x00bytes\n')
        self.candidate()
        self.assertEqual(self.data('qa_candidate')['files']['source.txt'],{'kind':'deleted'})
        self.assertEqual(self.data('qa_candidate')['files']['new file.txt']['kind'],'file')
        self.assertEqual(self.accept().terminal,'ACCEPTED')
        self.assertEqual(self.git('diff-tree','--no-commit-id','--name-status','-r','HEAD').splitlines(),
                         ['A\tnew file.txt','D\tsource.txt'])
        self.assertEqual(self.git('status','--porcelain'),'')

    def test_unrelated_untracked_file_blocks_and_preserves_both(self):
        self.candidate(); before=self.index();(self.repo/'other.txt').write_text('unrelated')
        self.assert_blocked(self.accept(),'unexpected_worktree_paths',index=before)
        self.assertEqual((self.repo/'other.txt').read_text(),'unrelated')
        self.assertEqual((self.repo/'source.txt').read_text(),'reviewed\n')

    def test_unrelated_tracked_edit_blocks(self):
        self.candidate(); (self.repo/'tests.txt').write_text('unrelated')
        self.assert_blocked(self.accept(),'unexpected_worktree_paths')

    def test_candidate_content_drift(self):
        self.candidate(); before=self.index(); (self.repo/'source.txt').write_text('post-QA change')
        self.assert_blocked(self.accept(),'candidate_drift',index=before)

    def test_removed_change_and_unexpected_deletion(self):
        self.candidate(); (self.repo/'source.txt').write_text('baseline\n')
        self.assert_blocked(self.accept(),'unexpected_worktree_paths')

    def test_candidate_unexpected_deletion(self):
        self.candidate(); (self.repo/'source.txt').unlink()
        self.assert_blocked(self.accept(),'candidate_drift')

    def test_base_head_drift(self):
        self.candidate(); self.git('commit','--allow-empty','-m','independent')
        head=self.git('rev-parse','HEAD').strip(); before=self.index()
        self.assert_blocked(self.accept(),'base_head_changed',head=head,index=before)

    def test_staged_content_preserved(self):
        self.candidate(); (self.repo/'other.txt').write_text('staged');self.git('add','other.txt')
        before=self.index()
        self.assert_blocked(self.accept(),'unexpected_staged_paths',index=before)
        self.assertIn('other.txt',self.git('diff','--cached','--name-only'))

    def test_even_candidate_staging_blocks(self):
        self.candidate(); self.git('add','source.txt'); before=self.index()
        self.assert_blocked(self.accept(),'unexpected_staged_paths',index=before)

    def test_intent_to_add_is_not_absorbed(self):
        self.candidate(); (self.repo/'other.txt').write_text('intent')
        self.git('add','--intent-to-add','other.txt'); before=self.index()
        self.assert_blocked(self.accept(),'unexpected_staged_paths',index=before)

    def test_missing_identity(self):
        self.candidate();self.git('config','--unset','user.name');self.git('config','--unset','user.email')
        self.assert_blocked(self.accept(),'git_identity_missing',index=self.index())

    def test_reopen_preserves_index_head_worktree_and_feedback(self):
        c=self.candidate();self.git('add','source.txt');before=self.index()
        result=reopen_v2_work(self.repo,self.root,'Still fails for an empty queue.')
        self.assertEqual(result.terminal,'REOPENED')
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.head)
        self.assertEqual(self.index(),before)
        self.assertEqual((self.repo/'source.txt').read_text(),'reviewed\n')
        self.assertEqual(self.workspace().read_artifact('qa_feedback'),'Still fails for an empty queue.')
        self.assertEqual(self.workspace().latest_artifact('qa_candidate').sha256,c.identity)
        self.assertEqual(self.data('qa_decision')['feedback_artifact'],'qa_feedback')

    def test_empty_reopen_feedback_does_not_finalize(self):
        self.candidate()
        for feedback in ('', ' \n', None, 'private\ud800'):
            with self.assertRaises(LifecycleError):reopen_v2_work(self.repo,self.root,feedback)
        self.assertEqual(self.workspace().versions('qa_intent'),())

    def test_accepted_cannot_commit_or_reopen_twice(self):
        self.candidate(); result=self.accept()
        for operation in (self.accept,lambda:reopen_v2_work(self.repo,self.root,'again')):
            with self.assertRaisesRegex(LifecycleError,'already finalized'):operation()
        self.assertEqual(self.git('rev-parse','HEAD').strip(),result.committed_head)
        self.assertEqual(len(self.workspace().versions('qa_decision')),1)

    def test_reopened_cannot_accept_or_reopen_again(self):
        self.candidate();reopen_v2_work(self.repo,self.root,'rework')
        with self.assertRaisesRegex(LifecycleError,'already finalized'):self.accept()
        with self.assertRaisesRegex(LifecycleError,'already finalized'):reopen_v2_work(self.repo,self.root,'again')

    def test_blocked_decision_is_final(self):
        self.candidate();(self.repo/'source.txt').write_text('drift');self.accept()
        with self.assertRaisesRegex(LifecycleError,'already finalized'):self.accept()

    def test_unresolved_intent_blocks_replay(self):
        self.candidate();self.workspace().write_artifact('qa_intent','{"phase":"started"}')
        with self.assertRaisesRegex(LifecycleError,'unresolved'):self.accept()
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.head)

    def test_git_conflict_and_hidden_flags(self):
        self.candidate();(self.repo/'.git/MERGE_HEAD').write_text(self.head+'\n')
        self.assert_blocked(self.accept(),'git_conflict_state')

    def test_assume_unchanged_is_blocked(self):
        self.candidate();self.git('update-index','--assume-unchanged','source.txt')
        self.assert_blocked(self.accept(),'git_conflict_state',index=self.index())

    def test_private_staging_failure_preserves_real_index(self):
        self.candidate();before=self.index(); original=_Git.git
        def fail(session,*args,**kwargs):
            if args[0]=='write-tree':raise ScopeBlocked('injected staging failure')
            return original(session,*args,**kwargs)
        with patch.object(_Git,'git',fail): result=self.accept()
        self.assert_blocked(result,'commit_failed',index=before)
        self.assertFalse((self.repo/'.git/index.lock').exists())

    def test_drift_after_private_staging_blocks_before_commit(self):
        self.candidate(); before=self.index(); original=_Git.verify_tree
        def mutate(session,*args):
            original(session,*args);(self.repo/'source.txt').write_text('concurrent edit')
        with patch.object(_Git,'verify_tree',mutate):result=self.accept()
        self.assert_blocked(result,'candidate_drift',index=before)
        self.assertEqual((self.repo/'source.txt').read_text(),'concurrent edit')

    def test_post_commit_failure_is_distinct_and_never_reset(self):
        self.candidate(); original=_Git.git
        def fail(session,*args,**kwargs):
            if args[0]=='status':raise ScopeBlocked('postcondition unavailable')
            return original(session,*args,**kwargs)
        with patch.object(_Git,'git',fail):result=self.accept()
        self.assertEqual((result.terminal,result.reason),('COMMIT_BLOCKED','post_commit_verification_failed'))
        self.assertEqual(self.git('rev-parse','HEAD').strip(),result.committed_head)
        self.assertNotEqual(result.committed_head,self.head)
        with self.assertRaisesRegex(LifecycleError,'already finalized'):self.accept()

    def test_decision_persistence_failure_blocks_replay_after_real_commit(self):
        self.candidate(); original=WorkWorkspace.write_artifact
        def fail(workspace,name,*args,**kwargs):
            if name=='qa_decision':raise WorkContractError('injected disk failure')
            return original(workspace,name,*args,**kwargs)
        with patch.object(WorkWorkspace,'write_artifact',fail):
            with self.assertRaises(LifecycleRecoveryRequired) as caught:self.accept()
        head=self.git('rev-parse','HEAD').strip()
        self.assertNotEqual(head,self.head)
        self.assertEqual(caught.exception.committed_head,head)
        self.assertEqual(self.data('qa_intent')['commit'],head)
        with self.assertRaisesRegex(LifecycleError,'unresolved'):self.accept()

    def test_ambiguous_ref_update_failure_never_claims_unchanged_head(self):
        self.candidate(); original=_Git.advance_branch
        def fail(session,*args):
            original(session,*args);raise ScopeBlocked('error after successful ref update')
        with patch.object(_Git,'advance_branch',fail):result=self.accept()
        self.assertEqual(result.reason,'post_commit_verification_failed')
        self.assertEqual(self.git('rev-parse','HEAD').strip(),result.committed_head)
        self.assertNotEqual(result.committed_head,self.head)

    def test_unverifiable_ref_publication_requires_recovery_and_blocks_replay(self):
        self.candidate(); original=_Git.git; unavailable=False
        def fail(session,*args,**kwargs):
            nonlocal unavailable
            if unavailable:raise ScopeBlocked('Git unavailable after publication')
            result=original(session,*args,**kwargs)
            if args[0]=='update-ref':
                unavailable=True
                raise ScopeBlocked('ambiguous publication')
            return result
        with patch.object(_Git,'git',fail):
            with self.assertRaises(LifecycleRecoveryRequired) as caught:self.accept()
        head=self.git('rev-parse','HEAD').strip()
        self.assertNotEqual(head,self.head)
        self.assertEqual(caught.exception.committed_head,head)
        self.assertEqual(self.data('qa_intent')['commit'],head)
        self.assertEqual(self.workspace().versions('qa_decision'),())
        with self.assertRaisesRegex(LifecycleError,'unresolved'):self.accept()

    def test_wrong_worktree_blocks(self):
        self.candidate();other=self.base/'other';other.mkdir()
        self.assert_blocked(accept_v2_work(other,self.root),'worktree_identity_changed')

    def test_capture_requires_qa_terminal_fresh_external_workspace(self):
        with self.assertRaises(LifecycleError):
            create_qa_candidate(self.repo,self.source,replace(self.result,terminal_state='NEEDS_HUMAN'),'bugfix',self.root)
        with self.assertRaises(LifecycleError):
            create_qa_candidate(self.repo,self.source,self.result,'bugfix',self.repo/'qa')
        self.candidate()
        with self.assertRaises(LifecycleError):self.candidate()

    def test_capture_staged_or_symlink_is_rejected(self):
        self.git('add','source.txt')
        with self.assertRaisesRegex(LifecycleError,'unexpected_staged_paths'):self.candidate()

    def test_capture_symlink_rejected(self):
        (self.repo/'link').symlink_to(self.repo/'source.txt')
        with self.assertRaisesRegex(LifecycleError,'unsupported_path'):self.candidate()

    def test_odd_literal_filename_and_mode(self):
        p=self.repo/'--strange\nfile';p.write_text('literal\n');p.chmod(0o755)
        self.candidate();self.assertEqual(self.accept().terminal,'ACCEPTED')
        self.assertIn('100755',self.git('ls-tree','HEAD'))
        self.assertEqual(self.git('status','--porcelain'),'')

    def test_cas_failure_preserves_competing_head_and_index(self):
        self.candidate();before=self.index();original=_Git.advance_branch; competing=[]
        def race(session,*args):
            tree=self.git('rev-parse','HEAD^{tree}').strip()
            commit=self.git('commit-tree',tree,'-p',self.head,'-m','external').strip()
            self.git('update-ref','refs/heads/main',commit,self.head);competing.append(commit)
            return original(session,*args)
        with patch.object(_Git,'advance_branch',race):result=self.accept()
        self.assert_blocked(result,'commit_failed',head=competing[0],index=before)


if __name__ == '__main__':unittest.main()

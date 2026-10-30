"""DELIVERY-SUSPEND (#4) slice 1: durable shelf -> SUSPENDED -> delivery worktree removed. Real Git."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard import recovery, suspend
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, accept_v2_candidate, qa_fix_v2_work,
                                 resume_v2_work, rollback_v2_work, answer_v2_work, recovery_status, qa_history, RecoveryBlocked)
from switchyard.suspend import suspend_v2_work, verify_shelf
from switchyard.work_contract import WorkWorkspace

from test_delivery_isolation import IsolationBase, Crash, REQUEST

ROOT = Path(__file__).resolve().parents[1]
LOGO = bytes(range(256))*4
NEW_BIN = b'\x00\xff\xfenot utf-8\x80'


def crash_when(original, condition):
    state = {'done': False}
    def wrapper(*args, **kwargs):
        if not state['done'] and condition(*args, **kwargs):
            state['done'] = True
            raise Crash()
        return original(*args, **kwargs)
    return wrapper


class SuspendBase(IsolationBase):
    def setUp(self):
        super().setUp()
        (self.target/'logo.png').write_bytes(LOGO); (self.target/'obsolete.py').write_text('old\n')
        (self.target/'script.sh').write_text('#!/bin/sh\necho hi\n'); (self.target/'keep.txt').write_text('unchanged\n')
        self.git('add', 'logo.png', 'obsolete.py', 'script.sh', 'keep.txt'); self.git('commit', '-qm', 'A2')
        self.base = self.git('rev-parse', 'HEAD')

    def interrupted(self):
        self.fail_once.write_text('1')
        with self.assertRaises(ExecutionError):
            self.start()
        self.assertEqual(recovery_status(self.work)['terminal'], 'INTERRUPTED')

    def dirty(self):
        d = self.delivery
        (d/'source.py').write_text('modified text\n')
        (d/'logo.png').write_bytes(LOGO[::-1])
        (d/'obsolete.py').unlink()
        (d/'notes').mkdir(); (d/'notes/new.bin').write_bytes(NEW_BIN)
        (d/'script.sh').chmod(0o755)
        (d/'tool.sh').write_text('#!/bin/sh\nexit 0\n'); (d/'tool.sh').chmod(0o755)
        (d/'link').symlink_to('keep.txt')

    def worktree_facts(self):
        d = self.delivery; facts = {}
        for path in sorted(p for p in d.rglob('*') if '.git' not in p.relative_to(d).parts):
            rel = str(path.relative_to(d))
            if path.is_symlink():
                facts[rel] = ('link', os.readlink(path))
            elif path.is_file():
                facts[rel] = (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode))
        return facts, self.git('status', '--porcelain', '--untracked-files=all', cwd=d)

    def durable(self):
        """Everything durable except the worktree, the shelf and the recovery operation log: must not change."""
        out = {}
        for path in sorted(self.work.rglob('*')):
            rel = path.relative_to(self.work).parts
            if not path.is_file() or rel[0] in ('workspace', 'suspend', '.recovery.lock'):
                continue
            if rel[0] == 'recovery' and (rel[-1] == 'manifest.jsonl' or 'operation' in rel):
                continue
            out['/'.join(rel)] = path.read_bytes()
        return out

    def ops(self):
        store = WorkWorkspace.open(self.work/'recovery')
        return [json.loads(store.read_artifact('operation', r.version)) for r in store.versions('operation')]

    def shelf(self, name='shelf-1'):
        return self.work/'suspend'/name


class SuspendTests(SuspendBase):
    # 1-11, 18, 20, 23
    def test_suspend_shelves_every_kind_records_suspended_then_removes_worktree(self):
        self.interrupted(); self.dirty()
        facts, git_status = self.worktree_facts()
        source = self.source_state(); durable = self.durable()
        order = []
        original = suspend._remove_worktree
        def removing(iso):
            op = self.ops()[-1]  # Removal happens only after SUSPENDED references a published shelf.
            order.append((op['phase'], op['worktree_removed'], self.shelf().is_dir()))
            return original(iso)
        with patch('switchyard.suspend._remove_worktree', removing):
            status = suspend_v2_work(self.work, 'Pause for the weekend')
        self.assertEqual(order, [('suspended', False, True)])
        self.assertEqual((status['terminal'], status['suspended'], status['suspend_reason'], status['shelf_id'],
                          status['workspace_cleanup_pending'], status['delivery_worktree_state'], status['resume_allowed'],
                          status['restore_pending'], status['workflow_state'], status['rollback_allowed']),
                         ('SUSPENDED', True, 'Pause for the weekend', 'shelf-1', False, 'removed', True,
                          False, 'IMPLEMENT', False))
        self.assertNotIn('resume_blocked', status)  # `resume` restores the shelf.
        self.assertNotIn('isolation_blocked', status)  # Intentionally absent worktree is healthy.
        self.assertEqual((status['base_sha'], status['delivery_branch']), (self.base, 'refs/heads/switchyard/work'))
        # Worktree gone (via Git), branch kept at its HEAD, source untouched, all other durable state untouched.
        self.assertFalse(self.delivery.exists()); self.assertEqual(self.worktrees(), [str(self.target)])
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), self.base)
        self.assertEqual(self.source_state(), source); self.assertEqual(self.durable(), durable)
        # The shelf is complete, verified and sufficient to recreate the exact state.
        manifest = verify_shelf(self.shelf())
        self.assertEqual(hashlib.sha256((self.shelf()/'manifest.json').read_bytes()).hexdigest(), status['shelf_manifest_sha256'])
        self.assertEqual((manifest['head'], manifest['base_sha'], manifest['delivery_branch'], manifest['work_id']),
                         (self.base, self.base, 'refs/heads/switchyard/work', 'work'))
        files = manifest['files']
        blob = lambda name: (self.shelf()/'blobs'/files[name]['blob']).read_bytes()
        self.assertEqual(blob('source.py'), b'modified text\n')
        self.assertEqual(blob('logo.png'), LOGO[::-1])
        self.assertEqual((blob('notes/new.bin'), files['notes/new.bin']['tracked']), (NEW_BIN, False))
        self.assertEqual(manifest['deleted'], ['obsolete.py']); self.assertNotIn('obsolete.py', files)
        self.assertEqual((files['script.sh']['mode'], files['script.sh']['tracked']), ('100755', True))
        self.assertEqual((files['tool.sh']['mode'], files['tool.sh']['tracked']), ('100755', False))
        self.assertEqual((files['link']['type'], files['link']['mode'], blob('link')), ('symlink', '120000', b'keep.txt'))
        self.assertIn('git_blob', files['keep.txt'])  # Unchanged bytes stay in the kept commit.
        self.assertEqual(manifest['index'], {'matches_head': True})
        self.assertFalse(any('.git' in p.split('/') or (p.startswith('.switchyard/') and p != '.switchyard/config.yaml') for p in files))
        self.assertIn('.gitignore', files)
        # Exactly the pre-suspend filesystem: every existing file's bytes/permissions and every link.
        expected = {}
        for name, entry in files.items():
            if entry['type'] == 'symlink':
                expected[name] = ('link', blob(name).decode())
            else:
                data = blob(name) if 'blob' in entry else subprocess.check_output(['git', 'cat-file', 'blob', entry['git_blob']], cwd=self.target)
                expected[name] = (hashlib.sha256(data).hexdigest(), entry['permissions'])
        self.assertEqual(expected, facts)
        self.assertIn('?? notes/new.bin', git_status); self.assertIn(' D obsolete.py', git_status)

    # 12
    def test_crash_while_writing_blobs_never_exposes_suspended(self):
        self.interrupted(); self.dirty()
        calls = {'n': 0}
        def second(*args):
            calls['n'] += 1; return calls['n'] == 2
        with patch('switchyard.suspend._write_blob', crash_when(suspend._write_blob, second)), self.assertRaises(Crash):
            suspend_v2_work(self.work)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['suspended'], status['resume_blocked']),
                         ('SUSPENDING', False, 'suspend_in_progress: retry suspend'))
        self.assertTrue(self.delivery.is_dir()); self.assertFalse(self.shelf().exists())
        self.assertEqual(len(list((self.work/'suspend').glob('.shelf-1.*'))), 1)  # Unpublished build only.
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(ctx.exception.reason, 'suspend_in_progress: retry suspend')
        self.assertEqual(suspend_v2_work(self.work)['terminal'], 'SUSPENDED')
        self.assertEqual(sorted(p.name for p in (self.work/'suspend').iterdir()), ['shelf-1'])

    # 13
    def test_crash_after_shelf_publication_reuses_the_same_shelf(self):
        self.interrupted(); self.dirty()
        with patch('switchyard.suspend._operation', crash_when(recovery._operation, lambda store, phase, **k: phase == 'suspended')), \
                self.assertRaises(Crash):
            suspend_v2_work(self.work, 'why')
        digest = hashlib.sha256((self.shelf()/'manifest.json').read_bytes()).hexdigest()
        self.assertEqual(recovery_status(self.work)['terminal'], 'SUSPENDING')
        status = suspend_v2_work(self.work)
        self.assertEqual((status['terminal'], status['shelf_manifest_sha256'], status['suspend_reason']), ('SUSPENDED', digest, 'why'))
        self.assertEqual(sorted(p.name for p in (self.work/'suspend').iterdir()), ['shelf-1'])

    def test_worktree_edited_after_published_shelf_is_refused_not_lost(self):
        self.interrupted(); self.dirty()
        with patch('switchyard.suspend._operation', crash_when(recovery._operation, lambda store, phase, **k: phase == 'suspended')), \
                self.assertRaises(Crash):
            suspend_v2_work(self.work)
        (self.delivery/'later.txt').write_text('edited after the shelf')
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'worktree_changed_since_shelf')
        self.assertTrue((self.delivery/'later.txt').is_file()); self.assertEqual(recovery_status(self.work)['terminal'], 'SUSPENDING')

    # 14, 15, 24-cleanup
    def test_crash_or_failure_before_worktree_removal_is_finished_by_retry(self):
        self.interrupted(); self.dirty()
        with patch('switchyard.suspend._remove_worktree', side_effect=Crash()), self.assertRaises(Crash):
            suspend_v2_work(self.work)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['suspended'], status['workspace_cleanup_pending'], status['delivery_worktree_state']),
                         ('SUSPENDED', True, True, 'present'))
        with patch('switchyard.suspend._remove_worktree', side_effect=RecoveryBlocked('RECOVERY_BLOCKED', 'git_inspection_failed')), \
                self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'workspace_cleanup_pending: worktree_remove_failed')
        self.assertTrue(recovery_status(self.work)['workspace_cleanup_pending'])
        status = suspend_v2_work(self.work)
        self.assertEqual((status['workspace_cleanup_pending'], status['delivery_worktree_state']), (False, 'removed'))
        count = len(self.ops())
        again = suspend_v2_work(self.work)  # Repeated suspend: no second shelf, no new record.
        self.assertEqual((again['shelf_id'], len(self.ops())), ('shelf-1', count))
        self.assertEqual(sorted(p.name for p in (self.work/'suspend').iterdir()), ['shelf-1'])
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), self.base)

    # 19
    def test_every_other_operation_refuses_suspended_explicitly(self):
        self.interrupted(); suspend_v2_work(self.work)
        for operation in (lambda: answer_v2_work(self.work, 'r', 'a', self.text, self.config),
                          lambda: qa_fix_v2_work(self.work, 'x', self.text, self.config),
                          lambda: accept_v2_candidate(self.work), lambda: rollback_v2_work(self.work)):
            with self.assertRaises(RecoveryBlocked) as ctx:
                operation()
            self.assertEqual(ctx.exception.reason, 'work_suspended: resume restores it first')
        out = subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'status', str(self.work)], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)['terminal'], 'SUSPENDED')

    # 21
    def test_missing_active_worktree_is_still_an_error(self):
        self.interrupted(); shutil.rmtree(self.delivery)
        status = recovery_status(self.work)
        self.assertEqual((status['isolation_blocked'], status['delivery_worktree_state']), ('delivery_worktree_missing', 'invalid'))
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'delivery_worktree_missing')
        self.assertFalse((self.work/'suspend').exists()); self.assertEqual(self.ops()[-1]['phase'], 'interrupted')

    # 22
    def test_unsupported_git_states_are_refused_and_staging_is_preserved(self):
        self.interrupted()
        merge_head = Path(self.git('rev-parse', '--git-path', 'MERGE_HEAD', cwd=self.delivery))
        merge_head = merge_head if merge_head.is_absolute() else self.delivery/merge_head
        merge_head.write_text(self.base+'\n')
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'unsupported_git_state: Git operation in progress')
        merge_head.unlink()
        self.git('update-index', '--add', '--cacheinfo', '160000,%s,vendor' % self.base, cwd=self.delivery)
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertTrue(ctx.exception.reason.startswith('unsupported_git_state: submodules'), ctx.exception.reason)
        self.git('rm', '-q', '--cached', 'vendor', cwd=self.delivery)
        # Staged-but-different content is recorded exactly, never flattened.
        (self.delivery/'source.py').write_text('staged\n'); self.git('add', 'source.py', cwd=self.delivery)
        (self.delivery/'source.py').write_text('unstaged on top\n')
        suspend_v2_work(self.work)
        manifest = verify_shelf(self.shelf())
        entry = next(e for e in manifest['index']['entries'] if e['path'] == 'source.py')
        self.assertEqual((self.shelf()/'blobs'/entry['blob']).read_bytes(), b'staged\n')
        self.assertEqual((self.shelf()/'blobs'/manifest['files']['source.py']['blob']).read_bytes(), b'unstaged on top\n')

    def test_final_and_running_states_are_not_suspendable(self):
        self.interrupted(); rollback_v2_work(self.work)
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'work_not_suspendable: rolled_back')
        self.assertTrue(self.delivery.is_dir()); self.assertFalse((self.work/'suspend').exists())


class SuspendQaTests(SuspendBase):
    def setUp(self):
        super().setUp()
        states = self.data['flows']['feature']['states']
        states['IMPLEMENT']['budget'] = states['VERIFY']['budget'] = {'visits': 3}
        self.text = json.dumps(self.data)

    # 16, 17, 18
    def test_qa_candidate_evidence_and_history_survive_suspend(self):
        execution = self.start()
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/'qa')
        attach_qa_candidate(self.work, self.root/'qa')
        log = self.root/'console.log'; log.write_bytes(b'TypeError: rename is undefined\n')
        qa_fix_v2_work(self.work, None, self.text, self.config, findings=[{'description': 'Rename fails', 'attachments': [str(log)]}])
        before = recovery_status(self.work); history = qa_history(self.work); durable = self.durable()
        self.assertEqual((before['terminal'], before['current_candidate_sequence']), ('READY_FOR_MANUAL_QA', 2))
        candidate_files = {p: p.read_bytes() for p in Path(before['qa_candidate']['workspace']).rglob('*') if p.is_file()}
        status = suspend_v2_work(self.work)
        self.assertEqual((status['terminal'], status['workflow_state']), ('SUSPENDED', 'READY_FOR_MANUAL_QA'))
        for key in ('qa_candidate', 'current_candidate_sequence', 'qa_fix_rounds', 'qa_verified_findings', 'qa_fixed_findings'):
            self.assertEqual(status[key], before[key], key)
        self.assertEqual(qa_history(self.work), history)  # Rounds, findings, evidence bytes verified.
        self.assertEqual(history['rounds'][0]['findings'][0]['evidence'][0]['filename'], 'console.log')
        self.assertEqual({p: p.read_bytes() for p in Path(before['qa_candidate']['workspace']).rglob('*') if p.is_file()}, candidate_files)
        self.assertEqual(self.durable(), durable)
        self.assertFalse(self.delivery.exists())

    def test_accepted_work_is_not_suspendable(self):
        execution = self.start()
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/'qa')
        attach_qa_candidate(self.work, self.root/'qa'); accept_v2_candidate(self.work)
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'work_not_suspendable: accepted')


class LegacySuspendTests(IsolationBase):
    # 24
    def test_legacy_in_place_work_cannot_be_suspended(self):
        self.fail_once.write_text('1')
        with self.assertRaises(ExecutionError):
            execute_recoverable_v2_work(self.text, 'feature', REQUEST, self.work, self.config, isolation=False)
        before = (self.work/'recovery/manifest.jsonl').read_bytes()
        with self.assertRaises(RecoveryBlocked) as ctx:
            suspend_v2_work(self.work)
        self.assertEqual(ctx.exception.reason, 'legacy_work_not_isolated')
        self.assertEqual((self.work/'recovery/manifest.jsonl').read_bytes(), before)
        self.assertFalse((self.work/'suspend').exists())
        self.assertEqual(recovery_status(self.work)['terminal'], 'INTERRUPTED')


if __name__ == '__main__':
    unittest.main()

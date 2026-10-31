"""TRANSPLANT (#8): explicit `work transplant` -- move a work's preserved implementation from base A onto B.

The same continuity engine as changed-base adoption (canonical product readers, continuity plan, Git 3-way
merge-tree, W2 provisioning); only the intent, the same-base refusal and the `TRANSPLANT_BLOCKED` surface
differ. Real Git, offline provider fixtures, real command verifier.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from switchyard import cli, continuity, recovery
from switchyard.continuity import (adopt_v2_work, transplant_v2_work, continuity_plan, derivation_chain,
                                   implementation_digest)
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, accept_v2_candidate, recovery_status,
                                 RecoveryBlocked, _operation)
from switchyard.suspend import suspend_v2_work
from switchyard.work_contract import WorkWorkspace

from test_continuity import ROOT
from test_continuity_history import HistoryBase
from test_continuity_transplant import TransplantBase
from test_delivery_isolation import Crash, crash_once, REQUEST
from test_delivery_suspend import LOGO

T = 'TRANSPLANT_BLOCKED'
CONFLICTS = [dict(path='logo.png', kind='binary'), dict(path='obsolete.py', kind='delete/modify'),
             dict(path='source.py', kind='content'), dict(path='tool.sh', kind='add/add')]


class ExplicitTransplantBase(TransplantBase):
    def transplant(self, old=None, new=None, config=None):
        return transplant_v2_work(old or self.work, new or self.w2, self.text, config or self.config)

    def w1_suspended(self):
        self.w1_ready(candidate=False); suspend_v2_work(self.work)
        self.assertEqual(recovery_status(self.work)['terminal'], 'SUSPENDED')

    def adoption(self, root):
        return json.loads(WorkWorkspace.open(root/'recovery').read_artifact('baseline'))['adoption']

    def blocked(self, call):
        with self.assertRaises(RecoveryBlocked) as ctx:
            call()
        return ctx.exception

    def tree_bytes(self, root, branch):
        out = {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}
        out['branch'] = self.git('rev-parse', branch).encode()
        return out


class SuspendedSourceTests(ExplicitTransplantBase):
    # 1-14, 19
    def test_suspended_work_is_transplanted_into_a_new_derived_work_without_being_resumed(self):
        self.w1_suspended(); w1 = self.w1_bytes(); w1_status = recovery_status(self.work); calls = self.model_calls()
        self.assertFalse(self.delivery.exists())
        b = self.advance(write={'keep.txt': 'upstream\n', 'upstream.bin': b'\x00B'})
        execution = self.transplant()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.model_calls(), calls)  # 0 provider invocations: only the command verifier ran.
        status = recovery_status(self.w2)
        self.assertEqual((status['work_id'], status['base_sha'], status['delivery_branch'], status['delivery_worktree']),
                         ('w2', b, 'refs/heads/switchyard/w2', str(self.d2)))
        self.assertEqual((status['derived'], status['derivation'], status['derived_from_work_id'], status['transplanted'],
                          status['transplant_from_base_sha'], status['transplant_to_base_sha'], status['transplant_strategy']),
                         (True, 'transplant', 'work', True, self.base, b, 'git_merge_tree_three_way'))
        self.assertTrue(status['adopted_implementation_source'].startswith('shelf:'))
        self.assertEqual(self.git('rev-parse', 'switchyard/w2'), b)  # Branch starts at B; the implementation is uncommitted.
        self.assertTrue(self.git('status', '--porcelain', cwd=self.d2))
        self.assertEqual(((self.d2/'source.py').read_text(), (self.d2/'logo.png').read_bytes()), ('changed', LOGO[::-1]))
        self.assertFalse((self.d2/'obsolete.py').exists())
        self.assertEqual(((self.d2/'keep.txt').read_text(), (self.d2/'upstream.bin').read_bytes()), ('upstream\n', b'\x00B'))
        # Provenance: one record, owned by W2, pointing at its immediate source.
        record = self.adoption(self.w2); plan = continuity_plan(self.w2); implement = self.implement(plan)
        shelf = json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('operation'))['shelf']
        self.assertEqual((record['intent'], record['from_work_id'], record['from_base_sha'], record['implementation_identity']),
                         ('transplant', 'work', self.base, shelf['manifest_sha256']))
        self.assertEqual(record['from_baseline_sha256'], WorkWorkspace.open(self.work/'recovery').latest_artifact('baseline').sha256)
        self.assertEqual((record['transplant']['from_base'], record['transplant']['to_base'], record['transplant']['result_digest']),
                         (self.base, b, implementation_digest(record['changes'])))
        self.assertEqual(status['transplant_result_digest'], record['transplant']['result_digest'])
        self.assertEqual([(d['stage'], d['action']) for d in plan['decisions']],
                         [('REQUEST', 'REUSED'), ('SPECIFY', 'REUSED'), ('IMPLEMENT', 'REUSED'), ('VERIFY', 'RERUN')])
        self.assertEqual(implement['reason'], 'deterministic_three_way_transplant')  # Not regenerated: never RERUN.
        self.assertEqual(implement['implementation']['source'], record['implementation_source'])
        # Fresh history and budgets: W2's log holds only its own VERIFY.
        events = tuple(read_events(self.w2/'events.jsonl'))
        self.assertEqual([e.state.split(':')[2] for e in events if e.kind == 'invocation_started'], ['VERIFY'])
        self.assertTrue({e.run_id for e in events}.isdisjoint({e.run_id for e in read_events(self.work/'events.jsonl')}))
        # W1 was read, never resumed: still SUSPENDED, no worktree, byte-identical.
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)), (w1, w1_status))
        self.assertFalse(self.delivery.exists())
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        commit = accept_v2_candidate(self.w2).committed_head
        self.assertEqual(self.git('rev-parse', commit+'^'), b)  # B -> C.
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)['terminal']), (w1, 'SUSPENDED'))


class SourceFormTests(ExplicitTransplantBase):
    # 15, 16
    def test_accepted_and_qa_ready_candidate_sources_transplant(self):
        forms = {}
        for form in ('candidate', 'accepted'):
            w1 = self.root/('w1-'+form); d = w1/'workspace'
            execution = execute_recoverable_v2_work(self.text, 'feature', REQUEST, w1, self.config)
            (d/'logo.png').write_bytes(LOGO[::-1])
            create_qa_candidate(d, execution.workspace, execution.result, 'feature', self.root/('qa-'+form))
            attach_qa_candidate(w1, self.root/('qa-'+form))
            if form == 'accepted':
                accept_v2_candidate(w1)
            forms[form] = w1
        b = self.advance(write={'keep.txt': 'B\n'})
        for form, w1 in forms.items():
            before = self.tree_bytes(w1, 'switchyard/'+w1.name); w2 = self.root/('w2-'+form)
            self.assertEqual(transplant_v2_work(w1, w2, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')
            status = recovery_status(w2)
            self.assertEqual((status['base_sha'], status['derivation'], status['derived_from_work_id']), (b, 'transplant', w1.name))
            self.assertTrue(status['adopted_implementation_source'].startswith({'candidate': 'candidate:', 'accepted': 'accepted_commit:'}[form]))
            self.assertEqual([(w2/'workspace'/n).read_bytes() for n in ('source.py', 'logo.png', 'keep.txt')], [b'changed', LOGO[::-1], b'B\n'])
            self.assertEqual(self.tree_bytes(w1, 'switchyard/'+w1.name), before)

    # 19
    def test_source_without_a_provable_implementation_is_blocked_before_any_write(self):
        self.w1_ready(candidate=False); self.advance(write={'keep.txt': 'B\n'})  # QA-ready, but no candidate.
        error = self.blocked(self.transplant)
        self.assertEqual((error.terminal, error.reason), (T, 'no_reusable_implementation'))
        self.assertFalse((self.w2/'recovery').exists())
        self.reset_source(); self.work, self.delivery = self.root/'w1b', self.root/'w1b/workspace'
        self.interrupted(); self.advance(write={'keep.txt': 'B\n'})
        error = self.blocked(self.transplant)
        self.assertEqual((error.terminal, error.reason), (T, 'implementation_uncertain: interrupted'))
        self.assertFalse((self.w2/'recovery').exists())


class RefusalTests(ExplicitTransplantBase):
    # 2, 30
    def test_same_base_transplant_is_refused_as_unnecessary_and_adopt_is_unchanged(self):
        self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        error = self.blocked(self.transplant)
        self.assertEqual((error.terminal, error.reason, error.paths),
                         (T, 'transplant_not_required: same_base (use work adopt to reuse it on this base, or work resume)', (self.base,)))
        self.assertFalse((self.w2/'recovery').exists())
        self.assertEqual((self.branches(), self.w1_bytes(), self.model_calls()), (['refs/heads/switchyard/work'], w1, calls))
        self.adopt()  # Ordinary same-base adoption: no transplant, intent `adopt`.
        status = recovery_status(self.w2)
        self.assertEqual((status['derived'], status['derivation'], status['transplanted'], status['base_sha']), (True, 'adopt', False, self.base))
        self.assertNotIn('transplant_strategy', status)
        self.assertEqual(self.implement(continuity_plan(self.w2))['reason'], 'durable_product_available')

    # 20, 21
    def test_unrelated_lineage_other_repository_and_dirty_target_are_refused(self):
        self.w1_ready()
        other = self.root/'other'; shutil.copytree(self.target, other); shutil.rmtree(other/'.git')
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'independent')):
            self.git(*args, cwd=other)
        error = self.blocked(lambda: self.transplant(config=self.provider(other)))
        self.assertEqual((error.terminal, error.reason), (T, 'repository_mismatch'))
        self.git('checkout', '-q', '--orphan', 'unrelated'); self.git('commit', '-qm', 'unrelated root')
        error = self.blocked(self.transplant)
        self.assertEqual((error.terminal, error.reason), (T, 'incompatible_base_lineage'))
        self.git('checkout', '-q', 'main'); self.advance(write={'keep.txt': 'B\n'}); (self.target/'stray.txt').write_text('dirt')
        error = self.blocked(self.transplant)  # The shared isolation preflight: never transplants onto dirty state.
        self.assertTrue(error.reason.startswith('dirty_source_worktree'))
        self.assertFalse((self.w2/'recovery').exists())

    # 15 (cycles)
    def test_a_work_cannot_be_transplanted_into_its_own_ancestry(self):
        self.w1_ready(); self.advance(write={'keep.txt': 'B\n'}); self.transplant()
        shutil.rmtree(self.work)  # W1's root is now a fresh path, but W2 still names it as its source.
        error = self.blocked(lambda: self.transplant(old=self.w2, new=self.work))
        self.assertEqual((error.terminal, error.reason), (T, 'derivation_cycle'))
        self.assertFalse((self.work/'recovery').exists())


class ConflictTests(ExplicitTransplantBase):
    # 22-27
    def test_conflicts_block_with_the_engine_s_structured_diagnostics_and_no_w2(self):
        self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        b = self.advance(write={'source.py': 'upstream edit', 'logo.png': LOGO[1:], 'obsolete.py': 'upstream keeps it\n',
                                'tool.sh': '#!/bin/sh\necho upstream tool\n'})
        source = self.source_state()
        errors = [self.blocked(self.transplant) for _ in range(2)]
        for error in errors:
            self.assertEqual((error.terminal, error.reason, error.paths), (T, 'transplant_conflict', ('logo.png', 'obsolete.py', 'source.py', 'tool.sh')))
        self.assertEqual([(e.transplant, e.continuity_plan) for e in errors][0], (errors[1].transplant, errors[1].continuity_plan))
        conflict = errors[0].transplant
        self.assertEqual(conflict['conflicts'], CONFLICTS)
        self.assertEqual((conflict['source_work_id'], conflict['from_base'], conflict['to_base'], conflict['strategy']),
                         ('work', self.base, b, 'git_merge_tree_three_way'))
        self.assertEqual((conflict['implementation']['source'], conflict['implementation']['identity']),
                         ('candidate:1', json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('qa_reference'))['identity']))
        self.assertEqual(self.implement(errors[0].continuity_plan)['reason'], 'transplant_conflict')
        adopted = self.blocked(self.adopt)  # Same engine, same normalized diagnostics; only the surface differs.
        self.assertEqual((adopted.terminal, adopted.reason, adopted.transplant, adopted.continuity_plan),
                         ('ADOPTION_BLOCKED', 'transplant_conflict', conflict, errors[0].continuity_plan))
        # No runnable W2, nothing conflicted anywhere, nothing of W1 or the target touched, no provider.
        self.assertFalse((self.w2/'recovery').exists()); self.assertFalse(self.d2.exists())
        self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])
        self.assertEqual((self.source_state(), self.w1_bytes(), self.model_calls()), (source, w1, calls))
        for root in (self.target, self.delivery):
            self.assertFalse([p for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts
                              and b'<<<<<<<' in p.read_bytes()])
        self.reset_source(); self.advance(delete=['source.py'])
        self.assertEqual(self.blocked(self.transplant).transplant['conflicts'], [dict(path='source.py', kind='modify/delete')])
        self.reset_source(); self.advance(write={'notes': 'a file where W1 adds a directory\n'})
        found = self.blocked(self.transplant).transplant['conflicts']  # Git also names its side-renamed `notes~<commit>`.
        self.assertEqual(found[0], dict(path='notes', kind='file/directory'))
        self.assertEqual({c['kind'] for c in found}, {'file/directory'})
        self.assertEqual(self.blocked(self.transplant).transplant['conflicts'], found)
        self.assertEqual(self.w1_bytes(), w1)


class TransplantRecoveryTests(ExplicitTransplantBase):
    # 28, 29
    def test_crashes_during_provisioning_converge_on_the_recorded_target(self):
        self.w1_suspended(); w1 = self.w1_bytes(); b = self.advance(write={'keep.txt': 'B\n'})
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.transplant()  # Intent and result blobs durable, branch created, no worktree yet.
        written = continuity_plan(self.w2)
        self.assertEqual(self.blocked(lambda: recovery_status(self.w2)).reason, 'transplant_incomplete: retry the same transplant')
        self.assertEqual(self.blocked(self.adopt).reason, 'derivation_in_progress: retry the same transplant')
        d = self.advance(write={'keep.txt': 'D\n'}, message='D')  # main moves on while the transplant is pending.
        original = continuity._write_change; count = {'n': 0}
        def write(*args):
            count['n'] += 1
            if count['n'] == 2:
                raise Crash()
            return original(*args)
        with patch('switchyard.continuity._write_change', write), self.assertRaises(Crash):
            self.transplant()  # Worktree exists, materialization half done.
        self.assertEqual(self.transplant().result.terminal_state, 'READY_FOR_MANUAL_QA')
        status = recovery_status(self.w2)
        self.assertEqual((status['base_sha'], status['transplant_to_base_sha'], self.git('rev-parse', 'switchyard/w2')), (b, b, b))
        self.assertNotEqual(b, d)
        self.assertEqual(((self.d2/'keep.txt').read_text(), (self.d2/'source.py').read_text()), ('B\n', 'changed'))
        self.assertEqual(continuity_plan(self.w2), written)
        self.assertEqual(status['transplant_result_digest'], self.implement(written)['transplant']['result_digest'])
        self.assertEqual(sorted(self.branches()), ['refs/heads/switchyard/w2', 'refs/heads/switchyard/work'])
        self.assertEqual(sorted(self.worktrees()), sorted([str(self.target), str(self.d2)]))
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)['terminal']), (w1, 'SUSPENDED'))


class ChainTests(ExplicitTransplantBase):
    # 14, 15, 17
    def test_a_transplanted_work_is_itself_a_transplant_source_and_points_at_its_immediate_source(self):
        self.w1_ready(); b = self.advance(write={'keep.txt': 'B\n'})
        execution = self.transplant()
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        w1, w2 = self.w1_bytes(), self.tree_bytes(self.w2, 'switchyard/w2')
        d = self.advance(write={'upstream.txt': 'D\n'}, message='D')
        w3 = self.root/'w3'
        self.assertEqual(self.transplant(old=self.w2, new=w3).result.terminal_state, 'READY_FOR_MANUAL_QA')
        status = recovery_status(w3)
        self.assertEqual((status['derived_from_work_id'], status['transplant_from_base_sha'], status['transplant_to_base_sha'], status['base_sha']),
                         ('w2', b, d, d))
        self.assertEqual(self.adoption(w3)['from_work_id'], 'w2')  # Immediate source only: not flattened to W1.
        self.assertEqual(self.adoption(w3)['source']['work_id'], 'w2')
        self.assertEqual(derivation_chain(w3), [w3, self.w2, self.work])
        self.assertEqual([(w3/'workspace'/n).read_text() for n in ('source.py', 'keep.txt', 'upstream.txt')], ['changed', 'B\n', 'D\n'])
        self.assertEqual((self.w1_bytes(), self.tree_bytes(self.w2, 'switchyard/w2')), (w1, w2))
        self.assertEqual([recovery_status(r)['terminal'] for r in (self.work, self.w2, w3)], ['READY_FOR_MANUAL_QA']*3)


class EngineTests(ExplicitTransplantBase):
    # 32
    def test_adopt_and_transplant_share_one_three_way_engine(self):
        sources = {p.name: p.read_text() for p in (ROOT/'switchyard').glob('*.py')}
        self.assertEqual([name for name, text in sources.items() if "'merge-tree'" in text], ['continuity.py'])
        self.assertEqual(sources['continuity.py'].count("'merge-tree'"), 1)
        self.w1_ready(); self.advance(write={'keep.txt': 'B\n'})
        seen = []; original = continuity._transplant
        with patch('switchyard.continuity._transplant', lambda plan, iso: seen.append(iso['delivery_branch']) or original(plan, iso)):
            self.transplant(); adopt_v2_work(self.work, self.root/'w3', self.text, self.config)
        self.assertEqual(seen, ['refs/heads/switchyard/w2', 'refs/heads/switchyard/w3'])
        self.assertEqual(self.adoption(self.w2)['changes'], self.adoption(self.root/'w3')['changes'])


class CliTests(ExplicitTransplantBase):
    # 21
    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'transplant', *args,
                               '--claude-executable', str(self.root/'claude'), '--codex-executable', str(self.root/'codex')],
                              cwd=ROOT, capture_output=True, text=True, env={**self.config.environment})

    def test_cli_transplants_and_reports_conflicts_structurally(self):
        self.w1_suspended()
        out = self.cli(str(self.work), str(self.w2))
        self.assertEqual(out.returncode, 2)
        self.assertIn('TRANSPLANT_BLOCKED: transplant_not_required: same_base', out.stderr)
        b = self.advance(write={'keep.txt': 'B\n'})
        out = self.cli(str(self.work), str(self.w2), '--source', str(self.target))
        self.assertEqual(out.returncode, 0, out.stderr)
        status = json.loads(out.stdout)
        self.assertEqual((status['derivation'], status['derived_from_work_id'], status['base_sha'], status['terminal']),
                         ('transplant', 'work', b, 'READY_FOR_MANUAL_QA'))
        self.reset_source(); self.advance(write={'source.py': 'upstream edit'})
        out = self.cli(str(self.work), str(self.root/'w3'))
        self.assertEqual(out.returncode, 2)
        lines = out.stderr.splitlines()
        self.assertIn('TRANSPLANT_BLOCKED: transplant_conflict', lines[0])
        self.assertEqual(json.loads(lines[1])['conflicts'], [dict(path='source.py', kind='content')])


class DefaultSourceTests(ExplicitTransplantBase):
    """The CLI's default `--source` comes from W1's frozen baseline, never from its live (reconciling) status."""
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'switchyard', 'work', *args,
                               '--claude-executable', str(self.root/'claude'), '--codex-executable', str(self.root/'codex')],
                              cwd=ROOT, capture_output=True, text=True, env={**self.config.environment})

    def dead_running(self):
        """W1 recorded RUNNING whose provider process no longer exists: status would reconcile it to INTERRUPTED."""
        self.w1_ready()
        dead = subprocess.Popen([sys.executable, '-c', '']); dead.wait()
        store = WorkWorkspace.open(self.work/'recovery')
        _operation(store, 'running', state='@v2:feature:VERIFY', provider_pid=dead.pid)

    def phase(self):
        return json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('operation'))['phase']

    # 2, 4
    def test_dead_running_source_stays_byte_identical_for_transplant_and_adopt(self):
        self.dead_running(); self.advance(write={'keep.txt': 'B\n'})
        w1 = self.w1_bytes()
        for action, terminal in (('transplant', T), ('adopt', 'ADOPTION_BLOCKED')):
            out = self.run_cli(action, str(self.work), str(self.root/('new-'+action)))
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn(terminal+': implementation_uncertain: running', out.stderr)
            self.assertEqual((self.w1_bytes(), self.phase()), (w1, 'running'))  # Not reconciled to INTERRUPTED.

    # 1, 5
    def test_default_source_never_consults_live_status_of_the_source(self):
        self.w1_suspended(); b = self.advance(write={'keep.txt': 'B\n'})
        self.assertEqual(continuity.default_source(self.work), str(self.target))
        original = recovery.recovery_status
        def status(root):
            self.assertNotEqual(Path(root).resolve(), self.work, 'source status consulted')
            return original(root)
        stdout = io.StringIO()
        with patch('switchyard.recovery.recovery_status', status), patch.dict(os.environ, self.config.environment), \
                contextlib.redirect_stdout(stdout):
            code = cli.main(['work', 'transplant', str(self.work), str(self.w2), '--claude-executable', str(self.root/'claude'),
                             '--codex-executable', str(self.root/'codex')])
        self.assertEqual(code, 0)
        self.assertEqual((json.loads(stdout.getvalue())['base_sha'], json.loads(stdout.getvalue())['derivation']), (b, 'transplant'))
        self.assertEqual(recovery_status(self.work)['terminal'], 'SUSPENDED')

    def test_missing_baseline_is_a_derivation_refusal(self):
        (self.root/'empty').mkdir()
        out = self.run_cli('transplant', str(self.root/'empty'), str(self.w2))
        self.assertEqual(out.returncode, 2)
        self.assertIn('TRANSPLANT_BLOCKED: unsupported_source_schema: no recovery baseline', out.stderr)
        self.assertEqual(list((self.root/'empty').iterdir()), [])


class HistoricalSourceTests(HistoryBase, ExplicitTransplantBase):
    # 18
    def test_release_2_3_work_transplants_through_the_canonical_reader(self):
        self.historical(); before = self.history_bytes()
        self.target_at(self.base)
        self.assertEqual(self.blocked(self.transplant).reason.split(' ')[0], 'transplant_not_required:')
        self.git('checkout', '-q', 'main'); self.git('branch', '-q', '-D', 'w2-target')
        b = self.target_at(self.base, write={'keep.txt': 'upstream\n'})
        self.transplant()
        status = recovery_status(self.w2)
        self.assertEqual((status['derivation'], status['source_work_schema_version'], status['transplant_from_base_sha'], status['base_sha']),
                         ('transplant', 2, self.base, b))
        self.assertEqual(((self.d2/'source.py').read_text(), (self.d2/'keep.txt').read_text()), ('changed', 'upstream\n'))
        self.assertEqual(self.history_bytes(), before)

    # 3
    def test_cli_default_source_of_a_schema_2_work_is_its_recorded_checkout(self):
        self.historical(); before = self.history_bytes()
        b = self.target_at(self.base, write={'keep.txt': 'upstream\n'})
        out = subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'transplant', str(self.work), str(self.w2),
                              '--claude-executable', str(self.root/'claude'), '--codex-executable', str(self.root/'codex')],
                             cwd=ROOT, capture_output=True, text=True, env={**self.config.environment})
        self.assertEqual(out.returncode, 0, out.stderr)
        status = json.loads(out.stdout)
        self.assertEqual((status['base_sha'], status['source_work_schema_version'], status['derivation']), (b, 2, 'transplant'))
        self.assertEqual(self.history_bytes(), before)


if __name__ == '__main__':
    unittest.main()

"""CONTINUITY (#5) slice 4: adopt works produced by an older runtime through a read-only schema reader.

The historical W1 is a REAL release 2.3.0 work: the release's own code (`git archive` of the RELEASE-2.3.0
commit, with its own offline provider fixture) runs in a subprocess and produces it. Nothing is hand-built,
and the test skips when that commit is not available in this checkout.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

from switchyard import recovery
from switchyard.continuity import adopt_v2_work, continuity_plan
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import attach_qa_candidate, accept_v2_candidate, recovery_status, RecoveryBlocked, CATALOG
from switchyard.versioning import SchemaError, work_schema, runtime_version
from switchyard.work_contract import WorkWorkspace

from test_continuity import ContinuityBase
from test_delivery_isolation import Crash, crash_once

ROOT = Path(__file__).resolve().parents[1]
RELEASE = 'dfd5c4d325d724549d1370ed2083b08f70fbaf7d'  # RELEASE-2.3.0
GENERATE = '''import json, sys
from switchyard.recovery import execute_recoverable_v2_work, attach_qa_candidate
from switchyard.qa_lifecycle import create_qa_candidate, accept_v2_work
from switchyard.provider_stages import ProviderRuntimeConfig
a = json.loads(sys.argv[1])
config = ProviderRuntimeConfig(a['target'], a['claude'], a['codex'], environment=a['env'])
execution = execute_recoverable_v2_work(a['text'], 'feature', a['request'], a['work'], config)
assert execution.result.terminal_state == 'READY_FOR_MANUAL_QA', execution.result.terminal_state
create_qa_candidate(a['target'], execution.workspace, execution.result, 'feature', a['qa'])
attach_qa_candidate(a['work'], a['qa'])
if a['accept']:
    assert accept_v2_work(a['target'], a['qa']).terminal == 'ACCEPTED'
'''


def release_tree(dest):
    if subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', RELEASE+'^{commit}'], capture_output=True).returncode:
        raise unittest.SkipTest('release 2.3.0 commit not available in this checkout')
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', RELEASE, 'switchyard', 'runtime', 'flow_config.py',
                              'tests/fixtures/provider_cli.py'], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter='data')
    (dest/'generate.py').write_text(GENERATE)
    return dest


class HistoryBase(ContinuityBase):
    request = 'Make source.py say changed'

    def historical(self, accept=True):
        """W1 written by release 2.3.0 in place in the caller's checkout (schema 2)."""
        release = release_tree(self.root/'release-2.3.0')
        script = (release/'tests/fixtures/provider_cli.py').read_text().replace(
            'outcome=plan.get', "if state=='IMPLEMENT':\n    Path('source.py').write_text('changed')\noutcome=plan.get")
        bin_dir = self.root/'release-bin'; bin_dir.mkdir()
        for name in ('claude', 'codex'):  # The fixture selects its CLI protocol from its own name.
            exe = bin_dir/name; exe.write_text('#!'+sys.executable+'\n'+script); exe.chmod(0o755)
        args = dict(target=str(self.target), claude=str(bin_dir/'claude'), codex=str(bin_dir/'codex'),
                    env={**{k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}, 'FAKE_PLAN': str(self.plan),
                         'FAKE_LOG': str(self.log), 'FAKE_CONTRACT_AWARE': '1', 'FAIL_ONCE': str(self.fail_once)},
                    text=self.text, request=self.request, work=str(self.work), qa=str(self.root/'qa1'), accept=accept)
        done = subprocess.run([sys.executable, str(release/'generate.py'), json.dumps(args)], cwd=release,
                              env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        self.c1 = self.git('rev-parse', 'main')

    def history_bytes(self):
        out = {str(p.relative_to(self.root)): p.read_bytes() for base in (self.work, self.root/'qa1')
               for p in sorted(base.rglob('*')) if p.is_file()}
        out['main'] = self.git('rev-parse', 'main').encode()
        return out

    def target_at(self, base, write=None):
        """A clean source branch for W2: the historical base, optionally advanced (B)."""
        self.git('checkout', '-q', '-b', 'w2-target', base)
        for name, text in (write or {}).items():
            (self.target/name).write_text(text)
        if write:
            self.git('add', '-A'); self.git('commit', '-qm', 'B')
        return self.git('rev-parse', 'HEAD')


class ReleaseWorkTests(HistoryBase):
    # 2-10, 20
    def test_release_2_3_accepted_work_is_adopted_on_the_same_base(self):
        self.historical(); before = self.history_bytes(); calls = self.model_calls()
        w1 = WorkWorkspace.open(self.work)
        base = json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('baseline'))
        self.assertEqual((work_schema(base), 'isolation' in base, base.get('runtime_version')), (2, False, None))  # Real 2.3.0 shape.
        self.assertEqual(self.git('rev-parse', 'main^'), self.base)  # 2.3.0 accepted in place: main moved A -> C1.
        self.target_at(self.base)
        execution = self.adopt()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        status = recovery_status(self.w2)
        self.assertEqual((status['source_work_schema_version'], status['source_runtime_version'],
                          status['target_work_schema_version'], status['target_runtime_version']), (2, None, 3, runtime_version()))
        self.assertEqual((status['adopted_implementation_source'], status['base_sha'], status['transplanted']),
                         ('accepted_commit:'+self.c1, self.base, False))
        plan = continuity_plan(self.w2)
        self.assertEqual([(d['stage'], d['action']) for d in plan['decisions']],
                         [('REQUEST', 'REUSED'), ('SPECIFY', 'REUSED'), ('IMPLEMENT', 'REUSED'), ('VERIFY', 'RERUN')])
        w2 = WorkWorkspace.open(self.w2)
        for name in ('request', 'microspec', 'implementation_handoff'):
            self.assertEqual(w2.read_artifact(name), w1.read_artifact(name), name)
        self.assertEqual((self.d2/'source.py').read_text(), 'changed')
        events = tuple(read_events(self.w2/'events.jsonl'))
        self.assertEqual([e.state.split(':')[2] for e in events if e.kind == 'invocation_started'], ['VERIFY'])
        self.assertTrue({e.run_id for e in events}.isdisjoint({e.run_id for e in read_events(self.work/'events.jsonl')}))
        source = json.loads(WorkWorkspace.open(self.w2/'recovery').read_artifact('baseline'))['adoption']['source']
        self.assertEqual((source['reader'], source['work_schema_version']), ('work-schema-2-in-place', 2))
        self.assertEqual((self.history_bytes(), self.model_calls()), (before, calls))
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        commit = accept_v2_candidate(self.w2).committed_head  # Current isolated acceptance, not 2.3.0's in-place one.
        self.assertEqual((self.git('rev-parse', commit+'^'), self.git('rev-parse', 'switchyard/w2')), (self.base, commit))
        self.assertEqual(self.history_bytes(), before)

    # 11
    def test_release_2_3_accepted_work_transplants_onto_a_changed_base(self):
        self.historical(); before = self.history_bytes()
        b = self.target_at(self.base, write={'keep.txt': 'upstream\n'})
        execution = self.adopt()
        status = recovery_status(self.w2)
        self.assertEqual((status['base_sha'], status['transplanted'], status['transplant_from_base_sha'], status['source_work_schema_version']),
                         (b, True, self.base, 2))
        self.assertEqual(((self.d2/'source.py').read_text(), (self.d2/'keep.txt').read_text()), ('changed', 'upstream\n'))
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        self.assertEqual(self.git('rev-parse', accept_v2_candidate(self.w2).committed_head+'^'), b)
        self.assertEqual(self.history_bytes(), before)

    # 13
    def test_release_2_3_candidate_without_acceptance_is_uncertain(self):
        self.historical(accept=False); before = self.history_bytes(); calls = self.model_calls()
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'implementation_uncertain: work schema 2 candidate bytes live only in the caller checkout')
        self.assertFalse((self.w2/'recovery').exists()); self.assertEqual((self.history_bytes(), self.model_calls()), (before, calls))

    # 12
    def test_historical_artifact_contract_change_invalidates_deterministically(self):
        self.historical(); self.target_at(self.base)
        data = json.loads(self.text); data['flows']['feature']['states']['SPECIFY']['produces']['microspec'] = dict(format='json', schema='microspec')
        plans = []
        for _ in range(2):
            with self.assertRaises(RecoveryBlocked) as ctx:
                adopt_v2_work(self.work, self.w2, json.dumps(data), self.config)
            self.assertEqual(ctx.exception.reason, 'continuity_invalidated: SPECIFY'); plans.append(ctx.exception.continuity_plan)
        self.assertEqual(plans[0], plans[1])

    # 17, 18
    def test_retry_canonicalizes_to_the_same_source_and_refuses_a_changed_one(self):
        self.historical(); self.target_at(self.base)
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        digest = json.loads(WorkWorkspace.open(self.w2/'recovery').read_artifact('operation'))['adoption']['source']['product_digest']
        WorkWorkspace.open(self.work).write_artifact('microspec', WorkWorkspace.open(self.work).read_artifact('microspec')+'# later edit\n')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'source_product_changed')
        self.assertEqual(json.loads(WorkWorkspace.open(self.w2/'recovery').read_artifact('operation'))['adoption']['source']['product_digest'], digest)
        self.assertFalse(self.d2.exists())

    def test_retry_with_an_unchanged_historical_source_completes(self):
        self.historical(); self.target_at(self.base)
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        written = continuity_plan(self.w2)
        self.assertEqual(self.adopt().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(continuity_plan(self.w2), written)


    def test_cli_adopts_schema_2_work_from_its_recorded_checkout(self):
        self.historical(); self.target_at(self.base)
        out = subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'adopt', str(self.work), str(self.w2),
                              '--claude-executable', str(self.root/'claude'), '--codex-executable', str(self.root/'codex')],
                             cwd=ROOT, capture_output=True, text=True, env={**self.config.environment})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((json.loads(out.stdout)['source_work_schema_version'], json.loads(out.stdout)['base_sha']), (2, self.base))


class SchemaIdentificationTests(unittest.TestCase):
    base = dict(snapshot={}, flow='feature', config='{}')

    # 14, 15, 16
    def test_schema_identification_never_guesses(self):
        self.assertEqual(work_schema(dict(self.base)), 1)  # The runtime's own convention: no field = contract 1.
        self.assertEqual(work_schema(dict(self.base, lifecycle_contract_version=2)), 2)
        self.assertEqual(work_schema(dict(self.base, lifecycle_contract_version=2, isolation={})), 3)
        self.assertEqual(work_schema(dict(self.base, lifecycle_contract_version=2, isolation={}, work_schema_version=3)), 3)
        for baseline, reason in ((dict(self.base, lifecycle_contract_version=2, work_schema_version=4), 'source_schema_too_new'),
                                 (dict(self.base, lifecycle_contract_version=3), 'source_schema_too_new'),
                                 (dict(self.base, lifecycle_contract_version=2, work_schema_version=3), 'ambiguous_source_schema'),
                                 (dict(self.base, lifecycle_contract_version='2'), 'ambiguous_source_schema'),
                                 (dict(flow='feature', config='{}'), 'ambiguous_source_schema')):
            with self.subTest(baseline), self.assertRaises(SchemaError) as ctx:
                work_schema(baseline)
            self.assertTrue(ctx.exception.reason.startswith(reason), ctx.exception.reason)


class SchemaRefusalTests(ContinuityBase):
    def fabricated(self, **fields):
        """A minimal recovery store with a given baseline: only to reach the schema boundary."""
        store = WorkWorkspace.open(self.work/'recovery', CATALOG); store.initialize_request('x')
        store.write_artifact('baseline', json.dumps(dict(snapshot={}, flow='feature', config=self.text, **fields)))
        store.write_artifact('operation', json.dumps(dict(phase='delivery_done', terminal='READY_FOR_MANUAL_QA')))

    # 14, 15, 16
    def test_unsupported_too_new_and_ambiguous_sources_are_refused_before_any_write(self):
        for fields, reason in ((dict(), 'unsupported_source_schema: work schema 1'),
                               (dict(lifecycle_contract_version=2, work_schema_version=7), 'source_schema_too_new'),
                               (dict(lifecycle_contract_version=2, work_schema_version=3), 'ambiguous_source_schema')):
            with self.subTest(reason):
                import shutil
                shutil.rmtree(self.work, ignore_errors=True); self.fabricated(**fields); calls = self.model_calls()
                before = {p: p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
                with self.assertRaises(RecoveryBlocked) as ctx:
                    self.adopt()
                self.assertTrue(ctx.exception.reason.startswith(reason), ctx.exception.reason)
                self.assertEqual(({p: p.read_bytes() for p in self.work.rglob('*') if p.is_file()}, self.model_calls()), (before, calls))
                self.assertFalse((self.w2/'recovery').exists())

    # 1, 19
    def test_new_works_record_their_runtime_and_schema(self):
        self.w1_ready()
        base = json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('baseline'))
        self.assertEqual((base['work_schema_version'], base['runtime_version'], work_schema(base)), (3, runtime_version(), 3))
        self.assertNotIn('adopted', recovery_status(self.work))
        self.adopt()
        status = recovery_status(self.w2)
        self.assertEqual((status['source_work_schema_version'], status['source_runtime_version'], status['target_work_schema_version']),
                         (3, runtime_version(), 3))


if __name__ == '__main__':
    unittest.main()

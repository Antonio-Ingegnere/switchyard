"""ECONOMICS-006 (#2): continuity savings. Real offline works (explicit TEST prices): W1 delivered with a failed attempt
and a QA round, adopted into W2, transplanted onward to W3, reused again by W4. Read only from frozen records."""
import contextlib
from decimal import Decimal
import hashlib
import io
import json
import os
import shutil
import unittest
from unittest.mock import patch

from switchyard import economics, savings
from switchyard.cli import main
from switchyard.continuity import adopt_v2_work, transplant_v2_work
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import RecoveryBlocked, attach_qa_candidate, qa_fix_v2_work, resume_v2_work

from test_economics import SPLIT, CostBase, catalog
from test_continuity_transplant import TransplantBase

M = 'claude-sonnet-4-6'


def tree(*bases):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for base in bases for p in sorted(base.rglob('*'))
            if p.is_file() and not p.is_symlink()}


class SavingsBase(CostBase, TransplantBase):
    def cli(self, work, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(['work', 'savings', str(work), *args])
        return code, out.getvalue()

    def costs(self, work):
        return {r['invocation_id']: r for r in economics.read_costs(work)[0]}

    def produced_by(self, work, artifact):
        """Independently: the invocations whose live trace refs name the artifact (the latest version)."""
        out = []
        for e in self.events(work, event_type='invocation_finished'):
            for ref in e['payload'].get('artifact_refs') or []:
                if ref.get('name') == artifact:
                    out.append((ref['version'], e['invocation_id']))
        return max(out)[1]

    def candidate_for(self, work, delivery, execution, name):
        create_qa_candidate(delivery, execution.workspace, execution.result, 'feature', self.root/name)
        attach_qa_candidate(work, self.root/name)

    def w1_history(self):
        """W1: a failed IMPLEMENT attempt, a resumed success, one QA correction round; current candidate undecided."""
        failed = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True, usage=dict(SPLIT, input_tokens=100),
                                 modelUsage={M: dict(inputTokens=100, outputTokens=340, cacheReadInputTokens=8000,
                                                     cacheCreationInputTokens=500)})).encode()
        self.deliver(replace_env(self.claude_emits(failed, exit_code=1), FAKE_CODEX_USAGE=self.codex_usage()), fails=True)
        execution = resume_v2_work(self.work, self.text, self.priced_config())
        self.candidate(execution, 'qa1')
        execution = qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.priced_config())
        self.candidate(execution, 'qa1b')
        return execution

    def codex_usage(self):
        from test_economics import CODEX
        return json.dumps(CODEX)


def replace_env(config, **env):
    from dataclasses import replace
    return replace(config, environment={**config.environment, **env})


class DirectAdoptionTests(SavingsBase):
    # 1, 3-16, 19-21, 23, 24, 26-33
    def test_direct_adoption_separates_historical_new_and_proxy_and_stays_read_only(self):
        self.w1_history()
        w1 = self.costs(self.work)
        execution = adopt_v2_work(self.work, self.w2, self.text, self.priced_config())
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')  # 33: continuity semantics unchanged.
        before = tree(self.work, self.w2)

        r = savings.report(self.w2)
        decisions = {d['stage']: d for d in r['reuse']['decisions']}
        self.assertEqual([(d['stage'], d['action']) for d in r['reuse']['decisions']][-1], ('VERIFY', 'RERUN'))  # 3
        self.assertEqual((r['reuse']['reused_stages'], r['reuse']['rerun_stages'], r['reuse']['invalidated_stages']),
                         (len(decisions)-1, 1, 0))
        self.assertEqual(decisions['REQUEST']['provenance']['attribution'], 'not_applicable')  # 7: no model spending.
        spec, impl = decisions['SPECIFY']['provenance'], decisions['IMPLEMENT']['provenance']
        self.assertEqual((spec['producer_work_id'], impl['producer_work_id'], r['historical_source']['producers']), ('work', 'work', ['work']))
        self.assertEqual(spec['attributable'], [self.produced_by(self.work, 'microspec')])  # 5, 6: the exact producing invocation.
        self.assertEqual(impl['attributable'], [self.produced_by(self.work, 'implementation_handoff')])  # 4
        implement_runs = [i for i, c in w1.items() if c['state'] == 'IMPLEMENT']
        self.assertGreaterEqual(len(implement_runs), 3)  # 12-14: failed attempt, resumed success, QA round.
        self.assertEqual(sorted(impl['associated'] + impl['attributable']), sorted(implement_runs))  # Never guessed into the proxy.
        self.assertIsNotNone(decisions['IMPLEMENT']['implementation_digest'])

        proxy_ids = spec['attributable'] + impl['attributable']
        expected = sum(Decimal(w1[i]['estimated_amount']) for i in proxy_ids if w1[i]['estimated_amount'])
        a, h = r['avoided_regeneration'], r['historical_source']
        self.assertEqual(Decimal(a['known_estimated_cost']), expected)  # 8: frozen source amounts, exact.
        self.assertEqual((a['basis'], a['billed_savings'], a['actual_savings']), ('historical_execution_proxy', None, 'not_established'))
        self.assertEqual(a['coverage'], 'partial')  # Codex scope partial + unattributed earlier attempts.
        self.assertIn('source_invocation_attribution_uncertain', r['evidence']['flags'])
        stage_total = sum(Decimal(w1[i]['estimated_amount']) for i in w1 if w1[i]['state'] in ('SPECIFY', 'IMPLEMENT')
                          and w1[i]['estimated_amount'])
        self.assertEqual(Decimal(h['stage_associated']['known_estimated_cost']), stage_total)
        self.assertEqual(Decimal(h['associated_but_unattributed']['known_estimated_cost']), stage_total - expected)
        tokens = {t['provider']: t for t in h['tokens']}  # 9-11: per provider, never mixed.
        self.assertEqual(tokens['codex']['tokens']['input']['reported_sum'], 24763)  # Codex input includes cached input.
        self.assertEqual(tokens['codex']['tokens']['cache_read']['reported_sum'], 24448)
        self.assertEqual(tokens['codex']['coverage_scope'], {'exec_thread_total_unverified': 1})
        self.assertIn('claude', tokens)

        new = r['actual_new_work']  # 26: the verifier ran in W2 and has no provider-token charge.
        self.assertEqual((new['known_estimated_cost'], new['coverage'], new['provider_invocations'], new['not_applicable']), ('0', 'complete', 0, 1))
        self.assertTrue(set(self.costs(self.w2)).isdisjoint(w1))  # Source cost never becomes new spending.

        # 15, 16: products and decisions naming the same invocation count it once.
        real = savings._load
        def duplicated(path):
            facts = real(path)
            if path == self.w2.resolve():
                adoption = json.loads(json.dumps(facts['adoption']))
                decision = next(d for d in adoption['continuity']['decisions'] if d['stage'] == 'SPECIFY')
                adoption['continuity']['decisions'].insert(2, json.loads(json.dumps(decision)))
                facts['adoption'] = adoption
            return facts
        with patch('switchyard.savings._load', side_effect=duplicated):
            twice = savings.report(self.w2)
        self.assertEqual((twice['avoided_regeneration']['known_estimated_cost'], twice['historical_source']['attributable_invocations']),
                         (a['known_estimated_cost'], h['attributable_invocations']))

        # 29, 30, 32, 34 (price stability): JSON decimal strings, stable; no catalog, process, network or Git.
        code, raw = self.cli(self.w2, '--json')
        payload = json.loads(raw, parse_float=lambda s: self.fail('float in JSON: %s' % s))
        self.assertEqual((code, payload['schema_version']), (0, 1))
        self.set_catalog(catalog(revision='test.9', sonnet_input='300'))
        with patch('switchyard.economics.load_registry', side_effect=AssertionError('no catalog')), \
                patch('subprocess.Popen', side_effect=AssertionError('no process')), \
                patch('socket.socket', side_effect=AssertionError('no network')):
            self.assertEqual(self.cli(self.w2, '--json')[1], raw)
            code, text = self.cli(self.w2, '--source-details', '--invocations')
            self.cli(self.work)
        for expected_text in ('adopted from work', 'Historical proxy:', 'Actual savings:      not established',
                              'Billed savings:      not claimed', 'Historical source expenditure (original work; not spent by this work):'):
            self.assertIn(expected_text, text)
        self.assertEqual(tree(self.work, self.w2), before)  # 31

        # 28: an ordinary work is "not applicable", never zero savings.
        plain = savings.report(self.work)
        self.assertEqual((plain['derived'], plain['avoided_regeneration']['coverage'], plain['avoided_regeneration']['known_estimated_cost']),
                         (False, 'not_applicable', None))
        self.assertIn('No continuity derivation recorded.', self.cli(self.work)[1])

        # 24: a damaged source pricing snapshot is excluded, never trusted.
        snapshot = self.work/'economics/pricing'/economics.file_name(impl['attributable'][0])
        original = snapshot.read_bytes()
        snapshot.write_bytes(original.replace(b'"input":"3"', b'"input":"0.3"'))
        damaged = savings.report(self.w2)
        self.assertLess(Decimal(damaged['avoided_regeneration']['known_estimated_cost']), Decimal(a['known_estimated_cost']))
        self.assertEqual(damaged['avoided_regeneration']['coverage'], 'partial')
        snapshot.write_bytes(original)

        # 23: a tampered source artifact is never followed.
        microspec = self.work/'artifacts/microspec'
        path = sorted(microspec.iterdir())[-1]; content = path.read_bytes()
        os.chmod(path, 0o644); path.write_bytes(content + b'\n# tampered\n')
        tampered = {d['stage']: d for d in savings.report(self.w2)['reuse']['decisions']}
        self.assertEqual(tampered['SPECIFY']['provenance']['reason'], 'source_artifact_identity_mismatch')
        self.assertEqual(tampered['SPECIFY']['action'], 'REUSED')  # The reuse fact itself stays visible.
        path.write_bytes(content)

        # 21: a source from before cost recording: usage may exist, cost is unavailable (never backfilled).
        shutil.move(str(self.work/'economics'), str(self.root/'economics-aside'))
        with patch('switchyard.economics._recording_runtime', return_value=False):
            legacy = savings.report(self.w2)
        self.assertEqual((legacy['avoided_regeneration']['known_estimated_cost'], legacy['avoided_regeneration']['available']), (None, False))
        self.assertIn('source_cost_unavailable', legacy['evidence']['flags'])
        shutil.move(str(self.root/'economics-aside'), str(self.work/'economics'))

        # 19, 20: the source directory is gone: reuse provenance stays, economics become unavailable.
        shutil.move(str(self.work), str(self.root/'w1-moved'))
        gone = savings.report(self.w2)
        self.assertEqual([d['action'] for d in gone['reuse']['decisions']], [d['action'] for d in r['reuse']['decisions']])
        self.assertEqual((gone['avoided_regeneration']['known_estimated_cost'], gone['avoided_regeneration']['reasons']),
                         (None, ['source_economics_unavailable', 'source_work_unavailable']))
        self.assertIn('source_economics_unavailable', gone['evidence']['flags'])
        self.assertEqual(gone['actual_new_work'], r['actual_new_work'])
        shutil.move(str(self.root/'w1-moved'), str(self.work))

        # 27: the derived work's own corrective round is new spending; a missing record of it is unknown, not zero.
        self.set_catalog(catalog())
        self.candidate_for(self.w2, self.d2, execution, 'qa2')
        qa_fix_v2_work(self.w2, 'Needs more.', self.text, self.priced_config())
        corrected = savings.report(self.w2)
        self.assertGreater(Decimal(corrected['actual_new_work']['known_estimated_cost']), 0)
        self.assertEqual(corrected['avoided_regeneration']['known_estimated_cost'], a['known_estimated_cost'])  # Historical unchanged.
        provider = next(i for i, c in self.costs(self.w2).items() if c['provider'] != 'command')
        (self.w2/'economics/costs'/economics.file_name(provider)).unlink()
        missing = savings.report(self.w2)['actual_new_work']
        self.assertEqual((missing['coverage'], missing['reasons'].get('not_recorded')), ('partial' if missing['known_estimated_cost'] else 'unavailable', 1))


class ChainTests(SavingsBase):
    # 2, 17, 18, 22, 25
    def test_transitive_chain_repeated_reuse_broken_lineage_and_blocked_transplant(self):
        exec_w1 = self.deliver(self.priced_config())
        self.candidate(exec_w1, 'qa1')  # W1's candidate; then W2 (same base) and W4 (repeated reuse of W1).
        exec_w2 = adopt_v2_work(self.work, self.w2, self.text, self.priced_config())
        self.candidate_for(self.w2, self.d2, exec_w2, 'qa2')
        w4 = self.root/'w4'
        adopt_v2_work(self.work, w4, self.text, self.priced_config())
        b = self.advance(write={'keep.txt': 'upstream\n'})  # Another base.
        w3 = self.root/'w3'
        transplant_v2_work(self.w2, w3, self.text, self.priced_config())
        before = tree(self.work, self.w2, w3, w4)

        r2, r3, r4 = savings.report(self.w2), savings.report(w3), savings.report(w4)
        d3 = {d['stage']: d for d in r3['reuse']['decisions']}
        self.assertEqual((r3['derivation']['intent'], r3['derivation']['base_changed'], r3['derivation']['target_base_sha']), ('transplant', True, b))
        self.assertEqual(r3['derivation']['transplant']['strategy'], 'git_merge_tree_three_way')
        self.assertEqual(r3['historical_source']['producers'], ['work'])  # 17: W1 is the original producer, not W2.
        self.assertEqual([h['work_id'] for h in d3['IMPLEMENT']['provenance']['hops']], ['w2', 'work'])
        self.assertTrue(all(h['verified'] for h in d3['IMPLEMENT']['provenance']['hops']))
        self.assertEqual(d3['IMPLEMENT']['provenance']['attributable'], [self.produced_by(self.work, 'implementation_handoff')])
        self.assertEqual(r3['avoided_regeneration']['known_estimated_cost'], r2['avoided_regeneration']['known_estimated_cost'])
        self.assertIn('transplanted_to_a_different_base', r3['avoided_regeneration']['caveats'])  # 25
        self.assertTrue(set(self.costs(w3)).isdisjoint(self.costs(self.work)))  # No W1 (or invented W2) spending in W3.
        self.assertEqual(r3['actual_new_work']['provider_invocations'], 0)
        self.assertEqual(r4['avoided_regeneration']['known_estimated_cost'], r2['avoided_regeneration']['known_estimated_cost'])  # 18
        self.assertIn('not_additive_across_derived_works', r4['avoided_regeneration']['caveats'])
        self.assertEqual(tree(self.work, self.w2, w3, w4), before)

        # 22: another work at the recorded source path is never accepted by name or location.
        shutil.move(str(self.work), str(self.root/'w1-aside'))
        shutil.copytree(w4, self.work, symlinks=True)
        broken = {d['stage']: d for d in savings.report(w3)['reuse']['decisions']}
        self.assertEqual([h['verified'] for h in broken['IMPLEMENT']['provenance']['hops']], [True, False])
        self.assertEqual(broken['IMPLEMENT']['provenance']['reason'], 'source_baseline_mismatch')
        shutil.rmtree(self.work); shutil.move(str(self.root/'w1-aside'), str(self.work))

        # 25: a conflicting transplant creates no derived work, so no reuse is ever claimed.
        self.git('reset', '-q', '--hard', self.base)
        self.advance(write={'source.py': 'conflicting upstream\n'})
        w5 = self.root/'w5'
        with self.assertRaises(RecoveryBlocked):
            transplant_v2_work(self.work, w5, self.text, self.priced_config())
        with self.assertRaises(savings.SavingsError):
            savings.report(w5)
        self.assertEqual(self.cli(w5)[0], 2)


if __name__ == '__main__':
    unittest.main()

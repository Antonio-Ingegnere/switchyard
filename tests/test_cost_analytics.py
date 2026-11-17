"""ECONOMICS-003 (#2): historical cost analytics across works, read only from frozen cost assessments. Real offline
deliveries (explicit TEST prices) on a controlled UTC wall clock, in real work stores under one declared root."""
import contextlib
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
import shutil
import unittest
from unittest.mock import patch

import runtime
from switchyard import analytics, economics
from switchyard.cli import main
from switchyard.diagnostics import Diagnostics
from switchyard.recovery import _operations, _store, accept_v2_candidate, qa_fix_v2_work, resume_v2_work

from test_economics import SPLIT, CostBase, catalog
from test_usage import LifecycleUsageTests

M = 'claude-sonnet-4-6'


def clock(year, month, day, hour=12, minute=0):
    """A controlled UTC wall clock: one second per sample (events of one drive stay within minutes)."""
    wall = int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()) * 10**9
    state = dict(wall=wall, mono=10**12)
    def sample(self):
        state['wall'] += 10**9; state['mono'] += 10**9
        return state['wall'], state['mono']
    return patch.object(runtime._Clock, 'sample', sample)


def tree(base):
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(base.rglob('*'))
            if p.is_file() and not p.is_symlink()}


class AnalyticsBase(CostBase):
    def setUp(self):
        super().setUp()
        self.works = self.root/'works'
        self.works.mkdir()

    def at(self, name):
        self.work = self.works/name; self.delivery = self.work/'workspace'
        return self.work

    def summary(self, **kw):
        return analytics.report(self.works, **kw)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['costs', 'summary', '--works-root', str(self.works), *args])
        return code, out.getvalue(), err.getvalue()

    def groups(self, **kw):
        return {g['key']: g for g in self.summary(**kw)['groups']}

    def per_work_known(self, work):
        """The ECONOMICS-004 per-work reader, independently: the same frozen amounts."""
        records = economics.read_costs(work)[0]
        return [Decimal(r['estimated_amount']) for r in records if r['estimated_amount'] is not None]

    def history(self):
        """alpha: Codex + Claude, READY_FOR_MANUAL_QA, Sunday 2026-11-29 (ISO 2026-W48).
        beta: a failed Claude invocation on Monday 2026-11-30 (2026-W49), INTERRUPTED, resumed on 2026-12-01, a QA
        round, then ACCEPTED. gamma: Claude without a configured price on 2027-01-01 (ISO 2026-W53, month 2027-01)."""
        self.at('alpha')
        with clock(2026, 11, 29, 23, 0):
            self.deliver(self.priced_config())
        self.at('beta')
        failed = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True, usage=dict(SPLIT, input_tokens=100),
                                 modelUsage={M: dict(inputTokens=100, outputTokens=340, cacheReadInputTokens=8000,
                                                     cacheCreationInputTokens=500)})).encode()
        with clock(2026, 11, 30, 0, 30):
            self.deliver(self.claude_emits(failed, exit_code=1), fails=True)
        self.interrupted = self.summary(group_by='status')
        with clock(2026, 12, 1, 9):
            execution = resume_v2_work(self.work, self.text, self.priced_config())
        self.candidate(execution, 'qa-beta-1')
        with clock(2026, 12, 2, 9):
            execution = qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.priced_config())
        self.candidate(execution, 'qa-beta-2')
        accept_v2_candidate(self.work)
        self.at('gamma')
        self.set_catalog(dict(catalog(), entries=[e for e in catalog()['entries'] if e['provider'] == 'codex']))
        with clock(2027, 1, 1, 10):
            self.deliver(self.priced_config())
        self.set_catalog(catalog())


class HistoryTests(AnalyticsBase):
    # 1-9, 11-14, 16-22, 24, 25, 29-36
    def test_history_across_works_periods_dimensions_lifecycle_and_read_only(self):
        self.history()
        (self.works/'notes').mkdir(); (self.works/'notes/readme.txt').write_text('not a work')
        (self.works/'qa-store').mkdir(); (self.works/'qa-store/manifest.jsonl').write_text('{}\n')  # A workspace, not a work.
        os.symlink(self.works/'alpha', self.works/'zz-link')
        (self.works/'loose.json').write_text('{}')
        before = tree(self.works)

        r = self.summary()
        self.assertEqual([w for w in analytics.discover(self.works)[0]], [self.works.resolve()/n for n in ('alpha', 'beta', 'gamma')])  # 1, 2
        self.assertEqual((r['scope']['symlinks_skipped'], r['scope']['non_work_entries'], r['coverage']['works_observed']), (1, 3, 3))  # 3
        expected = sum((sum(self.per_work_known(self.works/n), Decimal(0)) for n in ('alpha', 'beta', 'gamma')), Decimal(0))
        self.assertEqual(Decimal(r['known_estimated_subtotal']), expected)  # 4, 29: exact, unrounded.
        self.assertFalse(r['coverage']['complete'])  # 25: Codex scope partial, gamma unpriced.
        self.assertGreaterEqual(r['coverage']['price_unavailable'], 1)
        self.assertEqual(r['billed_cost'], None)

        days = self.groups(group_by='day')  # 5, 8: UTC calendar dates of the invocations' own end events.
        self.assertEqual(sorted(days), ['2026-11-29', '2026-11-30', '2026-12-01', '2026-12-02', '2027-01-01'])
        self.assertEqual(days['2026-11-29']['known_estimated_subtotal'], '0.01809475')  # alpha: 0.00466975 + 0.013425
        weeks = self.groups(group_by='week')  # 6: Sunday closes W48; Monday opens W49; 2027-01-01 is ISO 2026-W53.
        self.assertEqual(sorted(weeks), ['2026-W48', '2026-W49', '2026-W53'])
        self.assertEqual(weeks['2026-W48']['known_estimated_subtotal'], '0.01809475')
        months = self.groups(group_by='month')  # 7
        self.assertEqual(sorted(months), ['2026-11', '2026-12', '2027-01'])
        self.assertEqual([g['key'] for g in self.summary(group_by='week')['groups']], ['2026-W48', '2026-W49', '2026-W53'])  # Chronological.
        nov30 = self.summary(date_from='2026-11-30', date_to='2026-11-30')  # 9: inclusive calendar date, half-open internally.
        self.assertEqual((nov30['period']['interval'], nov30['coverage']['provider_invocations']),
                         (dict(start='2026-11-30T00:00:00Z', end_exclusive='2026-12-01T00:00:00Z'), 2))  # Codex + the failed Claude.
        self.assertEqual(self.summary(date_from='2026-11-29', date_to='2026-11-29')['known_estimated_subtotal'], '0.01809475')
        self.assertEqual(self.summary(date_from='2027-02-01')['coverage']['provider_invocations'], 0)
        for reconcile in ('day', 'week', 'month', 'work', 'state', 'agent', 'provider', 'model', 'status'):  # 30
            self.assertTrue(self.summary(group_by=reconcile)['reconciliation']['reconciles_with_subtotal'], reconcile)

        states = self.groups(group_by='state')  # 11
        self.assertEqual(states['VERIFY']['provider_invocations'], 0)
        self.assertGreater(states['VERIFY']['not_applicable'], 0)  # Command verifiers: no provider-token charge.
        self.assertEqual(states['SPECIFY']['known_estimated_subtotal'], '0.0093395')  # alpha, gamma (beta's first Codex reported no usage).
        self.assertEqual(set(self.groups(group_by='provider')), {'claude', 'codex', 'command'})  # 13
        self.assertTrue(set(self.groups(group_by='agent')))  # 12: recorded resolved agents.
        models = self.groups(group_by='model')  # 14
        self.assertEqual(models['offline']['known_estimated_subtotal'], '0.0093395')  # alpha, gamma (beta's first Codex reported no usage).
        claude_lines = [l['amount'] for n in ('alpha', 'beta', 'gamma') for c in economics.read_costs(self.works/n)[0] for l in c['lines']
                        if l['model'] == M]
        self.assertEqual(Decimal(models[M]['known_estimated_subtotal']), sum(map(Decimal, claude_lines)))  # Each line once.
        self.assertGreater(models[M]['price_unavailable'], 0)  # gamma's unpriced Claude model stays visible in its group.

        works = self.summary(group_by='work')  # 16, 31
        self.assertEqual([g['key'] for g in works['groups']][:2], ['beta', 'alpha'])  # Cost descending.
        top = self.summary(group_by='work', last=1)
        self.assertEqual(([g['key'] for g in top['groups']], top['ranking']), (['beta'], 'top_work_by_known_estimated_spend'))
        rec = top['reconciliation']  # A top-N view is not an integrity failure: the full accounting still reconciles.
        self.assertTrue(rec['reconciles_with_subtotal'])
        self.assertEqual((top['known_estimated_subtotal'], rec['groups_known_sum'], rec['displayed_groups_known_sum']),
                         (works['known_estimated_subtotal'], works['known_estimated_subtotal'], works['groups'][0]['known_estimated_subtotal']))
        self.assertEqual(Decimal(rec['displayed_groups_known_sum']) + Decimal(rec['omitted_groups_known_sum']), Decimal(top['known_estimated_subtotal']))
        self.assertEqual((rec['groups_total'], rec['groups_displayed'], rec['groups_omitted']), (3, 1, 2))
        recent = self.summary(group_by='week', last=1)['reconciliation']  # Time view: the most recent period only.
        self.assertTrue(recent['reconciles_with_subtotal'])
        self.assertEqual((recent['displayed_groups_known_sum'], recent['groups_omitted']), (weeks['2026-W53']['known_estimated_subtotal'], 2))
        self.assertEqual({g['key']: g['status'] for g in works['groups']},
                         dict(alpha='READY_FOR_MANUAL_QA', beta='ACCEPTED', gamma='READY_FOR_MANUAL_QA'))
        self.assertEqual(sum(Decimal(g['known_estimated_subtotal']) for g in works['groups'] if g['known_estimated_subtotal']),
                         Decimal(r['known_estimated_subtotal']))  # 30: sum(per-work) == global.
        status = self.groups(group_by='status')  # 17
        self.assertEqual(set(status), {'ACCEPTED', 'READY_FOR_MANUAL_QA'})

        # 18-21: the failed invocation's spend stays in the ACCEPTED work, once; each retry is its own invocation.
        beta = [g for g in works['groups'] if g['key'] == 'beta'][0]
        beta_started = [e for e in self.canonical(self.works/'beta') if e.kind == 'invocation_started']
        beta_costs = economics.read_costs(self.works/'beta')[0]
        self.assertEqual(beta['provider_invocations'] + beta['not_applicable'], len(beta_started))
        self.assertEqual(Decimal(beta['known_estimated_subtotal']), sum(self.per_work_known(self.works/'beta'), Decimal(0)))
        outcomes = {o['outcome']: o for o in self.summary(status='ACCEPTED')['invocation_outcomes']}
        self.assertEqual(outcomes['failed']['provider_invocations'], 1)
        self.assertEqual(outcomes['failed']['known_estimated_subtotal'],
                         [c for c in beta_costs if c['provider'] == 'claude'][0]['estimated_amount'])
        self.assertEqual({o['outcome'] for o in self.interrupted['delivery_outcomes']}, {'awaiting_manual_qa', 'interrupted'})  # 19
        before_failure = {o['outcome']: o['known_estimated_subtotal'] for o in self.interrupted['delivery_outcomes']}
        self.assertEqual(before_failure['interrupted'], outcomes['failed']['known_estimated_subtotal'])  # Same frozen amount later.
        self.assertGreaterEqual(len({c['state_visit_id'] for c in beta_costs if c['state'] == 'IMPLEMENT'}), 2)  # 20: QA round.

        # 22: a later recovery pass writes nothing new and nothing is counted twice.
        self.work = self.works/'beta'
        writer = Diagnostics(self.work, 'beta'); writer.bind(LifecycleUsageTests.compiled(self))
        writer.backfill(self.canonical(), _operations(_store(self.work)))
        self.assertEqual(self.summary(group_by='work')['groups'], works['groups'])
        before = tree(self.works)

        # 32: JSON: deterministic, decimal strings only, schema 1.
        code, raw, _ = self.cli('--json', '--group-by', 'week')
        payload = json.loads(raw, parse_float=lambda s: self.fail('float in JSON: %s' % s))
        self.assertEqual((code, payload['schema_version'], payload['currency'], payload['period']['timezone']), (0, 1, 'USD', 'UTC'))
        self.assertEqual(self.cli('--json', '--group-by', 'week')[1], raw)
        code, text, _ = self.cli()
        for expected_text in ('Known estimated subtotal:', 'Complete spend coverage:  no', 'Billed spend:             unavailable',
                              'Coverage: PARTIAL', 'Known subtotal is not proven total spend.', 'Command verifiers:'):
            self.assertIn(expected_text, text)
        self.assertNotIn('Total cost', text)
        top_text = self.cli('--group-by', 'work', '--last', '1')[1]
        self.assertIn('Top 1 works by known estimated spend', top_text)
        self.assertIn('(2 more group(s) not shown:', top_text)

        # 34: a changed current catalog cannot change the report; 35, 36: read-only, no provider/Git/network/pricing.
        self.set_catalog(catalog(revision='test.9', sonnet_input='300'))
        with patch('switchyard.economics.load_registry', side_effect=AssertionError('no catalog')), \
                patch('subprocess.Popen', side_effect=AssertionError('no process')), \
                patch('socket.socket', side_effect=AssertionError('no network')), \
                patch('switchyard.recovery._lock', side_effect=AssertionError('no lock')):
            self.assertEqual(json.loads(self.cli('--json', '--group-by', 'week')[1]), payload)
            self.assertEqual({r['catalog_revision'] for r in payload['pricing_revisions']}, {'test.1'})  # Frozen provenance.
            for dimension in analytics.DIMENSIONS:
                self.cli('--group-by', dimension)
        self.assertEqual(tree(self.works), before)
        self.assertFalse(any((self.works/n/'analytics').exists() for n in ('alpha', 'beta', 'gamma')))


class IntegrityTests(AnalyticsBase):
    # 10, 15, 23, 26, 27, 28, 33
    def test_damaged_duplicated_legacy_and_undated_records_stay_visible_and_excluded(self):
        self.at('alpha')
        with clock(2026, 11, 29, 9):
            self.deliver(self.priced_config())
        good = self.summary()
        self.assertEqual(good['known_estimated_subtotal'], '0.01809475')

        shutil.copytree(self.works/'alpha', self.works/'alpha-copy', symlinks=True)  # Same durable identities twice.
        dup = self.summary()
        self.assertEqual({i['code'] for i in dup['issues']}, {'duplicate_work_identity', 'conflicting_invocation_identity'})
        self.assertIsNone(dup['known_estimated_subtotal'])  # Neither copy is chosen; nothing is doubled.
        self.assertEqual(dup['coverage']['conflicting'], 4)
        shutil.rmtree(self.works/'alpha-copy')

        claude = [r for r in economics.read_costs(self.works/'alpha')[0] if r['provider'] == 'claude'][0]
        pricing = self.works/'alpha/economics/pricing'/economics.file_name(claude['invocation_id'])
        original = pricing.read_bytes()
        os.chmod(pricing, 0o644); pricing.write_bytes(original.replace(b'"input":"3"', b'"input":"0.3"'))  # 27
        bad = self.summary()
        self.assertEqual([i['code'] for i in bad['issues']], ['pricing_snapshot_altered_or_mismatched'])
        self.assertEqual((bad['known_estimated_subtotal'], bad['coverage']['invalid'], bad['coverage']['complete']), ('0.00466975', 1, False))
        pricing.write_bytes(original)
        cost = self.works/'alpha/economics/costs'/economics.file_name(claude['invocation_id'])
        record = cost.read_bytes()
        cost.write_bytes(record.replace(b'"estimated_amount":"0.013425"', b'"estimated_amount":"0.014"'))
        self.assertEqual([i['code'] for i in self.summary()['issues']], ['cost_amount_inconsistent'])
        cost.write_text(json.dumps(dict(json.loads(record), schema_version=2)))  # 28: an unsupported future schema.
        future = self.summary()
        self.assertEqual(([i['code'] for i in future['issues']], future['known_estimated_subtotal']), (['unsupported_cost_schema'], '0.00466975'))
        cost.unlink()  # 26: a missing record is unknown, never zero.
        missing = self.summary()
        self.assertEqual((missing['coverage']['not_recorded'], missing['known_estimated_subtotal'], missing['issues']), (1, '0.00466975', []))
        with patch('switchyard.economics._recording_runtime', return_value=False):
            shutil.move(str(self.works/'alpha/economics'), str(self.root/'economics-aside'))
            legacy = self.summary()  # 17 (legacy): usage but no pricing.
            self.assertEqual((legacy['coverage']['pricing_not_captured'], legacy['known_estimated_subtotal']), (2, None))
            shutil.move(str(self.root/'economics-aside'), str(self.works/'alpha/economics'))
        cost.write_bytes(record)

        # 15: a charge line without an identified model goes to `unallocated`, never to the configured model.
        unknown = dict(json.loads(record)); line = dict(unknown['lines'][0], model=None)
        self.assertEqual(analytics._models(dict(lines=[line], unpriced=[])), ['unallocated'])
        self.assertEqual(analytics._amount(dict(lines=[line]), 'unallocated'), line['amount'])

        # 10: an undated invocation counts all-time, in no period, and blocks dated completeness.
        events = self.works/'alpha/events.jsonl'
        lines = [json.loads(l) for l in events.read_text().splitlines()]
        events.write_text(''.join(json.dumps(dict(l, occurred_at_unix_ns=None, duration_ns=None) if l['kind'] in ('invocation_finished', 'run_error')
                                             else l) + '\n' for l in lines))
        undated = self.summary(group_by='day')
        self.assertEqual((undated['known_estimated_subtotal'], undated['groups'], undated['coverage']['date_unknown']), ('0.01809475', [], 2))
        self.assertEqual(undated['reconciliation']['undated_known_sum'], '0.01809475')
        self.assertTrue(undated['reconciliation']['reconciles_with_subtotal'])
        period = self.summary(date_from='2026-11-01', date_to='2026-11-30')
        self.assertEqual((period['known_estimated_subtotal'], period['coverage']['complete'], period['date_unknown']['included_in_subtotal']),
                         (None, False, False))

        # 33: no work in scope, an unreadable log: unknown stays unknown.
        events.write_text('not json\n')
        broken = self.summary()
        self.assertEqual(([i['code'] for i in broken['issues']], broken['known_estimated_subtotal'], broken['coverage']['complete']),
                         (['events_unreadable'], None, False))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['costs', 'summary', '--works-root', str(self.works/'alpha')]), 2)  # A work root is not a works root.
            self.assertEqual(main(['costs', 'summary']), 2)  # The scope must be declared.


class AdoptionTests(AnalyticsBase):
    # 23
    def test_an_adopted_product_is_not_a_new_billed_invocation(self):
        self.at('w1')
        with clock(2026, 11, 20):
            execution = self.deliver(self.priced_config())
        self.candidate(execution)
        self.w2 = self.works/'w2'; self.d2 = self.w2/'workspace'
        with clock(2026, 11, 21):
            self.adopt(self.priced_config())
        r = self.summary(group_by='work')
        by_work = {g['key']: g for g in r['groups']}
        self.assertEqual((by_work['w2']['provider_invocations'], by_work['w2']['known_estimated_subtotal']), (0, '0'))
        self.assertEqual(by_work['w1']['known_estimated_subtotal'], '0.01809475')
        self.assertEqual(r['known_estimated_subtotal'], '0.01809475')  # W1's spend once; W2 ran only its verifier.
        ids = [set(c['invocation_id'] for c in economics.read_costs(w)[0]) for w in (self.works/'w1', self.w2)]
        self.assertTrue(ids[0].isdisjoint(ids[1]))


if __name__ == '__main__':
    unittest.main()

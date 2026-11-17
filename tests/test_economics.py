"""ECONOMICS-004 (#2): versioned pricing registry, frozen pricing snapshots and cost assessments. Pure assessment on
documented usage shapes, then real offline deliveries with explicitly defined TEST prices (never real rates)."""
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import os
import shutil
import unittest
from unittest.mock import patch

from switchyard import economics
from switchyard.cli import main
from switchyard.diagnostics import Diagnostics
from switchyard.economics import CatalogError, Registry, aggregate, assess, freeze, read_costs
from switchyard.provider_stages import claude_usage
from switchyard.recovery import _operations, _store, qa_fix_v2_work, resume_v2_work

from test_provider_diagnostics import SECRET
from test_usage import MODEL, USAGE, LifecycleUsageTests, UsageBase, envelope

DAY = '2026-11-13'
SPLIT = dict(USAGE, cache_creation=dict(ephemeral_5m_input_tokens=300, ephemeral_1h_input_tokens=200))
CODEX = dict(input_tokens=24763, cached_input_tokens=24448, cache_write_input_tokens=0, output_tokens=122, reasoning_output_tokens=64)


def catalog(revision='test.1', sonnet_input='3', entries=()):
    """Explicit TEST prices (per million tokens); not provider rates."""
    rates = dict(input=sonnet_input, cache_write_5m='3.75', cache_write_1h='6', cache_read='0.30', output='15')
    return dict(catalog='test', schema_version=1, revision=revision, currency='USD', unit='per_million_tokens',
                defaults=dict(effective_from='2026-01-01', verified=DAY, sources=dict(claude='https://example.test/claude',
                                                                                       codex='https://example.test/codex'),
                              basis=dict(claude='test_basis', codex='test_basis')),
                entries=[dict(provider='claude', model='claude-sonnet-4-6', pricing='flat', rates=rates),
                         dict(provider='claude', model='claude-haiku-test', pricing='flat',
                              rates=dict(input='1', cache_write_5m='1.25', cache_write_1h='2', cache_read='0.10', output='5')),
                         dict(provider='codex', model='offline', pricing='flat', cache_write='not_billed',
                              rates=dict(input='1.25', cache_read='0.125', output='10')), *entries])


def registry(*documents):
    return Registry([('builtin', economics.builtin_catalog())] + [('user_configuration', d) for d in documents or (catalog(),)])


def claude_record(raw, **extra):
    """A usage record shape (what usage.build keeps) plus the in-memory pricing detail."""
    u = claude_usage(raw)
    return dict(invocation_id='run:r:invocation:1', provider='claude', source=u['source'], tokens=u.get('tokens'),
                coverage_scope=u['semantics']['coverage_scope'] if u.get('semantics') else None,
                model_allocation=u.get('model_allocation'), reported_model=u.get('reported_model'),
                model_breakdown=u.get('model_breakdown') or [], model_breakdown_total=u.get('model_breakdown_total'),
                resolved_model='sonnet', configured_model='sonnet', _pricing_detail=u.get('pricing_detail'), **extra)


def codex_record(**tokens):
    base = dict(input=24763, output=122, cache_read=24448, cache_write=0)
    return dict(invocation_id='run:r:invocation:2', provider='codex', source='provider_reported', tokens=dict(base, **tokens),
                coverage_scope='exec_thread_total_unverified', model_allocation='not_reported', reported_model=None,
                model_breakdown=[], resolved_model='offline', configured_model='offline')


def priced(usage, reg=None, day=DAY):
    reg = reg or registry()
    return assess('w', usage, freeze('w', usage['invocation_id'], usage['provider'], economics.quantities(usage), day, reg), 'runtime')


def amounts(cost):
    return {(l['model'], l['category']): l['amount'] for l in cost['lines']}


class AssessmentTests(unittest.TestCase):
    # 1, 2, 13, 14
    def test_claude_categories_cache_split_exact_decimals_and_rounding(self):
        cost = priced(claude_record(envelope(usage=SPLIT, modelUsage=MODEL)))
        self.assertEqual((cost['status'], cost['cost_scope'], cost['reasons']), ('complete', 'invocation_wide_reconciled', []))
        m = 'claude-sonnet-4-6'
        self.assertEqual(amounts(cost), {(m, 'input'): '0.0036', (m, 'cache_read'): '0.0024', (m, 'output'): '0.0051',
                                         (m, 'cache_write_5m'): '0.001125', (m, 'cache_write_1h'): '0.0012'})
        self.assertEqual(cost['estimated_amount'], '0.013425')  # Exact; 1200*3 + 8000*0.30 + 340*15 + 300*3.75 + 200*6, /1e6.
        self.assertEqual(sum(Decimal(v) for v in amounts(cost).values()), Decimal(cost['estimated_amount']))
        no_split = priced(claude_record(envelope(usage=USAGE, modelUsage=MODEL)))
        self.assertEqual((no_split['status'], no_split['unpriced'][0]['reason'], no_split['estimated_amount']),
                         ('partial', 'cache_write_duration_not_reported', '0.0111'))  # Never guessed as 5m or 1h.
        tiny = [dict(status='complete', estimated_amount='0.00004', reasons=[], usage_source='provider_reported',
                     cost_scope='invocation_wide_reconciled')] * 3
        self.assertEqual(economics.summarize(tiny)['estimated_reported_usage_cost'], '0.00012')  # Unrounded aggregation.
        self.assertEqual([economics.display(x) for x in ('0.00004', '0.00012', '0.00025', '0.00035', '0', None)],
                         ['<$0.0001', '$0.0001', '$0.0002', '$0.0004', '$0.0000', '—'])  # Half-even, display only.

    # 3, 4, 5, 12
    def test_claude_whole_tree_by_model_never_adds_top_level_and_incomplete_scope_stays_visible(self):
        tree = dict(MODEL, **{'claude-haiku-test': dict(inputTokens=3000, outputTokens=600, cacheReadInputTokens=0, cacheCreationInputTokens=0)})
        cost = priced(claude_record(envelope(usage=SPLIT, modelUsage=tree)))
        self.assertEqual(cost['cost_scope'], 'agent_tree_by_model')  # Each model at its own rate.
        self.assertEqual(amounts(cost)[('claude-haiku-test', 'input')], '0.003')  # Helper tokens never at the primary's rate.
        self.assertEqual(amounts(cost)[('claude-sonnet-4-6', 'input')], '0.0036')  # Sonnet once: top-level never added again.
        self.assertEqual([(u['model'], u['category'], u['reason']) for u in cost['unpriced']],
                         [('claude-sonnet-4-6', 'cache_write', 'cache_write_duration_not_reported')])  # modelUsage has no TTL split.
        self.assertEqual(cost['status'], 'partial')
        unknown_helper = dict(MODEL, **{'claude-mystery-9': dict(inputTokens=10, outputTokens=1, cacheReadInputTokens=0, cacheCreationInputTokens=0)})
        u = priced(claude_record(envelope(usage=SPLIT, modelUsage=unknown_helper)))
        self.assertIn(('claude-mystery-9', 'input', 'price_not_configured'), [(x['model'], x['category'], x['reason']) for x in u['unpriced']])
        top = priced(claude_record(envelope(usage=SPLIT)))  # No breakdown: requested alias 'sonnet' cannot be priced.
        self.assertEqual((top['cost_scope'], top['status'], top['reasons']),
                         ('top_level_agent_loop_unverified', 'unavailable', ['price_not_configured']))
        exact = priced(dict(claude_record(envelope(usage=SPLIT)), resolved_model='claude-sonnet-4-6'))
        self.assertEqual((exact['status'], exact['reasons'], exact['assumptions'][-1]),
                         ('partial', ['scope_not_invocation_wide'], 'model_not_reported_requested_model_used'))
        below = dict(MODEL, **{'claude-sonnet-4-6': dict(inputTokens=5, outputTokens=340, cacheReadInputTokens=8000, cacheCreationInputTokens=500)})
        self.assertEqual(priced(claude_record(envelope(usage=SPLIT, modelUsage=below)))['reasons'], ['model_breakdown_below_usage'])
        fast = priced(claude_record(envelope(usage=dict(SPLIT, speed='fast'), modelUsage=MODEL)))
        self.assertEqual((fast['status'], fast['reasons']), ('unavailable', ['pricing_modifier_outside_catalog_basis']))

    # 6, 7, 8
    def test_codex_uncached_subtraction_reasoning_inside_output_and_invalid_counters(self):
        cost = priced(codex_record())
        self.assertEqual(amounts(cost), {('offline', 'input'): '0.00039375', ('offline', 'cache_read'): '0.003056',
                                         ('offline', 'output'): '0.00122'})  # (24763-24448)*1.25, 24448*0.125, 122*10; /1e6.
        self.assertEqual([l['tokens'] for l in cost['lines']], [315, 24448, 122])  # Reasoning (64) is inside the 122.
        self.assertEqual((cost['status'], cost['reasons']), ('partial', ['scope_not_invocation_wide']))
        bad = priced(codex_record(cache_read=30000))
        self.assertEqual(bad['unpriced'][0]['reason'], 'cached_input_exceeds_input')
        self.assertNotIn(('offline', 'cache_read'), amounts(bad))
        self.assertEqual(priced(codex_record(cache_read=None))['unpriced'][0]['reason'], 'input_split_not_reported')

    # 9, 10, 15
    def test_registry_lookup_versions_windows_overrides_and_rejections(self):
        reg = registry()
        self.assertEqual(reg.select('claude', 'claude-haiku-5-5', DAY)[1], 'tiered_pricing_unresolvable')  # Built-in, per-request tiers.
        self.assertEqual(reg.select('claude', 'sonnet', DAY)[1], 'price_not_configured')  # Aliases never resolved.
        self.assertEqual(reg.select('codex', 'gpt-5.3-codex', DAY)[0]['catalog_origin'], 'builtin')
        self.assertEqual(reg.select('claude', 'claude-sonnet-4-6', '2025-12-31')[1], 'price_not_effective')
        windows = catalog(entries=[dict(provider='codex', model='gpt-test', pricing='flat', cache_write='not_billed', effective_until='2026-06-01',
                                        rates=dict(input='1', cache_read='0.1', output='2')),
                                   dict(provider='codex', model='gpt-test', pricing='flat', cache_write='not_billed', effective_from='2026-06-01',
                                        rates=dict(input='2', cache_read='0.2', output='4'))])
        reg = registry(windows)
        self.assertEqual([reg.select('codex', 'gpt-test', d)[0]['rates']['input'] for d in ('2026-05-31', '2026-06-01')], ['1', '2'])
        override = catalog(entries=[dict(provider='codex', model='gpt-5.3-codex', pricing='flat', cache_write='not_billed',
                                         rates=dict(input='9', cache_read='0.9', output='9'))])
        self.assertEqual(registry(override).select('codex', 'gpt-5.3-codex', DAY)[0]['catalog_origin'], 'user_configuration')
        for broken in (dict(catalog(), entries=[dict(catalog()['entries'][0], rates=dict(catalog()['entries'][0]['rates'], input=3.0))]),
                       dict(catalog(), entries=[dict(catalog()['entries'][0], rates=dict(input='1'))]),
                       dict(catalog(), entries=catalog()['entries'][:1] * 2), dict(catalog(), currency='EUR'),
                       dict(catalog(), entries=[dict(catalog()['entries'][0], source='http://insecure')])):
            with self.assertRaises(CatalogError):
                registry(broken)
        self.assertEqual(priced(dict(codex_record(), source='unavailable', reason='no_jsonl_events'))['reasons'], ['usage_unavailable'])
        self.assertEqual(assess('w', codex_record(), None, 'runtime')['reasons'], ['pricing_not_captured'])  # No snapshot: no price.
        self.assertEqual(economics.plan(dict(provider='command', source='not_applicable'))[3], 'not_applicable')


class CostBase(UsageBase):
    def setUp(self):
        super().setUp()
        self.catalog = self.root/'pricing.json'
        self.set_catalog(catalog())
        env = patch.dict(os.environ, {economics.CATALOG_ENV: str(self.catalog)})
        env.start(); self.addCleanup(env.stop)

    def set_catalog(self, document):
        self.catalog.write_text(json.dumps(document))

    def priced_config(self, config=None, **claude):
        config = self.with_usage(config, **({'usage': SPLIT} | claude))
        return replace(config, environment={**config.environment, 'FAKE_CODEX_USAGE': json.dumps(CODEX)})

    def costs(self, **filters):
        return read_costs(self.work, **filters)[0]

    def economics_bytes(self):
        d = self.work/'economics'
        return self.files(d) if d.exists() else {}

    def run_costs(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['work', 'costs', str(self.work), *args])
        return code, out.getvalue()


class DeliveryCostTests(CostBase):
    # 1, 6, 16, 19, 20, 22, 23, 24
    def test_a_delivery_freezes_pricing_and_assesses_each_invocation_once(self):
        execution = self.deliver(self.priced_config())
        self.assertEqual((execution.result.terminal_state, len(self.calls())), ('READY_FOR_MANUAL_QA', 2))  # 22, 23
        records = self.costs()
        self.assertEqual([(r['state'], r['status'], r['estimated_amount']) for r in records],
                         [('SPECIFY', 'partial', '0.00466975'), ('IMPLEMENT', 'complete', '0.013425'), ('VERIFY', 'not_applicable', None)])
        snapshot = economics.read_snapshot(self.work, records[1]['invocation_id'])
        self.assertEqual((records[1]['pricing_snapshot_id'], records[1]['snapshot_integrity']), (snapshot['snapshot_id'], 'verified'))
        self.assertEqual((snapshot['priced_on'], snapshot['entries'][0]['rates']['input'], snapshot['entries'][0]['catalog_revision'],
                          snapshot['registry']['catalogs'][1]['origin']),
                         (datetime.now(timezone.utc).strftime('%Y-%m-%d'), '3', 'test.1', 'user_configuration'))
        totals = aggregate(records)['totals']
        self.assertEqual((totals['estimated_reported_usage_cost'], totals['provider_invocations'], totals['priced'], totals['complete'],
                          totals['partial'], totals['scope_incomplete'], totals['invocation_wide_proven'], totals['not_applicable']),
                         ('0.01809475', 2, 2, 1, 1, 1, False, 1))
        code, text = self.run_costs()
        for expected in ('Estimated reported-usage cost: $0.0181  (known subtotal; not a proven total)', 'Invocation-wide coverage:      partial',
                         'Billed cost:                   unavailable', 'Not applicable:                1 (command verifiers)'):
            self.assertIn(expected, text)
        self.assertIn('$0.0134', text); self.assertIn('partial (scope_not_invocation_wide)', text)
        raw = self.run_costs('--json')[1]
        payload = json.loads(raw, parse_float=lambda s: self.fail('float in JSON: %s' % s))  # 20: decimal strings only.
        self.assertEqual((payload['totals']['estimated_reported_usage_cost'], payload['records'][1]['lines'][0]['rate']), ('0.01809475', '3'))
        self.assertEqual(json.loads(self.run_costs('--provider', 'codex', '--json')[1])['totals']['estimated_reported_usage_cost'], '0.00466975')
        before = self.files(self.work)
        with patch('switchyard.economics.load_registry', side_effect=AssertionError('read must not load prices')):
            self.run_costs(); self.run_costs('--json'); self.run_costs('--state', 'IMPLEMENT')
        self.assertEqual(self.files(self.work), before)  # 19: read-only.
        data = b''.join(self.economics_bytes().values())
        for leaked in (SECRET, 'sess-', str(self.catalog), 'Change source.py', 'structured_output', '0.5'):
            self.assertNotIn(leaked.encode(), data, leaked)
        usage = b''.join(self.usage_bytes().values())
        self.assertNotIn(b'pricing_detail', usage); self.assertNotIn(b'ephemeral_', usage)  # 24: usage records unchanged.
        self.assert_consistent()

    # 17, 15: the historical price-change test
    def test_a_catalog_change_never_alters_an_earlier_assessment(self):
        execution = self.deliver(self.priced_config())
        frozen = self.economics_bytes()
        first = {r['invocation_id']: r for r in self.costs()}
        self.set_catalog(catalog(revision='test.2', sonnet_input='30'))  # The current catalog changes afterwards.
        self.candidate(execution)
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.priced_config())
        self.assertTrue(all(self.economics_bytes()[k] == v for k, v in frozen.items()))  # Byte-identical history.
        later = [r for r in self.costs() if r['invocation_id'] not in first and r['provider'] == 'claude']
        self.assertEqual(amounts(later[0])[('claude-sonnet-4-6', 'input')], '0.036')  # New invocation: new revision.
        old = [r for r in self.costs() if r['invocation_id'] in first and r['provider'] == 'claude'][0]
        self.assertEqual(amounts(old)[('claude-sonnet-4-6', 'input')], '0.0036')
        revisions = {economics.read_snapshot(self.work, r['invocation_id'])['entries'][0]['catalog_revision'] for r in (old, later[0])}
        self.assertEqual(revisions, {'test.1', 'test.2'})

    # 11, 18, 23
    def test_failures_retries_crashes_and_restarts_never_rewrite_or_reprice(self):
        failed = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True, usage=dict(SPLIT, input_tokens=100),
                                 modelUsage={'claude-sonnet-4-6': dict(inputTokens=100, outputTokens=340, cacheReadInputTokens=8000,
                                                                       cacheCreationInputTokens=500)})).encode()
        self.deliver(self.claude_emits(failed, exit_code=1), fails=True)
        failure = self.costs(state='IMPLEMENT')[0]
        self.assertEqual((failure['status'], amounts(failure)[('claude-sonnet-4-6', 'input')]), ('complete', '0.0003'))  # 11
        with patch('switchyard.economics._write', side_effect=OSError('disk full')):  # Crash before pricing publication.
            execution = resume_v2_work(self.work, self.text, self.priced_config())
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')  # 23: no impact on delivery.
        self.assertEqual({r['reasons'][0] for r in self.costs() if not r['recorded'] and r['provider'] != 'command'}, {'not_recorded'})
        before = self.economics_bytes()
        self.candidate(execution)
        writer = Diagnostics(self.work, 'work'); writer.bind(LifecycleUsageTests.compiled(self))
        writer.backfill(self.canonical(), _operations(_store(self.work)))  # The restart's recovery.
        backfilled = [r for r in self.costs() if r['observed_by'] == 'recovery_backfill']
        self.assertTrue(backfilled)
        self.assertEqual({r['reasons'][0] for r in backfilled if r['provider'] != 'command'}, {'pricing_not_captured'})  # Never today's price.
        self.assertTrue(all(self.economics_bytes()[k] == v for k, v in before.items()))
        frozen = self.economics_bytes()
        writer.backfill(self.canonical(), _operations(_store(self.work)))
        self.assertEqual(self.economics_bytes(), frozen)  # 18: idempotent.
        implement = self.costs(state='IMPLEMENT')
        self.assertEqual(len({r['invocation_id'] for r in implement}), 2)  # The retry is a separate assessment.

    def test_a_crash_after_the_snapshot_recovers_exactly_the_uninterrupted_cost(self):
        assessed = ('cost_id', 'cost_scope', 'status', 'reasons', 'assumptions', 'lines', 'unpriced', 'estimated_amount',
                    'pricing_snapshot_id', 'usage_id', 'currency', 'unit', 'arithmetic')
        self.deliver(self.priced_config())  # Uninterrupted: Claude with nonzero 5m and 1h cache writes.
        uninterrupted = self.costs(state='IMPLEMENT')[0]
        m = 'claude-sonnet-4-6'
        expected = [(m, 'input', 1200, '3', '0.0036'), (m, 'cache_read', 8000, '0.30', '0.0024'), (m, 'output', 340, '15', '0.0051'),
                    (m, 'cache_write_5m', 300, '3.75', '0.001125'), (m, 'cache_write_1h', 200, '6', '0.0012')]
        self.assertEqual([(l['model'], l['category'], l['tokens'], l['rate'], l['amount']) for l in uninterrupted['lines']], expected)
        self.assertEqual((uninterrupted['status'], uninterrupted['unpriced'], uninterrupted['estimated_amount']), ('complete', [], '0.013425'))
        snapshot = economics.read_snapshot(self.work, uninterrupted['invocation_id'])
        self.assertEqual([(i['category'], i['tokens']) for i in snapshot['quantities']['items']][-2:],
                         [('cache_write_5m', 300), ('cache_write_1h', 200)])  # The split is frozen with the rates.

        self.work = self.root/'crashed'; self.delivery = self.work/'workspace'
        real = economics._write
        def costs_fail(root, kind, record):
            if kind == 'costs':
                raise OSError('crash after the snapshot')
            return real(root, kind, record)
        with patch('switchyard.economics._write', side_effect=costs_fail):
            self.deliver(self.priced_config())
        self.assertFalse((self.work/'economics/costs').exists())
        frozen = self.economics_bytes()
        self.set_catalog(catalog(revision='test.2', sonnet_input='30'))  # Recovery must not reread the current catalog.
        writer = Diagnostics(self.work, 'work'); writer.bind(LifecycleUsageTests.compiled(self))
        with patch('switchyard.economics.load_registry', side_effect=AssertionError('recovery must not load prices')):
            writer.backfill(self.canonical(), _operations(_store(self.work)))
        recovered = self.costs(state='IMPLEMENT')[0]
        self.assertEqual((recovered['observed_by'], recovered['snapshot_integrity']), ('recovery_backfill', 'verified'))
        self.assertEqual([(l['model'], l['category'], l['tokens'], l['rate'], l['amount']) for l in recovered['lines']], expected)
        uninterrupted = dict(uninterrupted, pricing_snapshot_id=None, cost_id=None, usage_id=None)  # Work-scoped identities.
        crashed_ids = dict(pricing_snapshot_id=None, cost_id=None, usage_id=None)
        self.assertEqual({k: dict(recovered, **crashed_ids)[k] for k in assessed}, {k: uninterrupted[k] for k in assessed})
        self.assertEqual(recovered['cost_id'], economics.cost_id(recovered['work_id'], recovered['invocation_id']))
        self.assertEqual(recovered['pricing_snapshot_id'], economics.read_snapshot(self.work, recovered['invocation_id'])['snapshot_id'])
        self.assertTrue(all(self.economics_bytes()[k] == v for k, v in frozen.items()))  # The snapshot is never rewritten.
        usage = b''.join(self.usage_bytes().values())
        self.assertNotIn(b'ephemeral_', usage); self.assertNotIn(b'cache_write_5m', usage)  # Usage records unchanged.
        writer.backfill(self.canonical(), _operations(_store(self.work)))
        self.assertEqual(self.costs(state='IMPLEMENT')[0], recovered)  # Idempotent.

    # 9, 10, 21
    def test_missing_usage_missing_prices_and_legacy_works(self):
        self.set_catalog(dict(catalog(), entries=[]))  # Nothing priced for the fake models.
        self.deliver(self.with_usage())  # Codex reports no usable usage (offline zero default).
        by_state = {r['state']: r for r in self.costs()}
        self.assertEqual((by_state['SPECIFY']['status'], by_state['SPECIFY']['reasons']), ('unavailable', ['usage_unavailable']))
        self.assertEqual((by_state['IMPLEMENT']['status'], by_state['IMPLEMENT']['reasons']),
                         ('unavailable', ['price_not_configured', 'cache_write_duration_not_reported']))
        totals = aggregate(self.costs())['totals']
        self.assertEqual((totals['estimated_reported_usage_cost'], totals['price_unavailable'], totals['usage_unavailable']), (None, 1, 1))
        self.assertIn('Estimated reported-usage cost: —', self.run_costs()[1])
        shutil.rmtree(self.work/'economics')
        self.assertEqual({r['reasons'][0] for r in self.costs() if r['provider'] != 'command'}, {'not_recorded'})
        with patch('switchyard.economics._recording_runtime', return_value=False):  # A work from before 2.7.0.
            self.assertEqual({r['reasons'][0] for r in self.costs() if r['provider'] != 'command'}, {'historical_pricing_not_captured'})
        self.set_catalog({'broken': True})
        self.work = self.root/'second'; self.delivery = self.work/'workspace'
        self.assertEqual(self.deliver(self.priced_config()).result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual({r['reasons'][0] for r in self.costs() if r['provider'] != 'command'}, {'pricing_catalog_invalid'})


if __name__ == '__main__':
    unittest.main()

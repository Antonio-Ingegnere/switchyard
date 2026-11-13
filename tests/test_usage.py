"""ECONOMICS (#2) slice 1: provider-reported token usage. Normalizers on documented envelope/event shapes, then
real offline deliveries with the fake providers and real temporary works."""
import contextlib
from dataclasses import replace
import hashlib
import io
import json
import shutil
import sys
import unittest
from unittest.mock import patch

from switchyard import usage
from switchyard.cli import main
from switchyard.diagnostics import Diagnostics, read_diagnostics
from switchyard.provider_stages import claude_usage, codex_usage
from switchyard.recovery import _operations, _store, qa_fix_v2_work, resume_v2_work
from switchyard.usage import aggregate, read_usage

import test_human_input
from test_provider_diagnostics import DiagnosticsBase, SECRET


def envelope(**fields):
    return json.dumps(dict(type='result', subtype='success', is_error=False, **fields)).encode()


USAGE = dict(input_tokens=1200, output_tokens=340, cache_read_input_tokens=8000, cache_creation_input_tokens=500)
MODEL = {'claude-sonnet-4-6': dict(inputTokens=1200, outputTokens=340, cacheReadInputTokens=8000, cacheCreationInputTokens=500)}


class ClaudeNormalizationTests(unittest.TestCase):
    # 1, 2, 3, 10
    def test_final_cumulative_usage_with_cache_semantics_is_taken_once(self):
        u = claude_usage(envelope(usage=USAGE, num_turns=5, total_cost_usd=0.42, modelUsage=MODEL))
        self.assertEqual((u['source'], u['tokens'], u['turns']),
                         ('provider_reported', dict(input=1200, output=340, cache_read=8000, cache_write=500), 5))  # Not x5.
        self.assertEqual(u['semantics'], dict(basis='final_cumulative_result', input_includes_cache=False, authoritative_total=None,
                                              coverage_scope='invocation_wide_reconciled',
                                              fields=dict(input='usage.input_tokens', output='usage.output_tokens',
                                                          cache_read='usage.cache_read_input_tokens',
                                                          cache_write='usage.cache_creation_input_tokens')))
        self.assertEqual((u['reported_model'], u['model_allocation']), ('claude-sonnet-4-6', 'single_reported_model'))
        self.assertEqual(usage._input_including_cache(u['tokens'], u['semantics']), 9700)  # Excluded cache added once.
        self.assertNotIn('0.42', json.dumps(u))  # Monetary field not recorded.

    # 7, 8, 9
    def test_missing_zero_unknown_and_invalid_counts(self):
        self.assertEqual(claude_usage(envelope())['reason'], 'not_reported')
        self.assertEqual(claude_usage(b'not json')['reason'], 'no_result_envelope')
        u = claude_usage(envelope(usage=dict(input_tokens=0, output_tokens=7)))
        self.assertEqual(u['tokens'], dict(input=0, output=7, cache_read=None, cache_write=None))  # Reported zero vs unknown.
        self.assertIsNone(usage._input_including_cache(u['tokens'], u['semantics']))  # Not provable without cache counts.
        for bad, field in ((-1, 'input_tokens'), (1.5, 'output_tokens'), (True, 'input_tokens'), (10**13, 'cache_read_input_tokens'),
                           ('12', 'output_tokens')):
            u = claude_usage(envelope(usage=dict(USAGE, **{field: bad})))
            self.assertEqual((u['source'], u['reason'], u['invalid_fields']), ('unavailable', 'invalid_provider_usage', ['usage.'+field]), bad)
            self.assertNotIn('tokens', u)  # Never coerced into a seemingly valid count.
        self.assertEqual(claude_usage(envelope(usage=[1, 2]))['reason'], 'invalid_provider_usage')

    # 11
    def test_model_attribution_never_guesses(self):
        two = dict(MODEL, **{'claude-haiku-5-5': dict(inputTokens=10, outputTokens=2, cacheReadInputTokens=0, cacheCreationInputTokens=0)})
        u = claude_usage(envelope(usage=USAGE, modelUsage=two))
        self.assertEqual((u['model_allocation'], u['reported_model'], [m['model'] for m in u['model_breakdown']]),
                         ('multi_model_breakdown', None, ['claude-haiku-5-5', 'claude-sonnet-4-6']))
        self.assertEqual(u['tokens']['input'], 1200)  # The aggregate only; the breakdown is never added.
        record = dict(u, configured_model='sonnet')
        self.assertEqual(usage.attributed_model(record), 'unallocated')
        off = claude_usage(envelope(usage=USAGE, modelUsage={'claude-opus-5': dict(inputTokens=1, outputTokens=1)}))
        self.assertEqual((off['model_allocation'], off['reported_model']), ('inconsistent_breakdown', 'claude-opus-5'))
        self.assertEqual(claude_usage(envelope(usage=USAGE, modelUsage={'$(evil)': {}}))['model_allocation'], 'unknown')
        self.assertEqual(usage.attributed_model(dict(model_allocation='not_reported', configured_model='sonnet')), 'sonnet')


class CodexNormalizationTests(unittest.TestCase):
    # 5, 6, 10
    def test_turn_and_cumulative_events_are_not_interchangeable(self):
        turn = lambda i, c, o: json.dumps(dict(type='turn.completed', usage=dict(input_tokens=i, cached_input_tokens=c, output_tokens=o)))
        total = lambda i, c, o: json.dumps(dict(msg=dict(type='token_count', info=dict(
            total_token_usage=dict(input_tokens=i, cached_input_tokens=c, output_tokens=o),
            last_token_usage=dict(input_tokens=1, cached_input_tokens=1, output_tokens=1)))))
        u = codex_usage([turn(100, 40, 10), json.dumps(dict(type='item.completed')), turn(300, 200, 30)])
        self.assertEqual((u['tokens'], u['semantics']['basis'], u['turns']),
                         (dict(input=400, cache_read=240, output=40, cache_write=None), 'sum_of_turns', 2))
        self.assertTrue(u['semantics']['input_includes_cache'])
        self.assertEqual(usage._input_including_cache(u['tokens'], u['semantics']), 400)  # Cache is inside input: no double count.
        u = codex_usage([total(100, 40, 10), total(400, 240, 40)])  # Cumulative snapshots: latest wins, never summed.
        self.assertEqual((u['tokens']['input'], u['semantics']['basis']), (400, 'cumulative_snapshot'))
        self.assertEqual(codex_usage([turn(100, 40, 10), total(100, 40, 10)])['source'], 'provider_reported')
        conflict = codex_usage([turn(100, 40, 10), total(999, 40, 10)])
        self.assertEqual((conflict['source'], conflict['invalid_fields']), ('unavailable', ['conflicting_cumulative_and_turn_totals']))
        self.assertEqual(codex_usage(['{broken'])['reason'], 'invalid_provider_usage')
        self.assertEqual(codex_usage([turn(-5, 0, 1)])['invalid_fields'], ['turn.completed.usage.input_tokens'])
        self.assertEqual(codex_usage([])['reason'], 'not_reported')


class UsageBase(DiagnosticsBase):
    def with_usage(self, config=None, **extra):
        config = config or self.config
        fields = dict(dict(usage=USAGE, modelUsage=MODEL, num_turns=2, total_cost_usd=0.5, session_id='sess-'+SECRET), **extra)
        return replace(config, environment={**config.environment, 'FAKE_CLAUDE_ENVELOPE_EXTRA': json.dumps(fields)})

    def records(self, **filters):
        return read_usage(self.work, **filters)[0]

    def by_state(self):
        return {r['invocation_id']: r for r in self.records()}

    def usage_bytes(self):
        d = self.work/'observability/usage'
        return self.files(d) if d.exists() else {}

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['work', 'usage', str(self.work), *args])
        return code, out.getvalue()


class DeliveryUsageTests(UsageBase):
    # 12, 20, 23, 24, 28, 29
    def test_a_delivery_records_one_usage_record_per_invocation(self):
        self.deliver(self.with_usage())
        records = self.records()
        self.assertEqual([(r['state'], r['provider'], r['source'], r['reason']) for r in records],
                         [('SPECIFY', 'codex', 'unavailable', 'not_reported_in_exec_mode'), ('IMPLEMENT', 'claude', 'provider_reported', None),
                          ('VERIFY', 'command', 'not_applicable', 'verifier_command')])
        claude = records[1]
        self.assertEqual((claude['tokens'], claude['configured_model'], claude['resolved_model'], claude['reported_model'], claude['turns']),
                         (dict(input=1200, output=340, cache_read=8000, cache_write=500), 'sonnet', 'sonnet', 'claude-sonnet-4-6', 2))
        self.assertEqual((claude['completeness'], claude['derived']['input_including_cache'], claude['observed_by']), ('complete', 9700, 'runtime'))
        self.assertEqual(claude['usage_id'], hashlib.sha256(('work\0%s\0usage' % claude['invocation_id']).encode()).hexdigest()[:32])
        self.assertIsInstance(claude['duration_ms'], int)
        self.assertEqual([r['tokens'] for r in (records[0], records[2])], [dict(input=None, output=None, cache_read=None, cache_write=None)]*2)
        self.assertEqual(len(self.calls()), 2)  # 20: no extra provider process.
        data = b''.join(self.usage_bytes().values())
        for leaked in (SECRET, 'sess-', '0.5', 'total_cost', 'structured_output', 'operational', 'Change source.py'):
            self.assertNotIn(leaked.encode(), data, leaked)  # 28
        self.assert_consistent()  # 29: trace unaffected; diagnostics still one per invocation.
        self.assertEqual([d['invocation_id'] for d in read_diagnostics(self.work)], [r['invocation_id'] for r in records])
        code, text = self.run_cli()
        lines = text.splitlines()
        self.assertEqual(lines[2].split()[:4], ['STATE', 'PROVIDER/MODEL', 'INV', 'INPUT'])
        self.assertIn('claude/claude-sonnet-4-6  I2   1,200     340       8,000          500  provider_reported', text)
        self.assertIn('codex/offline             I1       —       —           —            —  unavailable (not_reported_in_exec_mode)', text)
        for line in ('  Input:                         1,200  (1 invocation(s) reporting)', '  Provider invocations: 2',
                     '  Usage unavailable:    1', '  Not applicable:       1 (command verifiers)', 'Estimated cost: not calculated',
                     'Billed cost:    unavailable', '  Totals cover 1 of 2 provider invocations.'):
            self.assertIn(line, text)
        self.assertNotIn(' 0 ', text.split('Reported totals')[0].split('\n', 3)[3])  # No zero stands in for unknown.
        payload = json.loads(self.run_cli('--json')[1])
        self.assertEqual((payload['totals']['tokens']['input'], payload['totals']['usage_reported'], payload['estimated_cost'],
                          payload['billed_cost'], payload['work_id']),
                         (dict(reported_sum=1200, invocations_reporting=1), 1, None, None, 'work'))
        self.assertEqual(payload['records'], json.loads(json.dumps(records)))

    # 4
    def test_structured_output_repair_is_one_invocation_counted_once(self):
        config = self.with_usage()
        config = replace(config, environment={**config.environment, 'FAKE_SCHEMA_INVALID_ATTEMPTS': '1'})
        self.deliver(config)
        implement = self.records(state='IMPLEMENT')
        self.assertEqual((len(implement), sum(c['provider'] == 'claude' for c in self.calls())), (1, 1))
        self.assertEqual(implement[0]['tokens']['input'], 1200)  # The final cumulative envelope, once; not per repair turn.

    # 13, 14
    def test_failed_invocations_with_and_without_reported_usage(self):
        failed = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True, usage=dict(USAGE, input_tokens=77))).encode()
        self.deliver(self.claude_emits(failed, exit_code=1), fails=True)
        r = self.records(state='IMPLEMENT')[0]
        self.assertEqual((r['source'], r['tokens']['input'], read_diagnostics(self.work, r['invocation_id'])[0]['result']['classification']),
                         ('provider_reported', 77, 'provider_exit'))  # A failure still consumed what it reports.
        self.work = self.root/'second'; self.delivery = self.work/'workspace'  # A fresh work.
        self.interrupted()  # Exit 7 before any output.
        r = self.records(state='IMPLEMENT')[0]
        self.assertEqual((r['source'], r['reason'], r['tokens']['input']), ('unavailable', 'no_result_envelope', None))

    # 15
    def test_a_timeout_or_launch_failure_never_claims_usage(self):
        self.configure(code=dict(timeout=1))
        (self.root/'claude').write_text('#!%s\nimport sys, time\nsys.stdin.read()\ntime.sleep(5)\n' % sys.executable)
        self.deliver(fails=True)
        r = self.records(state='IMPLEMENT')[0]
        self.assertEqual((r['source'], r['reason'], r['completeness'], set(r['tokens'].values())),
                         ('unavailable', 'no_result_envelope', 'unavailable', {None}))
        self.work = self.root/'second'; self.delivery = self.work/'workspace'  # A fresh work.
        (self.root/'claude').write_text('#!/nonexistent/interpreter\n')
        self.deliver(fails=True)
        self.assertEqual(self.records(state='IMPLEMENT')[0]['reason'], 'not_launched')


class LifecycleUsageTests(UsageBase):
    # 16, 17, 18, 30
    def test_retries_resumes_and_reopens_each_count_once(self):
        failed = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True, usage=dict(USAGE, input_tokens=100))).encode()
        self.deliver(self.claude_emits(failed, exit_code=1), fails=True)
        before = self.usage_bytes()
        execution = resume_v2_work(self.work, self.text, self.with_usage())
        self.assertTrue(all(self.usage_bytes()[k] == v for k, v in before.items()))  # 17: earlier records never rewritten.
        implement = self.records(state='IMPLEMENT')
        self.assertEqual([(r['tokens']['input'], r['state_visit_id']) for r in implement],
                         [(100, implement[0]['state_visit_id']), (1200, implement[0]['state_visit_id'])])  # 16
        self.assertEqual(aggregate(self.records())['totals']['tokens']['input'], dict(reported_sum=1300, invocations_reporting=2))
        self.assertEqual(aggregate(self.records()), aggregate(self.records()))  # Repeated observation changes nothing.
        self.candidate(execution)
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.with_usage())
        records = self.records()
        self.assertEqual(len(records), len({r['invocation_id'] for r in records}))  # 30: one record per invocation.
        self.assertEqual(aggregate(records)['totals']['tokens']['input']['reported_sum'], 2500)
        frozen = self.usage_bytes()
        writer = Diagnostics(self.work, 'work'); writer.bind(self.compiled())
        writer.backfill(self.canonical(), _operations(_store(self.work)))  # 18: retried recovery.
        self.assertEqual(self.usage_bytes(), frozen)

    def compiled(self):
        from switchyard.recovery import _Projection
        from switchyard.simple_config import simple_config_from_text
        from switchyard.workflow_compiler import compile_workflow
        return compile_workflow(simple_config_from_text(self.text).bundle, 'feature', lambda b: _Projection())

    # 19, 18
    def test_a_usage_publication_failure_changes_nothing_and_is_never_fabricated(self):
        with patch('switchyard.usage.write', side_effect=OSError('disk full')):
            execution = self.deliver(self.with_usage())
        self.assertEqual((execution.result.terminal_state, len(self.calls())), ('READY_FOR_MANUAL_QA', 2))
        self.assertEqual(len(read_diagnostics(self.work)), 3)  # Diagnostics unaffected.
        self.assertEqual({(r['source'], r['reason']) for r in self.records() if r['provider'] != 'command'}, {('unavailable', 'not_recorded')})
        self.candidate(execution)
        qa_fix_v2_work(self.work, 'Still wrong.', self.text, self.with_usage())  # Next drive: proven-end backfill.
        first = self.records()[:3]
        self.assertEqual([(r['source'], r['reason'], r['observed_by']) for r in first],
                         [('unavailable', 'not_captured', 'recovery_backfill'), ('unavailable', 'not_captured', 'recovery_backfill'),
                          ('not_applicable', 'verifier_command', 'recovery_backfill')])  # No durable evidence: not reconstructed.
        self.assertEqual(self.records()[3]['source'], 'provider_reported')


class CoverageScopeTests(UsageBase):
    # Regression: top-level `usage` counts only the top-level agent loop (Agent SDK contract); subagent and auxiliary
    # model calls appear only in `modelUsage`. Such totals must never be presented as invocation-wide consumption.
    def test_top_level_usage_with_subagent_calls_is_labelled_and_never_merged_with_the_breakdown(self):
        tree = dict(MODEL, **{'claude-haiku-5-5': dict(inputTokens=3000, outputTokens=600, cacheReadInputTokens=0, cacheCreationInputTokens=0)})
        self.deliver(self.with_usage(modelUsage=tree))
        r = self.records(state='IMPLEMENT')[0]
        self.assertEqual((r['coverage_scope'], r['completeness'], r['completeness_reasons'], r['model_allocation']),
                         ('top_level_agent_loop', 'partial', ['scope_not_invocation_wide'], 'multi_model_breakdown'))
        self.assertEqual(r['tokens'], dict(input=1200, output=340, cache_read=8000, cache_write=500))  # Top-level loop only.
        self.assertEqual(r['model_breakdown_total'], dict(input=4200, output=940, cache_read=8000, cache_write=500))  # Separate.
        totals = aggregate(self.records())['totals']
        self.assertEqual((totals['tokens']['input']['reported_sum'], totals['agent_tree_by_model']['input']['reported_sum'],
                          totals['coverage_scope'], totals['invocation_wide']),
                         (1200, 4200, {'top_level_agent_loop': 1}, False))  # Never 5400: nothing is double-counted.
        text = self.run_cli()[1]
        self.assertIn('  scope: top-level agent loop as reported for 1 of 1 reporting invocation(s); may exclude subagent,', text)
        self.assertIn('not complete invocation-wide consumption', text)
        self.assertIn('Per-model breakdown, whole agent tree incl. subagents (separate; not added to the totals above):', text)
        self.assertIn('  Input:                         1,200  (1 invocation(s) reporting)', text)
        self.assertIn('  Input:                         4,200  (1 invocation(s) reporting)', text)
        # Without a breakdown the scope cannot be proven; a documented zeroed crash result is not trusted as zero.
        self.assertEqual(claude_usage(envelope(usage=USAGE))['semantics']['coverage_scope'], 'top_level_agent_loop_unverified')
        crashed = json.dumps(dict(type='result', subtype='error_during_execution', is_error=True,
                                  usage=dict(input_tokens=0, output_tokens=0))).encode()
        self.assertEqual(claude_usage(crashed)['reason'], 'zeroed_crash_result')
        budget = json.dumps(dict(type='result', subtype='error_max_budget_usd', is_error=True, usage=USAGE, modelUsage=MODEL)).encode()
        self.assertEqual(claude_usage(budget)['semantics']['coverage_scope'], 'top_level_agent_loop')  # Omits the crossing response.


class ReadUsageTests(UsageBase):
    # 21, 22, 25, 26, 27
    def test_aggregation_filters_coverage_read_only_and_legacy(self):
        self.interrupted()
        resume_v2_work(self.work, self.text, self.with_usage())
        before = self.files(self.work)
        records = self.records()
        summary = aggregate(records)
        t = summary['totals']
        self.assertEqual((t['invocations'], t['provider_invocations'], t['usage_reported'], t['usage_unavailable'], t['not_applicable']),
                         (4, 3, 1, 2, 1))
        self.assertEqual(t['tokens']['cache_write'], dict(reported_sum=500, invocations_reporting=1))
        groups = summary['groups']
        self.assertEqual({d: [g['key'] for g in groups[d]] for d in ('state', 'agent', 'provider', 'model')},
                         dict(state=['SPECIFY', 'IMPLEMENT', 'VERIFY'], agent=['spec', 'code', 'tests'], provider=['codex', 'claude', 'command'],
                              model=['offline', 'sonnet', 'claude-sonnet-4-6', None]))
        self.assertEqual(len(groups['invocation']), 4); self.assertEqual(len(groups['run']), 1)
        implement = [g for g in groups['state'] if g['key'] == 'IMPLEMENT'][0]
        self.assertEqual((implement['usage_reported'], implement['usage_unavailable'], implement['tokens']['input']['reported_sum']), (1, 1, 1200))
        for dimension, groups_ in groups.items():  # Each invocation counted once per grouping.
            self.assertEqual(sum(g['invocations'] for g in groups_), 4, dimension)
            self.assertEqual(sum(g['tokens']['input']['reported_sum'] or 0 for g in groups_), 1200, dimension)
        unavailable = [g for g in groups['state'] if g['key'] == 'SPECIFY'][0]
        self.assertEqual(unavailable['tokens']['input'], dict(reported_sum=None, invocations_reporting=0))  # Not zero.
        self.assertEqual([r['state'] for r in self.records(provider='claude')], ['IMPLEMENT', 'IMPLEMENT'])
        self.assertEqual(len(self.records(model='claude-sonnet-4-6')), 1)
        self.assertEqual(len(self.records(model='sonnet')), 2)
        self.assertEqual([r['agent'] for r in self.records(agent='tests')], ['tests'])
        self.assertEqual(len(self.records(invocation='I3')), 1); self.assertEqual(self.records(run='run:none'), [])
        code, text = self.run_cli('--state', 'IMPLEMENT')
        self.assertIn('Provider invocations: 2', text)
        self.assertEqual(json.loads(self.run_cli('--provider', 'codex', '--json')[1])['totals']['usage_unavailable'], 1)
        self.assertEqual(self.files(self.work), before)  # 26: reading never writes or backfills.
        shutil.rmtree(self.work/'observability/usage')  # A 2.6.0 work whose usage writes all failed: not "legacy".
        self.assertEqual({r['reason'] for r in self.records() if r['provider'] != 'command'}, {'not_recorded'})
        with patch('switchyard.usage._recording_runtime', return_value=False):  # 27: a work from before this runtime version.
            self.assertEqual({r['reason'] for r in self.records() if r['provider'] != 'command'}, {'not_recorded_by_this_runtime_version'})
        shutil.rmtree(self.work/'observability'); (self.work/'events.jsonl').unlink()
        self.assertEqual(self.run_cli()[1].strip(), 'No invocations recorded.')
        with patch('switchyard.usage._recording_runtime', return_value=False):
            self.assertEqual(self.run_cli()[1].strip(), 'Usage unavailable: not recorded by this runtime version')


class HumanInputUsageTests(unittest.TestCase):
    _base = test_human_input.HumanInputTests
    setUp, setUp_base, git, save_config, start, answer = (_base.setUp, _base.setUp_base, _base.git, _base.save_config,
                                                          _base.start, _base.answer)

    def test_stub_stages_and_human_waits_record_unavailable_usage_once(self):
        self.start(); self.answer()
        records = read_usage(self.work)[0]
        self.assertEqual(len(records), len({r['invocation_id'] for r in records}))
        self.assertTrue(records and all(r['source'] in ('unavailable', 'not_applicable') for r in records))


if __name__ == '__main__':
    unittest.main()

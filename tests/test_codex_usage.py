"""ECONOMICS (#2) slice 1B: Codex usage from `codex exec --json`. The bounded stream parser on the documented event
shapes, then real offline deliveries through the real subprocess transport with the fake Codex CLI."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from switchyard import usage
from switchyard.diagnostics import Diagnostics, read_diagnostics
from switchyard.provider_stages import CODEX_MAX_EVENT_LINE, CodexUsageStream, codex_usage
from switchyard.recovery import resume_v2_work, _operations, _store
from switchyard.usage import aggregate

from test_provider_diagnostics import SECRET
from test_usage import LifecycleUsageTests, UsageBase

DOCUMENTED = dict(input_tokens=24763, cached_input_tokens=24448, output_tokens=122, reasoning_output_tokens=0)
REPORTED = dict(input_tokens=24763, cached_input_tokens=24448, cache_write_input_tokens=0, output_tokens=122,
                reasoning_output_tokens=64)


def line(kind, **fields):
    return json.dumps(dict(type=kind, **fields)).encode() + b'\n'


def completed(usage):
    return line('turn.completed', usage=usage)


class CodexStreamTests(unittest.TestCase):
    # 5, 6, 7, 8, 9
    def test_documented_turn_completed_usage_and_category_semantics(self):
        u = codex_usage([completed(DOCUMENTED).strip()])
        self.assertEqual((u['source'], u['tokens'], u['output_detail'], u['turns']),
                         ('provider_reported', dict(input=24763, output=122, cache_read=24448, cache_write=None),
                          dict(reasoning_output=0), 1))  # No cache-write field: unknown, not zero.
        self.assertEqual((u['semantics']['basis'], u['semantics']['input_includes_cache'], u['semantics']['output_includes_reasoning'],
                          u['semantics']['coverage_scope']),
                         ('cumulative_thread_total', True, True, 'exec_thread_total_unverified'))
        self.assertEqual(usage._input_including_cache(u['tokens'], u['semantics']), 24763)  # Never input + cached.
        u = codex_usage([completed(REPORTED).strip()])
        self.assertEqual((u['tokens']['cache_write'], u['tokens']['output'], u['output_detail']['reasoning_output']),
                         (0, 122, 64))  # Explicitly reported cache write kept; reasoning stays inside output.
        u = codex_usage([completed(dict(output_tokens=9)).strip()])
        self.assertEqual((u['tokens'], u['output_detail']), (dict(input=None, output=9, cache_read=None, cache_write=None),
                                                             dict(reasoning_output=None)))

    # 10
    def test_invalid_counts_are_rejected_never_coerced(self):
        for bad in (True, 1.5, -1, '12', 10**13, None):
            field = 'output_tokens'
            u = codex_usage([completed(dict(DOCUMENTED, **{field: bad})).strip()])
            if bad is None:
                self.assertIsNone(u['tokens']['output'])  # JSON null: not reported.
                continue
            self.assertEqual((u['source'], u['reason'], u['invalid_fields']),
                             ('unavailable', 'invalid_provider_usage', ['turn.completed.usage.output_tokens']), bad)
        self.assertEqual(codex_usage([line('turn.completed', usage=[1]).strip()])['invalid_fields'], ['turn.completed.usage'])

    # 11, 12, 13, 14
    def test_chunk_boundaries_oversized_malformed_and_unknown_events(self):
        stream = b''.join((line('thread.started', thread_id='t-'+SECRET), b'{broken\n', b'[1]\n', line('item.updated'),
                           line('item.completed', item=dict(type='agent_message', text='x'*(CODEX_MAX_EVENT_LINE*3))),
                           line('future.event', usage=dict(input_tokens=1)), completed(REPORTED)))
        for size in (1, 7, 4096, len(stream)):  # Byte-by-byte, split lines, many events per read.
            observer = CodexUsageStream()
            peak = 0
            for i in range(0, len(stream), size):
                observer(stream[i:i+size]); peak = max(peak, len(observer.line))
            u = observer.finish()
            self.assertEqual((u['source'], u['tokens']['input']), ('provider_reported', 24763), size)
            self.assertLessEqual(peak, CODEX_MAX_EVENT_LINE)  # Never the whole stream, never a whole oversized line.
            self.assertEqual((observer.malformed, observer.oversized), (2, 1))
            self.assertNotIn(SECRET, json.dumps(vars(observer), default=str))
        oversized_usage = line('turn.completed', usage=DOCUMENTED, pad='x'*CODEX_MAX_EVENT_LINE)
        self.assertEqual(codex_usage([oversized_usage.strip()])['invalid_fields'], ['oversized_turn_completed_event'])
        unterminated = CodexUsageStream(); unterminated(completed(DOCUMENTED).strip())
        self.assertEqual(unterminated.finish()['tokens']['output'], 122)

    # 15, 16 and the zero default
    def test_cumulative_snapshots_failed_turns_and_unproven_zero(self):
        later = dict(DOCUMENTED, input_tokens=30000, output_tokens=200)
        u = codex_usage([completed(DOCUMENTED).strip(), completed(later).strip()])
        self.assertEqual((u['tokens']['input'], u['turns']), (30000, 2))  # Cumulative: latest wins, never 54763.
        conflict = codex_usage([completed(later).strip(), completed(DOCUMENTED).strip()])
        self.assertEqual(conflict['invalid_fields'], ['conflicting_cumulative_snapshots'])
        self.assertEqual(codex_usage([line('turn.started').strip(), line('turn.failed', error=dict(message='x')).strip()])['reason'],
                         'turn_failed_without_usage')
        zeros = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0, output_tokens=0, reasoning_output_tokens=0)
        self.assertEqual(codex_usage([completed(zeros).strip()])['reason'], 'unverified_zero_usage')
        self.assertEqual(codex_usage([])['reason'], 'no_jsonl_events')
        self.assertEqual(codex_usage([line('turn.started').strip()])['reason'], 'not_reported')
        broken = CodexUsageStream()
        with patch.object(CodexUsageStream, '_event', side_effect=RuntimeError('parser bug')):
            broken(completed(DOCUMENTED))
        self.assertEqual(broken.finish()['reason'], 'usage_capture_failed')


class CodexDeliveryTests(UsageBase):
    def codex(self, config=None, usage=REPORTED, jsonl=None, **env):
        config = config or self.config
        extra = {'FAKE_CODEX_USAGE': json.dumps(usage)} if usage is not None else {}
        if jsonl is not None:
            (self.root/'codex.jsonl').write_bytes(jsonl); extra['FAKE_CODEX_JSONL'] = str(self.root/'codex.jsonl')
        return replace(config, environment={**config.environment, **extra, **env})

    def specify(self):
        return self.records(state='SPECIFY')

    def codex_calls(self):
        return [c for c in self.calls() if c.get('provider') == 'codex']

    def durable(self):
        return b''.join(v for k, v in self.files(self.work).items() if not k.startswith('workspace/'))

    # 1, 2, 3, 4, 5, 23, 24, 25, 26, 28
    def test_a_successful_codex_invocation_reports_usage_and_its_result_comes_only_from_the_file(self):
        prompt = 'PROMPT_TEXT ' + SECRET
        decoy = dict(outcome='blocked', produced_artifacts=[], handoff=None, diagnostics=None)  # Never the StageResult.
        jsonl = b''.join((line('thread.started', thread_id='0199-thread-'+SECRET), line('turn.started'),
                          line('item.completed', item=dict(type='command_execution', command='cat ~/.netrc', aggregated_output=SECRET)),
                          line('item.completed', item=dict(type='file_change', changes=[dict(path='billing.py', kind='update')])),
                          line('item.completed', item=dict(type='reasoning', text=prompt)),
                          line('item.completed', item=dict(type='agent_message', text=json.dumps(decoy))), completed(REPORTED)))
        execution = self.deliver(self.with_usage(self.codex(jsonl=jsonl)))
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')  # Accepted from --output-last-message.
        argv = self.codex_calls()[0]['argv']
        paths = argv[argv.index('--cd')+1], argv[argv.index('--output-schema')+1], argv[argv.index('--output-last-message')+1]
        self.assertEqual(argv, ['--ask-for-approval', 'never', 'exec', '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules',
                                '--sandbox', 'read-only', '--skip-git-repo-check', '--cd', paths[0], '--model', 'offline',
                                '--output-schema', paths[1], '--output-last-message', paths[2], '-c', 'features.shell_tool=true', '-'])
        self.assertEqual([p.rsplit('/', 1)[-1] for p in paths[1:]], ['schema.json', 'result.json'])  # Only --json is new.
        self.assertEqual((len(self.codex_calls()), len(self.calls())), (1, 2))  # 26: no additional launch.
        r = self.specify()[0]
        self.assertEqual((r['source'], r['tokens'], r['output_detail'], r['provider'], r['configured_model']),
                         ('provider_reported', dict(input=24763, output=122, cache_read=24448, cache_write=0),
                          dict(reasoning_output=64), 'codex', 'offline'))
        self.assertEqual((r['completeness'], r['completeness_reasons'], r['derived']['input_including_cache']),
                         ('partial', ['scope_not_invocation_wide'], 24763))
        totals = aggregate(self.records())['totals']
        self.assertEqual(totals['tokens']['input'], dict(reported_sum=None, invocations_reporting=2, withheld='mixed_input_semantics'))
        self.assertEqual((totals['input_including_cache']['reported_sum'], totals['tokens']['output']['reported_sum'],
                          totals['output_detail']['reasoning_output']['reported_sum']), (24763+9700, 122+340, 64))
        claude = self.records(state='IMPLEMENT')[0]  # 24: Claude accounting unchanged.
        self.assertEqual((claude['tokens']['input'], claude['coverage_scope'], claude['completeness']),
                         (1200, 'invocation_wide_reconciled', 'complete'))
        text = self.run_cli()[1]
        self.assertEqual(text.splitlines()[3].split()[:9], ['SPECIFY', 'codex/offline', 'I1', '24,763', '122', '64', '24,448', '0',
                                                             'provider_reported'])
        for expected in ('Input:                         not combined', 'Input (codex):                 24,763  (1 invocation(s) reporting)',
                         'Input (claude):                1,200  (1 invocation(s) reporting)', 'scope: Codex exec thread cumulative total',
                         'Reasoning output (in output):  64  (1 invocation(s) reporting)', '* reasoning output is a subset of output'):
            self.assertIn(expected, text)
        self.assertEqual(json.loads(self.run_cli('--provider', 'codex', '--json')[1])['totals']['tokens']['input']['reported_sum'], 24763)
        data = self.durable()
        for leaked in (SECRET, 'PROMPT_TEXT', '.netrc', 'billing.py', '0199-thread', 'agent_message', 'blocked', 'thread_id'):
            self.assertNotIn(leaked.encode(), data, leaked)  # 25: no JSONL content in any durable record.
        self.assert_consistent()  # 28
        self.assertEqual(self.git('status', '--porcelain'), '')

    # 17, 20, 21, 22
    def test_a_failed_exit_with_usage_and_its_retry_are_two_records_counted_once(self):
        self.deliver(self.codex(usage=DOCUMENTED, FAKE_CODEX_EXIT='1'), fails=True)
        first = self.specify()[0]
        self.assertEqual((first['source'], first['tokens']['input']), ('provider_reported', 24763))  # A failure still consumed.
        self.assertEqual(read_diagnostics(self.work, first['invocation_id'])[0]['result']['classification'], 'provider_exit')
        before = self.usage_bytes()
        resume_v2_work(self.work, self.text, self.with_usage(self.codex(usage=dict(DOCUMENTED, input_tokens=1000))))
        self.assertTrue(all(self.usage_bytes()[k] == v for k, v in before.items()))
        records = self.specify()
        self.assertEqual([r['tokens']['input'] for r in records], [24763, 1000])
        self.assertEqual(len({r['invocation_id'] for r in records}), 2)
        self.assertEqual(aggregate(records)['totals']['tokens']['input']['reported_sum'], 25763)  # Each exactly once.
        self.assertEqual(len(self.codex_calls()), 2)
        frozen = self.usage_bytes()
        writer = Diagnostics(self.work, 'work')
        writer.bind(self.compiled()); writer.backfill(self.canonical(), _operations(_store(self.work)))
        self.assertEqual(self.usage_bytes(), frozen)  # 22: recovery never duplicates or rewrites.

    compiled = LifecycleUsageTests.compiled

    # 16, 18, 19, 27 and a parser failure
    def test_failures_without_usage_missing_results_unsupported_mode_and_parser_bugs(self):
        cases = [
            ('turn_failed', self.codex(usage=None, jsonl=line('turn.started')+line('turn.failed', error=dict(message='boom')),
                                       FAKE_CODEX_EXIT='1'), ('unavailable', 'turn_failed_without_usage', None), 'provider_exit'),
            ('no_result', self.codex(FAKE_CODEX_NO_RESULT='1'), ('provider_reported', None, 24763), 'invalid_stage_result'),
            ('unsupported', self.codex(FAKE_CODEX_NO_JSON='1'), ('unavailable', 'no_jsonl_events', None), 'provider_exit'),
        ]
        for name, config, expected, classification in cases:
            with self.subTest(name):
                self.work = self.root/name; self.delivery = self.work/'workspace'
                launched = len(self.codex_calls())
                self.deliver(config, fails=True)
                r = self.specify()[0]
                self.assertEqual((r['source'], r['reason'], r['tokens']['input']), expected)
                self.assertEqual(read_diagnostics(self.work, r['invocation_id'])[0]['result']['classification'], classification)
                self.assertEqual(len(self.codex_calls()) - launched, 1)  # 27: no fallback launch without --json.
        self.work = self.root/'timeout'; self.delivery = self.work/'workspace'
        self.configure(spec=dict(timeout=1))
        self.deliver(self.codex(usage=None, jsonl=line('thread.started', thread_id='t')+line('turn.started'), FAKE_CODEX_SLEEP='5'),
                     fails=True)
        r = self.specify()[0]
        self.assertEqual((r['source'], r['reason'], set(r['tokens'].values())), ('unavailable', 'not_reported', {None}))  # 18
        self.assertTrue(read_diagnostics(self.work, r['invocation_id'])[0]['process']['timed_out'])
        self.work = self.root/'parser'; self.delivery = self.work/'workspace'
        self.configure(spec=dict(timeout=60))
        with patch.object(CodexUsageStream, '_feed', side_effect=RuntimeError('parser bug')):
            execution = self.deliver(self.with_usage(self.codex()))
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')  # 14: telemetry never changes the outcome.
        self.assertEqual(self.specify()[0]['reason'], 'usage_capture_failed')


if __name__ == '__main__':
    unittest.main()

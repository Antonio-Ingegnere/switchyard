"""OBSERVABILITY (#1) slice 2: `work logs` / `work watch` over the durable trace. Real deliveries with the offline
provider fixtures and real temporary works; watch progress is driven deterministically through its sleep hook."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from switchyard import logs
from switchyard.cli import main
from switchyard.diagnostics import diagnostic_path, read_diagnostics
from switchyard.recovery import _operation, _store, recovery_status, resume_v2_work
from switchyard.trace import trace_events

import test_human_input
from test_trace import TraceBase

EVENT_KEYS = {'sequence', 'event_id', 'event_type', 'work_id', 'run_id', 'state', 'invocation_id', 'timestamp', 'summary'}


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(['work', *args])
    return code, out.getvalue(), err.getvalue()


class LogsBase(TraceBase):
    def logs(self, *args):
        code, out, _ = run_cli('logs', str(self.work), *args)
        self.assertEqual(code, 0)
        self.assertNotIn('\x1b', out)  # 16: plain line output, no terminal control sequences.
        return out

    def watch(self, during=None, **options):
        """Run watch with a sleep hook: `during(n)` acts between polls (e.g. a controller restart)."""
        out, sleeps = io.StringIO(), []
        def sleep(seconds):
            sleeps.append(seconds)
            if during is not None:
                during(len(sleeps))
        code = logs.watch(self.work, out, sleep=sleep, **{'max_polls': 20, **options})
        self.assertNotIn('\x1b', out.getvalue())
        return code, out.getvalue(), sleeps

    def ndjson(self, text):
        records = [json.loads(line) for line in text.splitlines()]  # 17: every line is one complete JSON object.
        for r in records:
            if r['record'] == 'event':
                self.assertLessEqual(EVENT_KEYS, set(r))
        return records


class LogsTests(LogsBase):
    # 1, 2, 6, 18
    def test_logs_render_a_delivery_in_trace_sequence_order(self):
        self.start(); calls = len(self.calls())
        text = self.logs()
        lines = text.splitlines()
        self.assertEqual(lines[0], 'WORK  work  RUN  %s' % self.canonical()[0].run_id)
        body = [l.split()[1:3] for l in lines[2:]]
        self.assertEqual(body[:7], [['WORK', 'Started'], ['SPECIFY', 'Entered'], ['SPECIFY', 'Started'], ['SPECIFY', 'Completed'],
                                    ['SPECIFY', 'Transition'], ['SPECIFY', 'Exited'], ['IMPLEMENT', 'Entered']])
        self.assertEqual(body[-1], ['WORK', 'Finished'])
        self.assertTrue(any('SPECIFY' in l and 'Started' in l and 'codex/offline  spec, I1, attempt 1' in l for l in lines))
        self.assertTrue(any('IMPLEMENT' in l and 'claude/sonnet  code, I2' in l for l in lines))
        self.assertTrue(any('VERIFY' in l and 'Started' in l and 'command  tests, I3' in l for l in lines))
        completed = [l for l in lines if 'Completed' in l]
        self.assertEqual(len(completed), 3)
        self.assertTrue(all(l.rstrip().endswith(('ms', 's')) for l in completed))  # Durations shown.
        self.assertTrue(any('Transition  -> VERIFY (ready)' in l for l in lines))
        entries = json.loads(self.logs('--json'))
        trace = trace_events(self.work)
        self.assertEqual([(e['sequence'], e['event_id'], e['run_id'], e['invocation_id']) for e in entries],
                         [(e['sequence'], e['event_id'], e['run_id'], e['invocation_id']) for e in trace])
        self.assertTrue(all(EVENT_KEYS <= set(e) for e in entries))
        self.assertEqual(len(self.calls()), calls)  # 18: observing launches nothing.

    # 3, 4
    def test_a_failed_invocation_shows_its_reason_and_diagnostic_reference_without_raw_content(self):
        self.interrupted()
        text = self.logs()
        self.assertIn('IMPLEMENT           Failed      provider_exit (code 7)', text)
        self.assertIn(' '*42+'Invocation I2 | Diagnostic available', text)
        self.assertIn('WORK                Interrupted provider_failure, resume available', text)
        failed = [e for e in json.loads(self.logs('--json')) if e['event_type'] == 'run_interrupted'][0]
        self.assertEqual(failed['diagnostic'], dict(available=True, diagnostic_id=read_diagnostics(self.work, 'I2')[0]['diagnostic_id']))
        detail = self.logs('--invocation', 'I2')
        self.assertIn('Provider exited with status 7 before producing a valid stage result.', detail)
        everything = text + detail + self.logs('--json')
        for raw in ('STAGE OUTPUT CONTRACT', 'EXECUTION BINDING', 'operational stderr chatter', 'operational prose',
                    'progress: operational stdout', 'Change source.py', 'reasoning 0'):
            self.assertNotIn(raw, everything)
        os.unlink(diagnostic_path(self.work, failed['invocation_id']))
        self.assertIn('Invocation I2 | Diagnostic unavailable', self.logs())  # Not an execution failure; just absent.
        self.assertTrue(self.logs('--invocation', 'I2').rstrip().endswith('Diagnostic unavailable'))

    # 5, 13
    def test_filters_and_a_legacy_work_without_trace(self):
        self.start()
        verify = self.logs('--state', 'VERIFY').splitlines()[2:]
        self.assertTrue(verify and all(' VERIFY ' in l for l in verify))
        invocation = self.logs('--invocation', 'I2').split('\n\nWork:')[0].splitlines()[2:]
        self.assertEqual([l.split()[2] for l in invocation], ['Started', 'Completed', 'Transition'])
        self.assertEqual(len(self.logs('--last', '3').splitlines()), 5)
        self.assertEqual([e['sequence'] for e in json.loads(self.logs('--last', '2', '--json'))], [21, 22])
        self.assertEqual(self.logs('--run', 'run:none').strip(), 'No trace events available.')
        before = self.files(self.work)
        shutil.rmtree(self.work/'observability')
        self.assertEqual(self.logs().strip(), 'No trace events available.')
        self.assertEqual(json.loads(self.logs('--json')), [])
        code, out, _ = self.watch()
        self.assertEqual((code, out.splitlines()[2]), (0, 'No trace events available.'))
        self.assertIn('STATUS READY_FOR_MANUAL_QA', out.splitlines()[-1])
        self.assertFalse((self.work/'observability').exists())  # Nothing is backfilled by observing.
        self.assertEqual(self.files(self.work), {k: v for k, v in before.items() if not k.startswith('observability/')})


class WatchTests(LogsBase):
    # 7, 8, 9, 11, 15, 18
    def test_watch_follows_a_controller_restart_and_prints_every_event_once(self):
        self.interrupted(); calls = len(self.calls())
        started = len(trace_events(self.work))
        def during(poll):
            if poll == 1:
                resume_v2_work(self.work, self.text, self.config)  # A new controller drive while the watcher runs.
        code, out, sleeps = self.watch(during, json_mode=True, last=3)
        records = self.ndjson(out)
        events = [r for r in records if r['record'] == 'event']
        self.assertEqual([e['sequence'] for e in events], list(range(started-2, len(trace_events(self.work))+1)))  # Once each.
        self.assertEqual([e['event_id'] for e in events], [e['event_id'] for e in trace_events(self.work)][started-3:])
        self.assertEqual((records[0]['record'], records[0]['status']), ('status', 'INTERRUPTED'))
        self.assertEqual((records[-1]['record'], records[-1]['final'], records[-1]['status']), ('status', True, 'READY_FOR_MANUAL_QA'))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls()), calls + 1)  # Exactly the resumed delivery's own model process.
        before = self.files(self.work)
        self.watch(); self.logs(); self.logs('--json')  # 15: observation never writes.
        self.assertEqual(self.files(self.work), before)

    # 11
    def test_a_terminal_work_prints_its_final_status_and_exits(self):
        self.start()
        code, out, sleeps = self.watch(last=2)
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(sleeps), 1)  # One confirming poll, then exit.
        self.assertIn('Finished    READY_FOR_MANUAL_QA', lines[-2])
        self.assertTrue(lines[-1].startswith('STATUS READY_FOR_MANUAL_QA | next: manual QA:'))
        self.assertEqual(self.watch(interval=0)[2], [logs.MIN_INTERVAL])  # Bounded: no busy polling.

    # 12
    def test_ctrl_c_stops_watch_without_touching_the_work(self):
        self.interrupted()
        before, status = self.files(self.work), recovery_status(self.work)
        before = self.files(self.work)
        with patch('switchyard.logs.time.sleep', side_effect=KeyboardInterrupt):
            code, out, err = run_cli('watch', str(self.work))
        self.assertEqual((code, err.strip()), (130, 'switchyard: watch stopped; the work was not affected'))
        self.assertEqual(self.files(self.work), before)
        self.assertEqual(recovery_status(self.work)['terminal'], 'INTERRUPTED')
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')

    # 15 (pure status reader)
    def test_a_stale_running_record_is_reported_never_reconciled(self):
        self.interrupted()
        _operation(_store(self.work), 'running', state='IMPLEMENT', provider_pid=2**22+12345)  # No such process.
        before = self.files(self.work)
        code, out, sleeps = self.watch(max_polls=3)
        self.assertEqual(len(sleeps), 3)  # RUNNING is active: watch keeps observing.
        self.assertIn('STATUS RUNNING', out)
        self.assertEqual(out.count('note: the recorded provider process'), 1)  # Bounded.
        self.assertEqual(self.files(self.work), before)  # recovery_status would have written an interruption here.

    # 14
    def test_an_incomplete_publication_or_gap_holds_the_cursor_and_nothing_is_skipped(self):
        self.start()
        events = self.work/'observability/events'
        names = sorted(n for n in os.listdir(events) if n.endswith('.json'))
        held = events/names[9]
        data = held.read_bytes()
        held.write_bytes(data[:20])  # Published but not (yet) readable.
        (events/'.tmp-1-000099-x.json').write_text('{')  # An in-flight temporary is never an event.
        cursor = logs.TraceCursor(self.work)
        self.assertEqual([e['sequence'] for e in cursor.poll()], list(range(1, 10)))
        self.assertEqual((cursor.waiting, cursor.poll()), (10, []))
        held.write_bytes(data)
        self.assertEqual([e['sequence'] for e in cursor.poll()], list(range(10, 23)))
        self.assertIsNone(cursor.waiting)
        gap = events/names[14]; moved = self.root/'moved'; gap.rename(moved)
        text = self.logs()
        self.assertIn('warning: trace incomplete: sequence 15', text)
        self.assertNotIn('Finished', text)  # Events after the gap are not shown.
        def during(poll):
            if poll == 3:
                moved.rename(gap)  # The missing event appears.
        code, out, _ = self.watch(during, json_mode=True, last=100)
        records = self.ndjson(out)
        self.assertEqual([r['sequence'] for r in records if r['record'] == 'event'], list(range(1, 23)))
        self.assertEqual(len([r for r in records if r['record'] == 'warning']), 1)  # Bounded, while it waited.
        self.assertEqual(records[-1]['status'], 'READY_FOR_MANUAL_QA')
        gap.rename(moved)  # A gap that never resolves: reported, then the final status; still nothing skipped.
        code, out, sleeps = self.watch(json_mode=True, last=100)
        records = self.ndjson(out)
        self.assertEqual(([r['sequence'] for r in records if r['record'] == 'event'], len(sleeps)), (list(range(1, 15)), logs.GAP_PATIENCE+1))
        self.assertEqual([r['record'] for r in records[-2:]], ['warning', 'status'])
        moved.rename(gap); (events/'.tmp-1-000099-x.json').unlink()


class HumanWaitWatchTests(unittest.TestCase):
    _base = test_human_input.HumanInputTests
    setUp, setUp_base, git, save_config, start, answer = (_base.setUp, _base.setUp_base, _base.git, _base.save_config,
                                                          _base.start, _base.answer)

    # 10
    def test_a_human_wait_prints_the_next_action_and_exits(self):
        self.start()
        out = io.StringIO()
        self.assertEqual(logs.watch(self.work, out, sleep=lambda s: None, max_polls=5), 0)
        last = out.getvalue().splitlines()[-1]
        request = recovery_status(self.work)['human_input_request_id']
        self.assertEqual(last, 'STATUS WAITING_FOR_HUMAN | next: answer the clarification: switchyard work answer %s %s "<answer>"'
                         % (self.work, request))
        self.assertIn('Waiting     human input requested (%s)' % request, out.getvalue())
        self.answer()
        out = io.StringIO()
        logs.watch(self.work, out, sleep=lambda s: None, max_polls=5, last=100)
        self.assertIn('Answered    human input received', out.getvalue())


if __name__ == '__main__':
    unittest.main()

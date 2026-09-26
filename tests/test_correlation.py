"""Opaque run context across runtime, CLI and historical event streams."""
import contextlib
from dataclasses import FrozenInstanceError, asdict, fields, replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml
from flow_config import LoadedFlow
from runtime import (Event, ExecutionError, ExecutorResult, Flow, InvocationRequest,
                     InvocationResponse, RunContext, State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from switchyard.cli import main
from switchyard.observability import ObservabilityError, read_events, summarize_runs


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'events.jsonl'
        self.work = WorkUnit('literal task ; $HOME')
        self.flow = Flow('check', {'check': State('x', {'success': 'done'}),
                                   'done': State(terminal=True)})

    def execute(self, context=None, recorder=None, flow=None, adapter=None):
        adapter = adapter if adapter is not None else Mock(run=Mock(return_value=InvocationResponse(ExecutorResult('success'))))
        with patch('runtime._Clock.sample', return_value=(123, 500)):
            return run(self.work, flow or self.flow, {'x': adapter}, context=context, recorder=recorder)

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def process(self, *args):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        return subprocess.run([sys.executable, '-m', 'switchyard', *args], cwd=self.root,
                              env=env, capture_output=True, text=True, timeout=10)

    def test_no_context_and_all_context_combinations(self):
        for context in (None, RunContext(), RunContext('c'), RunContext(parent_run_id='external parent'), RunContext('c', 'p')):
            with self.subTest(context=context):
                result = self.execute(context)
                expected = context or RunContext()
                self.assertTrue(all((e.correlation_id, e.parent_run_id) ==
                                    (expected.correlation_id, expected.parent_run_id) for e in result.events))
                summary, = summarize_runs(result.events)
                self.assertEqual((summary.correlation_id, summary.parent_run_id),
                                 (expected.correlation_id, expected.parent_run_id))

    def test_context_is_immutable_and_has_only_two_fields(self):
        context = RunContext('c', 'p')
        self.assertEqual([f.name for f in fields(context)], ['correlation_id', 'parent_run_id'])
        with self.assertRaises(FrozenInstanceError):
            context.correlation_id = 'changed'
        self.assertFalse(hasattr(context, '__dict__'))

    def test_identifier_validation_no_coercion(self):
        class StringLike:
            def __str__(self):
                raise AssertionError('must not coerce')
        for name in ('correlation_id', 'parent_run_id'):
            for value in (True, 1, 1.5, [], {}, b'id', StringLike()):
                with self.subTest(name=name, value=type(value)), self.assertRaises(TypeError):
                    RunContext(**{name: value})
            for value in ('', 'secret\0value'):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError) as caught:
                    RunContext(**{name: value})
                self.assertNotIn('secret', str(caught.exception))
            for value in (' ', ' a:b/42 ', 'корреляция', 'line\nnext'):
                self.assertEqual(getattr(RunContext(**{name: value}), name), value)

    def test_invalid_context_rejected_before_any_side_effect(self):
        forged = object.__new__(RunContext)
        object.__setattr__(forged, 'correlation_id', '')
        object.__setattr__(forged, 'parent_run_id', None)
        class ExtendedContext(RunContext):
            pass
        adapter, recorder = Mock(), Mock()
        for context in ({}, 'c', True, ExtendedContext(), forged):
            with self.subTest(context=context), patch('runtime.uuid4') as identity, patch('runtime._Clock') as clock:
                with self.assertRaises((TypeError, ValueError)):
                    run(self.work, self.flow, {'x': adapter}, context=context, recorder=recorder)
                identity.assert_not_called()
                clock.assert_not_called()
                adapter.run.assert_not_called()
                recorder.record.assert_not_called()

    def test_context_never_changes_requests_order_or_timing(self):
        adapter = Mock(run=Mock(return_value=InvocationResponse(ExecutorResult('success'))))
        samples = [(1000 + i * 100, 500 + i * 10) for i in range(9)]
        with patch('runtime.uuid4', return_value='fixed'), patch('runtime._Clock.sample', side_effect=samples * 2) as clock:
            plain = run(self.work, self.flow, {'x': adapter})
            correlated = run(self.work, self.flow, {'x': adapter}, context=RunContext('c', 'unknown-parent'))
            self.assertEqual(clock.call_count, 18)
        self.assertEqual(correlated.events[-1].duration_ns, 80)
        self.assertEqual(tuple(replace(e, correlation_id=None, parent_run_id=None) for e in correlated.events), plain.events)
        request = adapter.run.call_args.args[0]
        self.assertIs(type(request), InvocationRequest)
        self.assertEqual([f.name for f in fields(request)], ['work_unit', 'diagnostics'])
        self.assertIs(request.work_unit, self.work)
        self.assertIsNone(request.diagnostics)
        self.assertEqual([f.name for f in fields(self.work)], ['task'])
        self.assertEqual(request.work_unit.task, 'literal task ; $HOME')

    def test_budget_and_failure_events_keep_context(self):
        context = RunContext('c', 'p')
        flow = Flow('check', {'check': State('x', max_state_visits_per_run=0), 'done': State(terminal=True)})
        for current, adapter in ((flow, Mock()), (self.flow, Mock(run=Mock(side_effect=ValueError('failed'))))):
            with self.subTest(flow=current), self.assertRaises(ExecutionError) as caught:
                self.execute(context, flow=current, adapter=adapter)
            self.assertEqual(caught.exception.events[-1].kind, 'run_error')
            self.assertTrue(all((e.correlation_id, e.parent_run_id) == ('c', 'p') for e in caught.exception.events))
            self.assertEqual(summarize_runs(caught.exception.events)[0].correlation_id, 'c')
        disk_error = OSError('recording failed')
        with self.assertRaises(ExecutionError) as caught:
            self.execute(context, recorder=Mock(record=Mock(side_effect=disk_error)))
        self.assertEqual(caught.exception.events[0].correlation_id, 'c')
        self.assertEqual(caught.exception.events[0].parent_run_id, 'p')
        self.assertIs(caught.exception.__cause__, disk_error)

    def test_followup_uses_completed_run_identity_and_keeps_runs_separate(self):
        recorder = JsonlRecorder(self.path)
        first = self.execute(RunContext('lifecycle'), recorder)
        parent = next(e.run_id for e in first.events if e.kind == 'run_started')
        second = self.execute(RunContext('lifecycle', parent), recorder)
        self.assertEqual(tuple(read_events(self.path)), first.events + second.events)
        a, b = summarize_runs(read_events(self.path))
        self.assertNotEqual(a.run_id, b.run_id)
        self.assertEqual((a.correlation_id, b.correlation_id), ('lifecycle', 'lifecycle'))
        self.assertEqual(b.parent_run_id, a.run_id)
        self.assertIsNone(a.parent_run_id)
        with self.assertRaises(FrozenInstanceError):
            b.parent_run_id = 'changed'

    def test_missing_parent_is_valid_and_interleaving_keeps_first_seen_order(self):
        first = self.execute(RunContext('c', 'absent-parent')).events
        second = self.execute(RunContext('c', 'another-absent-parent')).events
        interleaved = tuple(e for pair in zip(second, first) for e in pair)
        self.assertEqual(summarize_runs(interleaved), summarize_runs(second + first))
        self.assertEqual([s.parent_run_id for s in summarize_runs(interleaved)],
                         ['another-absent-parent', 'absent-parent'])

    def test_mixed_old_timed_correlated_appended_logs(self):
        records, expected = [], []
        for omitted, context in (({'occurred_at_unix_ns', 'duration_ns', 'correlation_id', 'parent_run_id'}, None),
                                 ({'correlation_id', 'parent_run_id'}, None)):
            events = self.execute(context).events
            records.extend({k: v for k, v in asdict(e).items() if k not in omitted} for e in events)
            expected.extend(replace(e, **{k: None for k in omitted}) for e in events)
        self.path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        prefix = self.path.read_bytes()
        third = self.execute(RunContext('c', 'external'), JsonlRecorder(self.path))
        self.assertTrue(self.path.read_bytes().startswith(prefix))
        self.assertEqual(tuple(read_events(self.path)), tuple(expected) + third.events)
        a, b, c = summarize_runs(read_events(self.path))
        self.assertEqual([s.correlation_id for s in (a, b, c)], [None, None, 'c'])
        self.assertEqual([s.run_duration_ns for s in (a, b, c)], [None, 0, 0])
        self.assertEqual(c.parent_run_id, 'external')

    def test_inconsistent_context_rejected_from_events_and_jsonl(self):
        for context in (None, RunContext('c', 'p')):
            events = self.execute(context).events
            for field in ('correlation_id', 'parent_run_id'):
                for position in (1, len(events)-1):
                    with self.subTest(context=context, field=field, position=position):
                        changed = list(events)
                        changed[position] = replace(changed[position], **{field: 'different'})
                        self.path.write_text(''.join(json.dumps(asdict(e)) + '\n' for e in changed))
                        for source in (changed, read_events(self.path)):
                            with self.assertRaisesRegex(ObservabilityError, 'inconsistent run correlation'):
                                summarize_runs(source)
        summary, = summarize_runs(self.execute(RunContext('c', 'p')).events[:3])
        self.assertEqual((summary.correlation_id, summary.parent_run_id), ('c', 'p'))
        self.assertFalse(summary.completed_normally)

    def test_identifier_validation_in_reader_and_direct_events(self):
        for field in ('correlation_id', 'parent_run_id'):
            for value in ('', 'bad\0id', 1, True, [], {}):
                event = replace(Event('run_started', 'r'), **{field: value})
                self.path.write_text(json.dumps(asdict(event)) + '\n')
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ObservabilityError):
                        tuple(read_events(self.path))
                    with self.assertRaises(ObservabilityError):
                        summarize_runs([event])
        record = asdict(Event('run_started', 'r'))
        record['metadata'] = {}
        self.path.write_text(json.dumps(record) + '\n')
        with self.assertRaises(ObservabilityError):
            tuple(read_events(self.path))

    def test_each_optional_correlation_field_can_be_omitted(self):
        for omitted in ((), ('correlation_id',), ('parent_run_id',), ('correlation_id', 'parent_run_id')):
            record = asdict(Event('run_started', 'r', correlation_id='c', parent_run_id='p'))
            for field in omitted:
                record.pop(field)
            self.path.write_text(json.dumps(record) + '\n')
            event, = read_events(self.path)
            self.assertEqual(event.correlation_id, None if 'correlation_id' in omitted else 'c')
            self.assertEqual(event.parent_run_id, None if 'parent_run_id' in omitted else 'p')

    def test_cli_context_passed_separately_and_task_unchanged(self):
        loaded = LoadedFlow(self.flow, {})
        result = self.execute()
        for extras, expected in (([], None), (['--correlation-id', ' c '], RunContext(' c ')),
                                 (['--parent-run-id', 'p'], RunContext(parent_run_id='p')),
                                 (['--correlation-id', 'c', '--parent-run-id', 'p'], RunContext('c', 'p'))):
            with self.subTest(extras=extras), patch('switchyard.cli.load_flow', return_value=loaded), patch('switchyard.cli.run', return_value=result) as execute:
                status, out, err = self.invoke(['run', 'f', '--task', self.work.task, *extras])
                self.assertEqual((status, err), (0, ''))
                self.assertEqual(execute.call_args.args[0], self.work)
                self.assertEqual(execute.call_args.kwargs, {} if expected is None else {'context': expected})
                self.assertEqual(len(out.splitlines()), 2)

    def test_cli_validation_rejects_flags_and_invalid_ids_before_side_effects(self):
        for flag in ('--correlation-id', '--parent-run-id'):
            with patch('switchyard.cli.load_flow') as load:
                with self.assertRaises(SystemExit) as caught:
                    self.invoke(['validate', 'f', flag, 'value'])
                self.assertEqual(caught.exception.code, 2)
                load.assert_not_called()
            for value in ('', 'secret\0value'):
                with patch('switchyard.cli.load_flow', return_value=LoadedFlow(self.flow, {})), patch('switchyard.cli.run') as execute, patch('switchyard.cli.JsonlRecorder') as recorder, patch('switchyard.cli.WorkUnit') as work:
                    self.assertEqual(self.invoke(['run', 'f', '--task', 'secret task', flag, value, '--events-file', str(self.path)]),
                                     (2, '', 'switchyard: invalid run context\n'))
                    execute.assert_not_called()
                    recorder.assert_not_called()
                    work.assert_not_called()
                self.assertFalse(self.path.exists())

    def test_real_cli_context_recording_and_validation(self):
        file = self.root / 'flow.yaml'
        file.write_text(yaml.safe_dump({'version': 1, 'initial': 'done', 'executors': {},
                                        'states': {'done': {'terminal': True}}}))
        result = self.process('run', str(file), '--task', 'private task', '--events-file', 'events.jsonl',
                              '--correlation-id', 'private correlation', '--parent-run-id', 'external-parent')
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertNotIn('private', result.stdout)
        events = tuple(read_events(self.path))
        self.assertTrue(all((e.correlation_id, e.parent_run_id) == ('private correlation', 'external-parent') for e in events))
        prefix = self.path.read_bytes()
        for flag in ('--correlation-id', '--parent-run-id'):
            result = self.process('validate', str(file), flag, 'c')
            self.assertEqual(result.returncode, 2)
            result = self.process('run', str(file), '--task', 't', flag, '', '--events-file', 'events.jsonl')
            self.assertEqual((result.returncode, result.stdout, result.stderr), (2, '', 'switchyard: invalid run context\n'))
            self.assertEqual(self.path.read_bytes(), prefix)

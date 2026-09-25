import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import tracemalloc
import unittest
from unittest.mock import Mock, patch

from runtime import (ExecutionError, ExecutorResult, Flow, InvocationRequest,
                     InvocationResponse, State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from runtime.command import (Command, CommandExecutor, _capture, _diagnostic,
                             _read_chunk, _retain, _INTERRUPTED)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = WorkUnit('original task')

    def executor(self, code='pass', cap=32, timeout=5):
        return CommandExecutor(Command(sys.executable, ('-c', code), self.tmp.name, timeout, cap))

    def execute(self, code='pass', cap=32, timeout=5):
        return self.executor(code, cap, timeout).run(InvocationRequest(self.work))

    def process(self):
        p = Mock(returncode=0)
        p.poll.return_value = 0
        p.stdout.fileno.return_value = 42
        return p

    def test_configuration_and_defaults(self):
        for cap in (True, False, 1.0, '1'):
            with self.assertRaises(TypeError): self.executor(cap=cap)
        for cap in (-1, 4097):
            with self.assertRaises(ValueError): self.executor(cap=cap)
        self.assertIsNone(self.execute(cap=None).diagnostics)
        for cap in (0, 4096):
            self.assertEqual(self.execute(cap=cap).diagnostics.utf8, b'')
        with patch('runtime.command.os.name', 'nt'), patch('runtime.command.sys.version_info', (3, 11)), patch('runtime.command.subprocess.Popen') as popen:
            with self.assertRaises(NotImplementedError): self.execute()
            popen.assert_not_called()

    def test_launch_error(self):
        command = Command(str(Path(self.tmp.name)/'absent'), (), self.tmp.name, 2, 10)
        response = CommandExecutor(command).run(InvocationRequest(self.work))
        self.assertEqual(response.result.outcome, 'error')
        self.assertIsNone(response.diagnostics)

    def test_exact_utf8_and_chunk_independence(self):
        cases = [(b'abcdef', 4, b'cdef'), (b'abcd', 4, b'abcd'), (b'abcde', 4, b'bcde'),
                 ('Aé'.encode(), 3, 'Aé'.encode()), ('Aé'.encode(), 2, 'é'.encode()),
                 ('Aé'.encode(), 1, b''), (b'A\xff', 4, 'A�'.encode()),
                 (b'A\xff', 3, '�'.encode()), (b'A\xff', 2, b''), (b'abcdef', 0, b'')]
        for raw, cap, expected in cases:
            for size in (1, 2, 4096):
                tail = b''
                for i in range(0, len(raw), size): tail = _retain(tail, raw[i:i+size], cap)
                self.assertLessEqual(len(tail), cap)
                self.assertEqual(_diagnostic(tail, cap).utf8, expected)
            actual = self.execute(f'import os; os.write(1, {raw!r})', cap).diagnostics.utf8
            self.assertEqual(actual, expected)
            actual.decode('utf-8')

    def test_outcomes_and_merge(self):
        for code, outcome in ((0, 'success'), (7, 'failed')):
            result = self.execute(f'import os; os.write(1,b"next_state=accepted"); os.write(2,b"ERROR"); exit({code})')
            self.assertEqual(result.result.outcome, outcome)
            self.assertEqual(result.diagnostics.utf8, b'next_state=acceptedERROR')

    def test_large_output_independent_watchdog(self):
        # Separate harness deadline also catches a broken executor timeout loop.
        script = '''
import os, sys
from runtime import InvocationRequest, WorkUnit
from runtime.command import Command, CommandExecutor
import runtime.command as c
high = [0, 0]
original_read, original_retain = c._read_chunk, c._retain
def read(fd):
    chunk = original_read(fd)
    if isinstance(chunk, bytes): high[1] = max(high[1], len(chunk))
    return chunk
def retain(tail, chunk, cap):
    result = original_retain(tail, chunk, cap)
    high[0] = max(high[0], len(result))
    return result
c._read_chunk, c._retain = read, retain
code = "import os; chunk=b'x'*4096; " + "[(os.write(1,chunk),os.write(2,chunk)) for _ in range(1024)]; os.write(2,b'END'); exit(7)"
for cap in (4096, 0):
    high[:] = [0, 0]
    response = CommandExecutor(Command(sys.executable, ('-c',code), os.getcwd(), 10, cap)).run(InvocationRequest(WorkUnit('t')))
    assert response.result.outcome == 'failed'
    assert response.diagnostics.utf8 == (b'x'*4093+b'END' if cap else b'')
    assert high == [cap, 4096], high
    print('8 MiB capture: cap=%d retained=%d read=%d PASS' % (cap,*high))
'''
        completed = subprocess.run([sys.executable, '-c', script], timeout=25, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        print(completed.stdout.strip())

    def test_allocation_bound(self):
        peaks = []
        chunk = b'x'*4096
        for count in (256, 4096):
            tracemalloc.start()
            tail = b''
            for _ in range(count): tail = _retain(tail, chunk, 4096)
            _diagnostic(tail, 4096)
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
        self.assertLess(max(peaks), 65536)
        self.assertLess(abs(peaks[1]-peaks[0]), 4096)
        print('Retention/normalization traced peaks (1/16 MiB):', peaks)

    def test_read_classification(self):
        for value in (b'x', b''):
            with patch('runtime.command.os.read', return_value=value) as read:
                self.assertEqual(_read_chunk(1), value)
                read.assert_called_once_with(1, 4096)
        for exc, expected in ((BlockingIOError(), None), (OSError(errno.EAGAIN, 'again'), None),
                              (InterruptedError(), _INTERRUPTED), (OSError(errno.EINTR, 'interrupt'), _INTERRUPTED)):
            with patch('runtime.command.os.read', side_effect=exc): self.assertIs(_read_chunk(1), expected)
        with patch('runtime.command.os.read', side_effect=OSError(errno.EIO, 'read failed')):
            with self.assertRaises(OSError): _read_chunk(1)

    def test_final_drain_bound_and_close(self):
        for value in (b'x'*4096, _INTERRUPTED):
            p = self.process()
            with patch('runtime.command.os.set_blocking'), patch('runtime.command._read_chunk', return_value=value) as read, patch('runtime.command.sleep') as sleep:
                response = _capture(p, time.monotonic()+5, 0)
            self.assertEqual(read.call_count, 256)
            sleep.assert_not_called()
            p.wait.assert_called_once()
            p.stdout.close.assert_called_once()
            self.assertEqual(response.diagnostics.utf8, b'')

    def test_creation_timeout_and_failure_precedence(self):
        for failure in (None, 'setup', 'read', 'normalization', 'close'):
            p = self.process()
            p.poll.return_value = None
            bug = RuntimeError('capture bug')
            with patch('runtime.command.subprocess.Popen', return_value=p), patch('runtime.command.monotonic', side_effect=[0, 10]), patch('runtime.command.os.set_blocking', side_effect=bug if failure == 'setup' else None), patch('runtime.command._read_chunk', side_effect=bug if failure == 'read' else None, return_value=b''), patch('runtime.command._diagnostic', side_effect=bug if failure == 'normalization' else None, wraps=_diagnostic):
                if failure == 'close': p.stdout.close.side_effect = bug
                if failure:
                    with self.assertRaises(RuntimeError) as caught: self.execute()
                    self.assertIs(caught.exception, bug)
                else:
                    self.assertEqual(self.execute().result.outcome, 'timeout')
            p.kill.assert_called()
            p.wait.assert_called()

    def test_capture_and_cleanup_failure_primary(self):
        p = self.process()
        p.poll.return_value = None
        bug, cleanup = RuntimeError('capture bug'), OSError('cleanup bug')
        p.wait.side_effect = cleanup
        flow = Flow('verify', {'verify': State('v', {'success': 'done'}), 'done': State(terminal=True)})
        with patch('runtime.command.subprocess.Popen', return_value=p), patch('runtime.command.os.set_blocking', side_effect=bug):
            with self.assertRaises(ExecutionError) as caught: run(self.work, flow, {'v': self.executor()})
        self.assertIs(caught.exception.__cause__, bug)
        self.assertIs(bug.__cause__, cleanup)
        self.assertEqual(caught.exception.events[-1].kind, 'run_error')
        self.assertNotIn('transition_selected', [e.kind for e in caught.exception.events])
        p.stdout.close.assert_called_once()

    def test_timeout_direct_process_reaped_and_early_eof(self):
        real_popen = subprocess.Popen
        for code in ('import os,time; os.close(1); os.close(2); time.sleep(30)',
                     'import os\nwhile True: os.write(1,b"x"*4096)'):
            processes = []
            def start(*args, **kwargs):
                p = real_popen(*args, **kwargs); processes.append(p); return p
            with patch('runtime.command.subprocess.Popen', side_effect=start):
                result = self.execute(code, timeout=0.15)
            self.assertEqual(result.result.outcome, 'timeout')
            self.assertIsNotNone(processes[0].returncode)
            self.assertTrue(processes[0].stdout.closed)
            self.assertLessEqual(len(result.diagnostics.utf8), 32)

    @unittest.skipUnless(os.name == 'posix', 'descendant cleanup harness uses POSIX signals')
    def test_descendant_stream_normal_exit_and_timeout(self):
        for writing in (False, True):
            for timeout in (False, True):
                pidfile = Path(self.tmp.name)/'pid'
                pidfile.unlink(missing_ok=True)
                child = 'import time; time.sleep(20)' if not writing else 'import os,time\nwhile True:\n try: os.write(1,b"x"*4096)\n except BrokenPipeError: time.sleep(20)'
                parent = f'import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,"-c",{child!r}]); Path({str(pidfile)!r}).write_text(str(p.pid)); ' + ('time.sleep(20)' if timeout else '')
                start = time.monotonic()
                try:
                    response = self.execute(parent, timeout=0.4 if timeout else 3)
                    self.assertLess(time.monotonic()-start, 5)
                    self.assertEqual(response.result.outcome, 'timeout' if timeout else 'success')
                    os.kill(int(pidfile.read_text()), 0)
                finally:
                    if pidfile.exists():
                        try: os.kill(int(pidfile.read_text()), signal.SIGKILL)
                        except ProcessLookupError: pass

    def test_real_handoff_provenance(self):
        requests = []
        class Consumer:
            def run(self, request):
                requests.append(request)
                return InvocationResponse(ExecutorResult('success'))
        flow = Flow('verify', {
            'verify': State('v', {'failed': 'fix', 'success': 'accepted'}),
            'fix': State('f', {'success': 'accepted'}, diagnostics_from_previous_max_bytes=5),
            'accepted': State(terminal=True)})
        log = Path(self.tmp.name)/'events.jsonl'
        result = run(self.work, flow, {'v': self.executor('import os; os.write(1,b"start"); os.write(2,b"ERROR"); exit(7)', cap=5), 'f': Consumer()}, recorder=JsonlRecorder(str(log)))
        self.assertNotIn('ERROR', log.read_text())
        self.assertNotIn('diagnostic', log.read_text())
        request = requests[0]
        self.assertIs(request.work_unit, self.work)
        self.assertEqual(request.diagnostics.payload.utf8, b'ERROR')
        event = next(e for e in result.events if e.kind == 'invocation_finished')
        origin = request.diagnostics.origin
        self.assertEqual((origin.run_id, origin.state, origin.state_visit_id, origin.invocation_id),
                         (event.run_id, event.state, event.state_visit_id, event.invocation_id))
        self.assertEqual(result.terminal_state, 'accepted')

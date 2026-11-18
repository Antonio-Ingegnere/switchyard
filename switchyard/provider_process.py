"""Shared bounded process transport for V2 stage adapters, not a workflow engine."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import codecs
import os
from pathlib import Path
import subprocess
import signal
from tempfile import TemporaryFile
from time import monotonic, sleep

from runtime import DiagnosticPayload
from runtime.command import Command, _stop_and_wait, _read_chunk, _INTERRUPTED, _diagnostic


class ProviderExecutionError(RuntimeError):
    def __init__(self, message, diagnostics=None, returncode=None):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.returncode = returncode  # Provider nonzero exit status; None for other failures.


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    evidence: str | None
    diagnostics: DiagnosticPayload


class InvocationFacts:
    """Observational facts about ONE invocation's provider process (#1 diagnostics), bounded while streaming.

    Execution never reads them. No stream content is kept: only the byte count of the separate stderr stream.
    """
    def __init__(self):
        self.process = None       # dict of process metadata for the (last) launched process
        self.launches = 0
        self.stderr_bytes = 0
        self.channel = None       # adapter: documented structured provider fields only
        self.structured = None    # adapter: structured-output contract facts
        self.result = None        # the StageResult outcome, when the adapter returned one
        self.error = None         # the exception raised by the adapter, when it raised
        self.usage = None         # adapter: normalized provider-reported token usage (ECONOMICS-001)
        self.prompt = None        # adapter: measured composition of the prepared prompt, numbers only (ECONOMICS-002)

    def note(self, **fields):
        if self.process is not None:
            self.process.update(fields)


_FACTS = ContextVar('switchyard_invocation_facts', default=None)


@contextmanager
def observe_invocation():
    """Collect InvocationFacts for the adapter call in this context (observation only)."""
    facts = InvocationFacts()
    token = _FACTS.set(facts)
    try:
        yield facts
    finally:
        _FACTS.reset(token)


def note_invocation(update):
    """Apply `update(facts)` to the current InvocationFacts; any failure is swallowed (never execution)."""
    facts = _FACTS.get()
    if facts is not None:
        try:
            update(facts)
        except Exception:
            pass


def _begin(config, combined):
    def start(facts):
        facts.launches += 1
        facts.process = dict(launched=False, exit_code=None, termination_signal=None, terminated_by=None,
                             timed_out=False, timeout_seconds=config.timeout_seconds, duration_ms=None,
                             stderr_stream='combined_with_stdout' if combined else 'separate')
        facts.stderr_bytes = 0
    note_invocation(start)


def _stderr(data):
    def keep(facts):
        facts.stderr_bytes += len(data)
    note_invocation(keep)


def _signal_name(number):
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


class _Evidence:
    def __init__(self, limit):
        self.limit = limit - 1
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.tail = b''
        self.truncated = False

    def feed(self, data, final=False):
        encoded = self.decoder.decode(data, final=final).encode('utf-8')
        combined = self.tail + encoded
        self.truncated |= len(combined) > self.limit
        self.tail = combined[-self.limit:] if self.limit else b''
        self.tail = self.tail.decode('utf-8', errors='ignore').encode('utf-8')

    def finish(self):
        self.feed(b'', final=True)
        return ('!' if self.truncated else '=') + self.tail.decode('utf-8')


def execute(executable, argv, config, *, input_bytes=b'', capture=None,
            result_stdout=False, result_file=None, environment=None, stdout_observer=None):
    """One process. Full semantic stdout or bounded evidence/log tails, never retries.

    `stdout_observer(chunk)`: observational telemetry fed every stdout chunk as read (never a result channel). It must
    keep its own bounded state; an exception detaches it and never changes the process outcome or the draining."""
    Command(executable, tuple(argv), config.working_directory, config.timeout_seconds)
    cap = config.max_result_bytes
    evidence = _Evidence(capture.max_bytes) if capture else None
    combined = capture is not None and capture.stream == 'combined'
    full = bytearray()
    diagnostic = b''
    deadline = monotonic() + config.timeout_seconds
    _begin(config, combined)
    with TemporaryFile() as stdin:
        stdin.write(input_bytes); stdin.seek(0)
        started = monotonic()
        try:
            process = subprocess.Popen([executable, *argv], shell=False,
                                       cwd=config.working_directory, env=dict(environment or config.environment),
                                       stdin=stdin, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if combined else subprocess.PIPE, bufsize=0,
                                       start_new_session=(os.name == 'posix'))
        except OSError as exc:
            raise ProviderExecutionError(f'cannot start executable {executable!r}') from exc
        note_invocation(lambda facts: facts.note(launched=True))
        streams = {process.stdout: 'stdout'}
        if process.stderr is not None:
            streams[process.stderr] = 'stderr'
        try:
            if config.process_observer is not None:
                config.process_observer('started', process.pid)
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
            pending = dict(streams)
            while pending or process.poll() is None:
                if monotonic() >= deadline:
                    note_invocation(lambda facts: facts.note(timed_out=True, terminated_by='switchyard_timeout',
                                                             termination_signal='SIGKILL'))
                    raise ProviderExecutionError('process timeout', _diagnostic(diagnostic, 4096))
                if result_file is not None and Path(result_file).exists() and Path(result_file).stat().st_size > cap:
                    raise ProviderExecutionError('structured result exceeds byte limit')
                progress = False
                for stream, name in tuple(pending.items()):
                    data = _read_chunk(stream.fileno())
                    if data is _INTERRUPTED or data is None:
                        continue
                    if not data:
                        del pending[stream]
                        continue
                    progress = True
                    if name == 'stderr' or combined:
                        diagnostic = (diagnostic + data)[-4096:]
                    if name == 'stderr':
                        _stderr(data)
                    if name == 'stdout' and result_stdout:
                        if len(full) + len(data) > cap:
                            raise ProviderExecutionError('structured stdout exceeds byte limit')
                        full.extend(data)
                    if name == 'stdout' and stdout_observer is not None:
                        try:
                            stdout_observer(data)
                        except Exception:
                            stdout_observer = None  # Telemetry only: detached, the stream keeps draining.
                    if evidence and (combined or capture.stream == name):
                        evidence.feed(data)
                if not progress:
                    sleep(min(0.005, max(0, deadline - monotonic())))
            code = process.wait()
            if code < 0:
                note_invocation(lambda facts: facts.note(termination_signal=_signal_name(-code), terminated_by='signal'))
                raise ProviderExecutionError(f'process terminated by signal {-code}', _diagnostic(diagnostic,4096))
            note_invocation(lambda facts: facts.note(exit_code=code))
            return ProcessOutput(code, bytes(full), evidence.finish() if evidence else None,
                                 _diagnostic(diagnostic, 4096))
        except OSError as exc:
            raise ProviderExecutionError('process I/O failure', _diagnostic(diagnostic, 4096)) from exc
        finally:
            try:
                # Reap the leader first: a zombie-only group can reject killpg
                # with EPERM on macOS. Remaining tool children retain the group.
                _stop_and_wait(process)
                elapsed = int((monotonic() - started) * 1000)
                note_invocation(lambda facts: facts.note(duration_ms=elapsed))
                if os.name == 'posix':
                    try:os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:pass
                if config.process_observer is not None:
                    config.process_observer('terminated', process.pid)
            finally:
                for stream in streams:
                    stream.close()

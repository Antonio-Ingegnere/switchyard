"""Shared bounded process transport for V2 stage adapters, not a workflow engine."""
from dataclasses import dataclass
import codecs
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryFile
from time import monotonic, sleep

from runtime import DiagnosticPayload
from runtime.command import Command, _stop_and_wait, _read_chunk, _INTERRUPTED, _diagnostic


class ProviderExecutionError(RuntimeError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    evidence: str | None
    diagnostics: DiagnosticPayload


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
            result_stdout=False, result_file=None, environment=None):
    """One process. Full semantic stdout or bounded evidence/log tails, never retries."""
    Command(executable, tuple(argv), config.working_directory, config.timeout_seconds)
    cap = config.max_result_bytes
    evidence = _Evidence(capture.max_bytes) if capture else None
    combined = capture is not None and capture.stream == 'combined'
    full = bytearray()
    diagnostic = b''
    deadline = monotonic() + config.timeout_seconds
    with TemporaryFile() as stdin:
        stdin.write(input_bytes); stdin.seek(0)
        try:
            process = subprocess.Popen([executable, *argv], shell=False,
                                       cwd=config.working_directory, env=dict(environment or config.environment),
                                       stdin=stdin, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if combined else subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise ProviderExecutionError(f'cannot start executable {executable!r}') from exc
        streams = {process.stdout: 'stdout'}
        if process.stderr is not None:
            streams[process.stderr] = 'stderr'
        try:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
            pending = dict(streams)
            while pending or process.poll() is None:
                if monotonic() >= deadline:
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
                    if name == 'stdout' and result_stdout:
                        if len(full) + len(data) > cap:
                            raise ProviderExecutionError('structured stdout exceeds byte limit')
                        full.extend(data)
                    if evidence and (combined or capture.stream == name):
                        evidence.feed(data)
                if not progress:
                    sleep(min(0.005, max(0, deadline - monotonic())))
            code = process.wait()
            if code < 0:
                raise ProviderExecutionError(f'process terminated by signal {-code}', _diagnostic(diagnostic,4096))
            return ProcessOutput(code, bytes(full), evidence.finish() if evidence else None,
                                 _diagnostic(diagnostic, 4096))
        except OSError as exc:
            raise ProviderExecutionError('process I/O failure', _diagnostic(diagnostic, 4096)) from exc
        finally:
            try:
                _stop_and_wait(process)
            finally:
                for stream in streams:
                    stream.close()

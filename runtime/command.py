"""Direct command execution; no workflow or budget knowledge."""

from dataclasses import dataclass
import errno
import math
import os
import subprocess
import sys
from time import monotonic, sleep

from . import DiagnosticPayload, ExecutorResult, InvocationRequest, InvocationResponse


@dataclass(frozen=True, slots=True)
class Command:
    executable: str
    argv: tuple[str, ...]
    cwd: str
    invocation_timeout_seconds: float
    diagnostic_capture_max_bytes: int | None = None

    def __post_init__(self):
        cap = self.diagnostic_capture_max_bytes
        if cap is not None:
            if type(cap) is not int:
                raise TypeError("diagnostic capture limit must be an int, excluding bool")
            if not 0 <= cap <= 4096:
                raise ValueError("diagnostic capture limit must be between 0 and 4096")
        for name in ("executable", "cwd"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a string")
            if not value or "\0" in value or not os.path.isabs(value):
                raise ValueError(f"{name} must be an absolute path without NUL")
        if type(self.argv) is not tuple or any(type(arg) is not str for arg in self.argv):
            raise TypeError("argv must be a tuple of strings")
        if any("\0" in arg for arg in self.argv):
            raise ValueError("argv must not contain NUL")
        timeout = self.invocation_timeout_seconds
        if type(timeout) not in (int, float):
            raise TypeError("invocation timeout must be an int or float, excluding bool")
        try:
            valid = timeout > 0 and math.isfinite(timeout)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("invocation timeout must be positive and finite")
        if os.name == "nt" and self.executable.lower().endswith((".bat", ".cmd")):
            raise ValueError("Windows batch executables are not supported")


def _stop_and_wait(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            # It may have exited between poll and kill; wait still confirms exit.
            pass
    process.wait()


_INTERRUPTED = object()

def _read_chunk(fd):
    """None is a transient no-data condition; empty bytes are EOF."""
    try:
        return os.read(fd, 4096)
    except InterruptedError:
        return _INTERRUPTED
    except BlockingIOError:
        return None
    except OSError as exc:
        if exc.errno == errno.EINTR:
            return _INTERRUPTED
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return None
        raise


def _retain(tail, chunk, cap):
    if cap == 0:  # [-0:] would retain the entire chunk.
        return b""
    return (tail + chunk)[-cap:]


def _diagnostic(tail, cap):
    if cap == 0:
        return DiagnosticPayload(b"")
    encoded = tail.decode("utf-8", errors="replace").encode("utf-8")[-cap:]
    # Only the leading character can be split: encoded was valid UTF-8.
    return DiagnosticPayload(encoded.decode("utf-8", errors="ignore").encode("utf-8"))


def _capture(process, deadline, cap):
    cleanup_attempted = False
    close_attempted = False
    tail = b""
    try:
        timed_out = deadline - monotonic() <= 0
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        eof = False
        while not timed_out:
            if process.poll() is not None:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                timed_out = True
                break
            chunk = None if eof else _read_chunk(fd)
            if chunk is _INTERRUPTED:
                continue
            if chunk == b"":
                eof = True
            elif chunk is not None:
                tail = _retain(tail, chunk, cap)
            if chunk is None or eof:
                sleep(min(0.005, max(0, deadline - monotonic())))
        cleanup_attempted = True
        if timed_out:
            _stop_and_wait(process)
        else:
            process.wait()
        for _ in range(256):
            chunk = _read_chunk(fd)
            if chunk is _INTERRUPTED:
                continue
            if not chunk:
                break
            tail = _retain(tail, chunk, cap)
        diagnostic = _diagnostic(tail, cap)
        close_attempted = True
        process.stdout.close()
        outcome = "timeout" if timed_out else ("success" if process.returncode == 0 else "failed")
        return InvocationResponse(ExecutorResult(outcome), diagnostic)
    except BaseException as primary:
        cleanup_error = None
        try:
            if not cleanup_attempted:
                _stop_and_wait(process)
        except BaseException as exc:
            cleanup_error = exc
        try:
            if not close_attempted:
                process.stdout.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            raise primary from cleanup_error
        raise


@dataclass(frozen=True, slots=True)
class CommandExecutor:
    command: Command

    def __post_init__(self):
        if type(self.command) is not Command:
            raise TypeError("CommandExecutor requires a Command")

    def run(self, request: InvocationRequest) -> InvocationResponse:
        if type(request) is not InvocationRequest:
            raise TypeError("expected InvocationRequest")
        if request.diagnostics is not None:
            raise ValueError("this executor does not consume diagnostics")
        command = self.command
        args = [command.executable, *command.argv]
        cap = command.diagnostic_capture_max_bytes
        if cap is not None and os.name == "nt" and sys.version_info < (3, 12):
            raise NotImplementedError("diagnostic capture requires Windows Python 3.12+")
        deadline = monotonic() + command.invocation_timeout_seconds
        io = {} if cap is None else {"bufsize": 0}
        try:
            process = subprocess.Popen(
                args, shell=False, cwd=command.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL if cap is None else subprocess.PIPE,
                stderr=subprocess.DEVNULL if cap is None else subprocess.STDOUT, **io,
            )
        except OSError:
            return InvocationResponse(ExecutorResult("error"))

        if cap is not None:
            return _capture(process, deadline, cap)

        try:
            remaining = deadline - monotonic()
            if remaining > 0:
                returncode = process.wait(timeout=remaining)
                return InvocationResponse(ExecutorResult("success" if returncode == 0 else "failed"))
        except subprocess.TimeoutExpired:
            pass
        except BaseException:
            _stop_and_wait(process)
            raise

        # Outside the wait's exception handler: cleanup failures escape, without
        # another cleanup attempt or a false normalized timeout result.
        _stop_and_wait(process)
        return InvocationResponse(ExecutorResult("timeout"))

"""Owned subprocesses with bounded JSON, diagnostics, cancellation and deadlines."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from threading import Event
from types import MappingProxyType
from typing import cast

REQUEST_MAX_BYTES = 8 * 1024 * 1024
STDERR_MAX_BYTES = 64 * 1024
WORKER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
WORKER_CPU_SECONDS = 300
JOB_REQUEST_LIMITS = MappingProxyType({
    "inspect": REQUEST_MAX_BYTES, "cover": REQUEST_MAX_BYTES, "convert": REQUEST_MAX_BYTES,
    "http": 64 * 1024 * 1024,
})
Cancellation = Event | Callable[[], bool] | None


class WorkerError(Exception):
    def __init__(self, reason: str, status: str = 'invalid_input', *, stderr: bytes = b'', started: bool = True):
        self.reason = reason
        self.status = status
        self.stderr = stderr[:STDERR_MAX_BYTES]
        self.started = started
        super().__init__(reason)


def cancellation_requested(cancel: Cancellation) -> bool:
    return cancel.is_set() if isinstance(cancel, Event) else bool(cancel and cancel())


def _wait_without_reaping(process: subprocess.Popen[bytes], deadline: float, cancel: Cancellation) -> int:
    while True:
        if cancellation_requested(cancel):
            raise WorkerError("shutdown_requested", "cancelled")
        observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if observed is not None:
            return observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status
        if time.monotonic() >= deadline:
            raise WorkerError("worker_deadline", "failed")
        time.sleep(.01)


def _stop_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)
        raise WorkerError("worker_descendants_not_reaped", "failed") from None


def run_worker(
    kind: str, request: dict[str, object], *, timeout: float = 300,
    max_output_bytes: int = 192 * 1024 * 1024, cancel: Cancellation = None,
) -> dict[str, object]:
    if kind not in JOB_REQUEST_LIMITS:
        raise WorkerError('unknown_worker_job', started=False)
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError('worker limits must be positive')
    process: subprocess.Popen[bytes] | None = None
    try:
        encoded = json.dumps(request, ensure_ascii=False).encode('utf-8')
        if len(encoded) > JOB_REQUEST_LIMITS[kind]:
            raise WorkerError('worker_request_limit')
        if cancellation_requested(cancel):
            raise WorkerError('shutdown_requested', 'cancelled')
        if sys.platform != "linux":
            raise WorkerError("worker_resource_limits_require_linux", "unsupported")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("KCE_")}
        environment.update({'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'})
        process = subprocess.Popen(
            [sys.executable, '-I', '-m', 'komga_cover_extractor.infrastructure.jobs', '--supervise', kind], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=environment,
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        output = bytearray()
        diagnostics = bytearray()
        deadline = time.monotonic() + min(timeout, WORKER_CPU_SECONDS)
        view = memoryview(encoded)
        sent = 0
        with selectors.DefaultSelector() as selector:
            for stream, events, label in (
                (process.stdin, selectors.EVENT_WRITE, 'input'),
                (process.stdout, selectors.EVENT_READ, 'output'),
                (process.stderr, selectors.EVENT_READ, 'error'),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, label)
            while selector.get_map():
                if cancellation_requested(cancel):
                    raise WorkerError('shutdown_requested', 'cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError('worker_deadline', 'failed')
                for key, _ in selector.select(min(remaining, .1)):
                    descriptor = key.fd
                    if key.data == 'input':
                        try:
                            sent += os.write(descriptor, view[sent:sent + 65536])
                        except BrokenPipeError:
                            sent = len(view)
                        if sent == len(view):
                            selector.unregister(descriptor)
                            process.stdin.close()
                        continue
                    try:
                        chunk = os.read(descriptor, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(descriptor)
                    elif key.data == 'output':
                        if len(output) + len(chunk) > max_output_bytes:
                            raise WorkerError('worker_output_limit')
                        output.extend(chunk)
                    else:
                        diagnostics.extend(chunk[:max(0, STDERR_MAX_BYTES - len(diagnostics))])
            return_code = _wait_without_reaping(process, deadline, cancel)
        if not output:
            raise WorkerError('worker_terminated' if return_code else 'missing_worker_result', 'failed', stderr=bytes(diagnostics))
        try:
            result: object = json.loads(output)
        except (ValueError, UnicodeError):
            raise WorkerError('invalid_worker_result', 'failed', stderr=bytes(diagnostics)) from None
        if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
            raise WorkerError('invalid_worker_result', 'failed')
        response = cast(dict[str, object], result)
        if return_code != 0 or response.get('status') not in {'valid', 'absent'}:
            status = response.get('status')
            reason = response.get('reason')
            raise WorkerError(str(reason) if isinstance(reason, str) else 'worker_failed',
                              str(status) if status in {'invalid_input', 'unsupported', 'missing_dependency', 'cancelled', 'failed', 'permission_denied', 'stale_plan', 'review_required', 'conflict', 'ambiguous', 'no_match'} else 'failed',
                              stderr=bytes(diagnostics))
        return response
    except WorkerError as error:
        error.started = process is not None
        raise
    except Exception:
        raise WorkerError('worker_failed', 'failed', started=process is not None) from None
    finally:
        if process is not None:
            try:
                _stop_and_reap(process)
            finally:
                for owned_stream in (process.stdin, process.stdout, process.stderr):
                    if owned_stream is not None:
                        owned_stream.close()







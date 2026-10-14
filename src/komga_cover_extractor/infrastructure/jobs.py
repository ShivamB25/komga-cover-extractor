"""Supervise fixed jobs and dispatch them in a resource-limited decoder process."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import cast

from .workers import (
    JOB_REQUEST_LIMITS,
    WORKER_CPU_SECONDS,
    WORKER_MEMORY_BYTES,
    WorkerError,
)


def _dispatch(kind: str, request: dict[str, object]) -> dict[str, object]:
    if kind == 'inspect':
        if request.get('artwork_image') is True:
            from ..application.artwork_workflow import artwork_image_job
            return artwork_image_job(request)
        from .xml_metadata import inspect_book_job
        return inspect_book_job(request)
    if kind == 'cover':
        from ..application.extract_covers import cover_job
        return cover_job(request)
    if kind == 'convert':
        from ..application.convert import conversion_job
        return conversion_job(request)
    if kind == 'http':
        from ..integrations.http import http_job
        return http_job(request)
    raise WorkerError('unknown_worker_job')


def _main() -> int:
    try:
        if sys.platform != 'linux':
            raise WorkerError('worker_resource_limits_require_linux', 'unsupported')
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        kind = sys.argv[1] if len(sys.argv) == 2 else ""
        if kind not in JOB_REQUEST_LIMITS:
            raise WorkerError('unknown_worker_job')
        request_limit = JOB_REQUEST_LIMITS[kind]
        encoded = sys.stdin.buffer.read(request_limit + 1)
        if len(encoded) > request_limit:
            raise WorkerError('worker_request_limit')
        request: object = json.loads(encoded)
        if not isinstance(request, dict) or not all(isinstance(key, str) for key in request):
            raise WorkerError('invalid_worker_request')
        response = _dispatch(kind, cast(dict[str, object], request))
        sys.stdout.write(json.dumps(response, ensure_ascii=False))
        return 0
    except WorkerError as error:
        response = {'status': error.status, 'reason': error.reason}
    except ModuleNotFoundError as error:
        extras = {"PIL": "covers", "lxml": "covers", "numpy": "image-similarity",
                  "cv2": "image-similarity", "skimage": "image-similarity",
                  "filetype": "conversion", "py7zr": "conversion", "rarfile": "conversion",
                  "requests": "integrations"}
        extra = extras.get((error.name or "").split(".")[0])
        response = {"status": "missing_dependency" if extra else "failed",
                    "reason": f"{extra}_extra_required" if extra else "worker_import_error"}
    except MemoryError:
        response = {'status': 'invalid_input', 'reason': 'worker_memory_limit'}
    except PermissionError:
        response = {'status': 'permission_denied', 'reason': 'archive_permission_denied'}
    except Exception as error:
        response = {'status': 'failed', 'reason': f'worker_error:{type(error).__name__}'}
    sys.stdout.write(json.dumps(response))
    return 1


def _owned_children() -> tuple[int, ...]:
    path = Path(f"/proc/self/task/{os.getpid()}/children")
    return tuple(int(item) for item in path.read_text(encoding="ascii").split())


def _reap_descendants(worker_pid: int) -> None:
    try:
        os.killpg(worker_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while True:
        for child in _owned_children():
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        if time.monotonic() >= deadline:
            raise WorkerError("worker_descendants_not_reaped", "failed")
        time.sleep(.01)


def _supervise(kind: str) -> int:
    worker_pid: int | None = None
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    try:
        if sys.platform != "linux":
            raise WorkerError("worker_resource_limits_require_linux", "unsupported")
        if kind not in JOB_REQUEST_LIMITS:
            raise WorkerError("unknown_worker_job")
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(36, ctypes.c_ulong(1), 0, 0, 0) != 0:
            raise WorkerError("worker_descendant_ownership_unavailable", "unsupported")
        _owned_children()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        worker_pid = os.fork()
        if worker_pid == 0:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.setpgid(0, 0)
            sys.argv = [sys.argv[0], kind]
            code = _main()
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
        while not stopping:
            observed = os.waitid(os.P_PID, worker_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if observed is not None:
                return observed.si_status if observed.si_code == os.CLD_EXITED else 1
            time.sleep(.01)
        return 1
    except WorkerError as error:
        sys.stdout.write(json.dumps({"status": error.status, "reason": error.reason}))
        return 1
    except OSError:
        sys.stdout.write(json.dumps({"status": "failed", "reason": "worker_supervision_failed"}))
        return 1
    finally:
        if worker_pid is not None and worker_pid > 0:
            _reap_descendants(worker_pid)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise":
        raise SystemExit(_supervise(sys.argv[2]))
    raise SystemExit(_main())

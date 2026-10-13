from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from komga_cover_extractor.infrastructure import workers

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux child ownership and resource limits")

_SUPERVISOR_SOURCE = r'''
import sys
from komga_cover_extractor.infrastructure import jobs
source, kind, supervised = sys.argv[1:]
def decode(_kind, request):
    scope = {"request": request}
    exec(source, scope)
    return scope["response"]
jobs._dispatch = decode
if supervised == "true":
    raise SystemExit(jobs._supervise(kind))
sys.argv = [sys.argv[0], kind]
raise SystemExit(jobs._main())
'''

_DECODER_SOURCE = r'''
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
root = Path(request["root"])
native = "import time; time.sleep(30)"
streams = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if request["mode"] == "success_redirected" else {}
attached = subprocess.Popen([sys.executable, "-I", "-c", native], **streams)
detached = subprocess.Popen([sys.executable, "-I", "-c", native], start_new_session=True, **streams)
record = {
    "worker": os.getpid(), "descendants": [attached.pid, detached.pid],
    "groups": [os.getpgrp(), os.getpgid(attached.pid), os.getpgid(detached.pid)],
    "limits": {"address_space": list(resource.getrlimit(resource.RLIMIT_AS)),
               "cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
               "core": list(resource.getrlimit(resource.RLIMIT_CORE))},
}
(root / "worker.json").write_text(json.dumps(record), encoding="utf-8")
while not (root / "release-worker").exists():
    time.sleep(.005)
if request["mode"] == "exit_no_result":
    os._exit(0)
if request["mode"] == "output_limit":
    sys.stdout.buffer.write(b"x" * (2 * 1024 * 1024))
    sys.stdout.buffer.flush()
    time.sleep(30)
if request["mode"] == "stderr_limit":
    sys.stderr.buffer.write(b"e" * (256 * 1024))
    sys.stderr.buffer.flush()
    response = {"status": "invalid_input", "reason": "fixture_decoder_failed"}
else:
    response = {"status": "valid", "value": 37}
'''

_APPLICATION_SOURCE = r'''
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from komga_cover_extractor.infrastructure import workers

root = Path(sys.argv[1])
mode = sys.argv[2]
prior = int(sys.argv[3])
decoder_source, supervisor_source = sys.argv[4:]
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(36, prior, 0, 0, 0) == 0

def flag():
    value = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(value), 0, 0, 0) == 0
    return value.value

def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True

def parent_of(pid):
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    raise AssertionError("missing observed parent")

original_popen = subprocess.Popen
supervisor_pid = None
owned_process = None
unrelated = None
orphan_parent = None
orphan_pid = None
orphan_adopter = None
actor_error = []
release_at = None
cancel_at = None
cancel_event = threading.Event()
received_signals = []

def request_shutdown(signum, _frame):
    received_signals.append(signum)
    cancel_event.set()

if mode == "signal_cancel":
    signal.signal(signal.SIGTERM, request_shutdown)
handler_snapshot = {item: signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGINT)}

orphan_source = r"""
import os
from pathlib import Path
import sys
import time
root = Path(sys.argv[1])
ready_read, ready_write = os.pipe()
child = os.fork()
if child:
    os.close(ready_write)
    assert os.read(ready_read, 1) == b"1"
    os.close(ready_read)
    os._exit(0)
os.close(ready_read)
os.setsid()
(root / "unrelated.pid").write_text(str(os.getpid()), encoding="ascii")
os.write(ready_write, b"1")
os.close(ready_write)
time.sleep(30)
"""

def launch(arguments, **options):
    global supervisor_pid, owned_process
    supervised = "--supervise" in arguments
    process = original_popen(
        [sys.executable, "-I", "-c", supervisor_source, decoder_source,
         arguments[-1], str(supervised).lower()], **options,
    )
    supervisor_pid = process.pid
    owned_process = process
    return process

workers.subprocess.Popen = launch

def observe_and_release():
    global unrelated, orphan_parent, orphan_pid, orphan_adopter, release_at, cancel_at
    if unrelated is None and (root / "worker.json").exists():
        unrelated = original_popen(
            [sys.executable, "-I", "-c", "import os,time;time.sleep(.05);os._exit(42)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        orphan_parent = original_popen(
            [sys.executable, "-I", "-c", orphan_source, str(root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert orphan_parent.wait(timeout=2) == 0
        orphan_pid = int((root / "unrelated.pid").read_text(encoding="ascii"))
        orphan_adopter = parent_of(orphan_pid)
        if mode in {"success", "success_redirected", "exit_no_result", "output_limit", "stderr_limit"}:
            release_at = time.monotonic()
            (root / "release-worker").touch()
        if mode == "cancel_event":
            cancel_at = time.monotonic()
            cancel_event.set()
        if mode == "signal_cancel":
            cancel_at = time.monotonic()
            os.kill(os.getpid(), signal.SIGTERM)
        if mode == "cancel":
            cancel_at = time.monotonic()
    return unrelated is not None and mode == "cancel"

def event_actor():
    try:
        deadline = time.monotonic() + 4
        while not (root / "worker.json").exists():
            if time.monotonic() >= deadline:
                raise AssertionError("worker did not initialize")
            time.sleep(.005)
        observe_and_release()
    except BaseException as error:
        actor_error.append(repr(error))
        cancel_event.set()

before = flag()
actor = threading.Thread(target=event_actor) if mode in {"cancel_event", "signal_cancel"} else None
if actor is not None:
    actor.start()
response = None
failure = None
started = time.monotonic()
try:
    response = workers.run_worker(
        "inspect", {"root": str(root), "mode": mode}, timeout=3,
        max_output_bytes=16 if mode == "output_limit" else 4096,
        cancel=cancel_event if actor is not None else observe_and_release,
    )
except workers.WorkerError as error:
    failure = {"status": error.status, "reason": error.reason,
               "started": error.started, "stderr_bytes": len(error.stderr)}
returned_at = time.monotonic()
elapsed = returned_at - started
boundary = cancel_at if cancel_at is not None else release_at if release_at is not None else started + 3
cleanup_seconds = returned_at - boundary
if actor is not None:
    actor.join(timeout=4)
    assert not actor.is_alive()
record = json.loads((root / "worker.json").read_text(encoding="utf-8"))
worker_pids = [supervisor_pid, record["worker"], *record["descendants"]]
remaining = [pid for pid in worker_pids if alive(pid)]
report = {
    "mode": mode, "caller_pid": os.getpid(), "caller_subreaper_before": before,
    "caller_subreaper_after": flag(), "unrelated_orphan_pid": orphan_pid,
    "unrelated_orphan_adopter": orphan_adopter,
    "unrelated_exit_status": unrelated.wait(timeout=2) if unrelated is not None else None,
    "worker_pids": worker_pids, "remaining_worker_pids": remaining,
    "worker_observations": record, "response": response, "failure": failure,
    "elapsed_seconds": elapsed, "cleanup_seconds": cleanup_seconds, "actor_errors": actor_error,
    "owned_streams_closed": [stream is not None and stream.closed for stream in
                             (owned_process.stdin, owned_process.stdout, owned_process.stderr)],
    "caller_signal_handlers_unchanged": {item: signal.getsignal(item) for item in handler_snapshot} == handler_snapshot,
    "caller_shutdown_signals": received_signals,
    "python": sys.executable, "workers_module": workers.__file__,
    "workers_sha256": hashlib.sha256(Path(workers.__file__).read_bytes()).hexdigest(),
}
for pid in {*worker_pids, orphan_pid}:
    if pid is None:
        continue
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
assert libc.prctl(36, prior, 0, 0, 0) == 0
print(json.dumps(report, sort_keys=True))
'''

_GUARDIAN_SOURCE = r'''
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
try:
    result = subprocess.run([sys.executable, "-I", "-c", *sys.argv[1:]],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=18)
finally:
    deadline = time.monotonic() + 5
    while True:
        path = Path(f"/proc/self/task/{os.getpid()}/children")
        for item in path.read_text(encoding="ascii").split():
            try:
                os.kill(int(item), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("fixture guardian descendants not reaped")
        time.sleep(.005)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
'''


def _run_probe(root: Path, mode: str, *, prior: int = 0) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _GUARDIAN_SOURCE, _APPLICATION_SOURCE,
         str(root), mode, str(prior), _DECODER_SOURCE, _SUPERVISOR_SOURCE],
        capture_output=True, timeout=25,
        cwd=root, env={key: value for key, value in os.environ.items() if not key.startswith("KCE_")},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    report = json.loads(result.stdout)
    assert report["actor_errors"] == []
    assert report["owned_streams_closed"] == [True, True, True]
    assert report["caller_signal_handlers_unchanged"] is True
    assert report["caller_subreaper_before"] == prior
    assert report["caller_subreaper_after"] == prior
    assert report["unrelated_exit_status"] == 42
    assert report["remaining_worker_pids"] == [], report
    for pid in {*report["worker_pids"], report["unrelated_orphan_pid"]}:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    if prior == 0:
        assert report["unrelated_orphan_adopter"] != report["caller_pid"]
    else:
        assert report["unrelated_orphan_adopter"] == report["caller_pid"]
    assert report["elapsed_seconds"] < 10
    assert 0 <= report["cleanup_seconds"] < 5
    return report


def test_decoder_deadline_reaps_worker_and_native_descendant(tmp_path: Path) -> None:
    report = _run_probe(tmp_path, "timeout")
    assert report["response"] is None
    assert report["failure"] == {
        "status": "failed", "reason": "worker_deadline", "started": True, "stderr_bytes": 0,
    }


@pytest.mark.parametrize("mode", ["cancel", "cancel_event", "signal_cancel"])
def test_decoder_cancellation_reaps_detached_children_without_adopting_unrelated_orphans(
    tmp_path: Path, mode: str,
) -> None:
    report = _run_probe(tmp_path, mode)
    assert report["caller_shutdown_signals"] == ([15] if mode == "signal_cancel" else [])
    assert report["response"] is None
    assert report["failure"] == {
        "status": "cancelled", "reason": "shutdown_requested", "started": True, "stderr_bytes": 0,
    }


@pytest.mark.parametrize("prior", [0, 1])
def test_success_preserves_caller_child_ownership_and_enforces_decoder_limits(tmp_path: Path, prior: int) -> None:
    report = _run_probe(tmp_path, "success", prior=prior)
    assert report["response"] == {"status": "valid", "value": 37}
    assert report["failure"] is None
    observations = report["worker_observations"]
    assert observations["groups"][0] == observations["groups"][1]
    assert observations["groups"][2] != observations["groups"][0]
    assert observations["limits"] == {
        "address_space": [2147483648, 2147483648], "cpu": [300, 300], "core": [0, 0],
    }


def test_successful_exit_with_redirected_lingering_children_keeps_exact_result(tmp_path: Path) -> None:
    report = _run_probe(tmp_path, "success_redirected")
    assert report["response"] == {"status": "valid", "value": 37}
    assert report["failure"] is None


def test_exited_decoder_with_pipe_holding_children_is_a_typed_failure_not_a_hang(tmp_path: Path) -> None:
    report = _run_probe(tmp_path, "exit_no_result")
    assert report["response"] is None
    assert report["failure"] == {
        "status": "failed", "reason": "missing_worker_result", "started": True, "stderr_bytes": 0,
    }


def test_decoder_output_over_budget_is_stopped_not_buffered_indefinitely(tmp_path: Path) -> None:
    report = _run_probe(tmp_path, "output_limit")
    assert report["failure"] == {
        "status": "invalid_input", "reason": "worker_output_limit", "started": True, "stderr_bytes": 0,
    }


def test_decoder_diagnostics_are_bounded_and_do_not_hide_failure(tmp_path: Path) -> None:
    report = _run_probe(tmp_path, "stderr_limit")
    assert report["failure"] == {
        "status": "invalid_input", "reason": "fixture_decoder_failed", "started": True, "stderr_bytes": 65536,
    }


def test_prelaunch_cancellation_does_not_claim_an_unknown_started_delivery() -> None:
    from threading import Event

    cancel = Event()
    cancel.set()
    with pytest.raises(workers.WorkerError) as caught:
        workers.run_worker("http", {"method": "POST"}, cancel=cancel)
    assert (caught.value.status, caught.value.reason, caught.value.started) == (
        "cancelled", "shutdown_requested", False,
    )

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group and subreaper contract")
_PACKAGE_SOURCE = Path(__file__).resolve().parents[2] / "src"

# Transport substitutes are real disposable processes, not installed-service proof.
_CHILD_SOURCE = r'''
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path(os.environ["FIXTURE_ROOT"])
mode = os.environ["FIXTURE_MODE"]
route = sys.argv[1]
(root / f"{route}.pid").write_text(str(os.getpid()), encoding="ascii")
(root / f"{route}.observation.json").write_text(json.dumps({
    "argv": sys.argv[1:], "root_input": os.environ.get("KOMGA_COVER_EXTRACTOR_ROOTS_JSON"),
    "quality": os.environ["KCE_IMAGE_QUALITY"], "group": os.getpgrp(),
}), encoding="utf-8")
print(f"{route} stdout preserved", flush=True)
print(f"{route} stderr preserved", file=sys.stderr, flush=True)

def stop(signum, frame):
    (root / f"{route}.signal").write_text(str(signum), encoding="ascii")
    raise SystemExit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
if mode == "stubborn" and route == "watch":
    descendant = r"""
import os
from pathlib import Path
import signal
import time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["FIXTURE_ROOT"]).joinpath("detached.pid").write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(.1)
"""
    subprocess.Popen([sys.executable, "-I", "-c", descendant], start_new_session=True)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline = time.monotonic() + 3
    while not (root / "detached.pid").exists():
        if time.monotonic() >= deadline:
            raise SystemExit(9)
        time.sleep(.005)

fd = int(os.environ.pop("KOMGA_COVER_EXTRACTOR_READY_FD"))
if mode == "invalid_ready":
    os.write(fd, b"doctor\n")
    os.close(fd)
elif mode != "no_ready":
    os.write(fd, b"ready\n")
    os.close(fd)
    (root / f"{route}.ready").write_text("initialized", encoding="ascii")

if route == "qbit" and mode in {"helper_fail", "helper_zero"}:
    deadline = time.monotonic() + 3
    while not (root / "watch.ready").exists():
        if time.monotonic() >= deadline:
            raise SystemExit(9)
        time.sleep(.005)
    raise SystemExit(7 if mode == "helper_fail" else 0)
if route == "watch" and mode == "watch_zero":
    deadline = time.monotonic() + 5
    while not (root / "watch.exit").exists():
        if time.monotonic() >= deadline:
            raise SystemExit(9)
        time.sleep(.005)
    raise SystemExit(0)
if route == "run":
    if os.environ["FIXTURE_HELPER"] == "true":
        deadline = time.monotonic() + 3
        while not (root / "qbit.ready").exists():
            if time.monotonic() >= deadline:
                raise SystemExit(9)
            time.sleep(.005)
    raise SystemExit(3 if mode == "run_review" else 0)
while True:
    time.sleep(.1)
'''


_DRIVER_SOURCE = r'''
import json
import os
from pathlib import Path
import ctypes
import signal
import subprocess
import sys

from komga_cover_extractor.application import serve as supervision
from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.config import load_config
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.libraries import LibraryRoot

root = Path(os.environ["FIXTURE_ROOT"])
helper = os.environ["FIXTURE_HELPER"] == "true"
watch = os.environ["FIXTURE_WATCH"] == "true"
config = load_config(environ={}, cwd=root, overrides={
    "paths": [root / "library"], "download_folders": [root / "downloads"],
    "state_dir": root / "absent-state", "log_dir": root / "absent-logs",
    "watchdog_toggle": watch, "image_quality": 72,
    "uncheck_non_qbit_upgrades_toggle": helper, "check_for_existing_series_toggle": helper,
    "qbittorrent_ip": "http://127.0.0.1", "qbittorrent_port": "8080",
    "qbittorrent_target_category": "manga",
})
request = RunRequest("serve", roots=(
    LibraryRoot(root / "library", formats=("volume",), extensions=(".cbz",), library_types=("manga",)),
    LibraryRoot(root / "downloads", roles=("download",)),
), authorization=RunAuthorization(
    apply=False, allow_remote=os.environ["FIXTURE_REMOTE"] == "true",
    allow_torrent_removal=os.environ["FIXTURE_REMOVAL"] == "true",
))
real_popen = subprocess.Popen

def launch(arguments, **kwargs):
    route_index = next(index for index, item in enumerate(arguments) if item in {"run", "watch", "qbit"})
    route = arguments[route_index]
    with (root / "commands.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(list(arguments)) + "\n")
    if os.environ["FIXTURE_MODE"] == "spawn_failure" and route == "qbit":
        raise OSError("scripted second-child spawn failure")
    process = real_popen(
        [sys.executable, "-I", str(root / "child.py"), *arguments[route_index:]], **kwargs,
    )
    with (root / "spawned.jsonl").open("a", encoding="ascii") as stream:
        stream.write(str(process.pid) + "\n")
    return process

supervision.subprocess.Popen = launch
if os.environ["FIXTURE_MODE"] == "cleanup_fault":
    def lose_process_ownership(*args, **kwargs):
        raise OSError("scripted ownership backend failure during shutdown")
    supervision._stop_children = lose_process_ownership
prior_handlers = {item: signal.getsignal(item) for item in (signal.SIGINT, signal.SIGTERM)}
prior_subreaper = ctypes.c_int()
assert ctypes.CDLL(None).prctl(37, ctypes.byref(prior_subreaper), 0, 0, 0) == 0
status = supervision.serve(
    request, config=config, config_path=root / "selected.toml",
    startup_timeout_seconds=float(os.environ["FIXTURE_STARTUP_TIMEOUT"]), shutdown_timeout_seconds=.5,
)
assert {item: signal.getsignal(item) for item in prior_handlers} == prior_handlers
after_subreaper = ctypes.c_int()
assert ctypes.CDLL(None).prctl(37, ctypes.byref(after_subreaper), 0, 0, 0) == 0
assert after_subreaper.value == prior_subreaper.value
raise SystemExit(status)
'''


def _fixture(
    root: Path, *, mode: str, helper: bool = True, watch: bool = True,
    remote: bool = False, removal: bool = False, startup_timeout: float = 3,
) -> dict[str, str]:
    (root / "child.py").write_text(textwrap.dedent(_CHILD_SOURCE), encoding="utf-8")
    (root / "selected.toml").write_text("image_quality = 19\n", encoding="utf-8")
    library = root / "library"
    library.mkdir()
    (library / "retain.cbz").write_bytes(b"disposable-book-must-not-change")
    (root / "downloads").mkdir()
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("KCE_")},
        "PYTHONPATH": str(_PACKAGE_SOURCE), "PYTHONUNBUFFERED": "1", "HOME": str(root),
        "FIXTURE_ROOT": str(root), "FIXTURE_MODE": mode,
        "FIXTURE_HELPER": str(helper).lower(), "FIXTURE_WATCH": str(watch).lower(),
        "FIXTURE_REMOTE": str(remote).lower(), "FIXTURE_REMOVAL": str(removal).lower(),
        "FIXTURE_STARTUP_TIMEOUT": str(startup_timeout),
    }


def _pids(root: Path) -> tuple[int, ...]:
    observed = {int(path.read_text(encoding="ascii")) for path in root.glob("*.pid")}
    spawned = root / "spawned.jsonl"
    if spawned.exists():
        observed.update(int(line) for line in spawned.read_text(encoding="ascii").splitlines())
    return tuple(observed)


def _cleanup(root: Path, process: subprocess.Popen[bytes] | None = None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    for pid in _pids(root):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _assert_all_reaped(root: Path) -> None:
    for pid in _pids(root):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (root / "absent-state").exists()
    assert not (root / "absent-logs").exists()
    assert (root / "library" / "retain.cbz").read_bytes() == b"disposable-book-must-not-change"


def _run(root: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(_DRIVER_SOURCE)],
            cwd=root, env=environment, capture_output=True,
            timeout=8, check=False,
        )
    finally:
        # Test failures still terminate every recorded disposable descendant.
        if sys.exc_info()[0] is not None:
            _cleanup(root)


def _wait_ready(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stderr is not None
    captured = bytearray()
    deadline = time.monotonic() + 5
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        while b"kce serve ready\n" not in captured:
            remaining = deadline - time.monotonic()
            assert remaining > 0, bytes(captured)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                assert chunk, bytes(captured)
                captured.extend(chunk)
    return bytes(captured)


@pytest.mark.parametrize(("mode", "expected"), [("helper_fail", 1), ("helper_zero", 1)])
def test_unexpected_helper_exit_stops_primary_and_preserves_both_output_streams(
    tmp_path: Path, mode: str, expected: int,
) -> None:
    environment = _fixture(tmp_path, mode=mode)
    try:
        result = _run(tmp_path, environment)
        assert result.returncode == expected, result.stderr
        assert b"torrent helper exited unexpectedly" in result.stderr
        assert b"watch stdout preserved\n" in result.stdout
        assert b"qbit stdout preserved\n" in result.stdout
        assert b"watch stderr preserved\n" in result.stderr
        assert b"qbit stderr preserved\n" in result.stderr
        assert (tmp_path / "watch.signal").read_text(encoding="ascii") == "15"
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path)


@pytest.mark.parametrize(("mode", "expected"), [("run_complete", 0), ("run_review", 3)])
def test_one_shot_run_status_is_preserved_and_helper_is_stopped_and_reaped(
    tmp_path: Path, mode: str, expected: int,
) -> None:
    environment = _fixture(tmp_path, mode=mode, watch=False)
    try:
        result = _run(tmp_path, environment)
        assert result.returncode == expected, result.stderr
        assert (tmp_path / "qbit.signal").read_text(encoding="ascii") == "15"
        assert b"run stdout preserved\n" in result.stdout
        assert b"qbit stderr preserved\n" in result.stderr
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_supervisor_forwards_signal_and_reaps_both_service_groups(
    tmp_path: Path, signum: signal.Signals,
) -> None:
    environment = _fixture(tmp_path, mode="signals", remote=True, removal=False)
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_DRIVER_SOURCE)], cwd=tmp_path, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        before = _wait_ready(process)
        start = time.monotonic()
        process.send_signal(signum)
        output, after = process.communicate(timeout=5)
        assert process.returncode == 128 + signum, before + after
        assert time.monotonic() - start < 3
        assert (tmp_path / "watch.signal").read_text(encoding="ascii") == str(signum)
        assert (tmp_path / "qbit.signal").read_text(encoding="ascii") == str(signum)
        assert output == b"watch stdout preserved\nqbit stdout preserved\n" or output == b"qbit stdout preserved\nwatch stdout preserved\n"
        assert (before + after).count(b"kce serve ready\n") == 1
        commands = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
        assert commands == [
            [sys.executable, "-I", "-m", "komga_cover_extractor", "--config", str(tmp_path / "selected.toml"), "watch", "--allow-remote"],
            [sys.executable, "-I", "-m", "komga_cover_extractor", "--config", str(tmp_path / "selected.toml"), "qbit", "--allow-remote"],
        ]
        observation = json.loads((tmp_path / "qbit.observation.json").read_text(encoding="utf-8"))
        assert observation["argv"] == ["qbit", "--allow-remote"]
        assert observation["quality"] == "72"
        root_input = json.loads(observation["root_input"])
        assert root_input["roots"][0] == {
            "path": str(tmp_path / "library"), "formats": ["volume"], "extensions": [".cbz"],
            "library_types": ["manga"], "roles": ["library"],
        }
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path, process)


def test_stubborn_service_and_detached_native_worker_are_killed_and_reaped(tmp_path: Path) -> None:
    environment = _fixture(tmp_path, mode="stubborn")
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_DRIVER_SOURCE)], cwd=tmp_path, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        before = _wait_ready(process)
        assert int((tmp_path / "detached.pid").read_text(encoding="ascii")) not in {
            int((tmp_path / "watch.pid").read_text(encoding="ascii")),
            int((tmp_path / "qbit.pid").read_text(encoding="ascii")),
        }
        start = time.monotonic()
        process.send_signal(signal.SIGTERM)
        output, after = process.communicate(timeout=5)
        assert process.returncode == 143, before + after
        assert time.monotonic() - start < 3
        assert b"watch stdout preserved" in output
        assert (tmp_path / "qbit.signal").read_text(encoding="ascii") == "15"
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path, process)


@pytest.mark.parametrize(("mode", "expected"), [
    ("no_ready", 2),
    ("invalid_ready", 1),
])
def test_dependency_presence_or_empty_success_cannot_fake_service_readiness(
    tmp_path: Path, mode: str, expected: int,
) -> None:
    environment = _fixture(
        tmp_path, mode=mode, helper=False, startup_timeout=.2 if mode == "no_ready" else 3,
    )
    try:
        result = _run(tmp_path, environment)
        assert result.returncode == expected, result.stderr
        assert b"kce serve ready\n" not in result.stderr
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path)


def test_zero_watch_exit_after_service_readiness_is_a_failure(tmp_path: Path) -> None:
    environment = _fixture(tmp_path, mode="watch_zero", helper=False)
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_DRIVER_SOURCE)], cwd=tmp_path,
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        readiness = _wait_ready(process)
        assert (tmp_path / "watch.ready").read_text(encoding="ascii") == "initialized"
        (tmp_path / "watch.exit").touch()
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 1, readiness + stderr
        assert b"kce serve ready\n" in readiness
        assert b"watch stdout preserved\n" in stdout
        assert b"watcher exited unexpectedly" in readiness + stderr
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path, process)


def test_second_child_spawn_failure_stops_already_started_primary(tmp_path: Path) -> None:
    environment = _fixture(tmp_path, mode="spawn_failure")
    try:
        result = _run(tmp_path, environment)
        assert result.returncode == 1
        assert b"cannot start or supervise installed service" in result.stderr
        commands = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [command[-1] for command in commands] == ["watch", "qbit"]
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path)


def test_shutdown_backend_failure_still_kills_and_reaps_known_leaders(tmp_path: Path) -> None:
    environment = _fixture(tmp_path, mode="cleanup_fault")
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_DRIVER_SOURCE)], cwd=tmp_path, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        before = _wait_ready(process)
        process.send_signal(signal.SIGTERM)
        output, after = process.communicate(timeout=5)
        assert process.returncode == 1, before + after
        assert b"owned child shutdown did not complete" in after
        assert b"qbit stdout preserved\n" in output
        _assert_all_reaped(tmp_path)
    finally:
        _cleanup(tmp_path, process)

"""Supervise installed application children at the invocation boundary."""
from __future__ import annotations

import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Configuration
    from ..domain.libraries import LibraryRoot
    from .pipeline import RunRequest


READY_ENV = "KOMGA_COVER_EXTRACTOR_READY_FD"
ROOTS_ENV = "KOMGA_COVER_EXTRACTOR_ROOTS_JSON"
ROOTS_MAX_BYTES = 64 * 1024
_READY = b"ready\n"
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def notify_ready() -> None:
    """Consume and close this child's inherited readiness pipe exactly once."""
    raw = os.environ.pop(READY_ENV, None)
    if raw is None:
        return
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise ValueError("invalid_readiness_descriptor")
    descriptor = int(raw)
    if descriptor < 3:
        raise ValueError("readiness_descriptor_must_be_owned_pipe")
    info = os.fstat(descriptor)
    if (not stat.S_ISFIFO(info.st_mode)
            or os.readlink(f"/proc/self/fd/{descriptor}") != f"pipe:[{info.st_ino}]"):
        raise ValueError("readiness_descriptor_must_be_owned_pipe")
    import fcntl
    if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY:
        raise ValueError("readiness_descriptor_must_be_writable_pipe")
    if not os.get_inheritable(descriptor):
        raise ValueError("readiness_descriptor_must_be_inherited")
    try:
        os.set_inheritable(descriptor, False)
        if os.write(descriptor, _READY) != len(_READY):
            raise OSError("incomplete_readiness_notification")
    finally:
        os.close(descriptor)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_child_root_field")
        result[name] = value
    return result


def child_roots(environ: Mapping[str, str]) -> tuple[LibraryRoot, ...]:
    """Decode the supervisor's structured roots at the child CLI boundary."""
    from ..domain.libraries import LibraryRoot, normalize_library_roots

    raw = environ.get(ROOTS_ENV)
    if raw is None:
        return ()
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > ROOTS_MAX_BYTES:
        raise ValueError("child_root_input_limit")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_fields)
        if not isinstance(data, dict) or set(data) != {"schema_version", "roots"}:
            raise ValueError
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError
        if not isinstance(data["roots"], list):
            raise ValueError
        roots: list[LibraryRoot] = []
        for item in data["roots"]:
            if not isinstance(item, dict) or set(item) != {
                "path", "formats", "extensions", "library_types", "roles",
            }:
                raise ValueError
            path = item["path"]
            if not isinstance(path, str) or not path or "\x00" in path or not Path(path).is_absolute():
                raise ValueError
            for field in ("formats", "extensions", "library_types", "roles"):
                values = item[field]
                if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value.strip() or "\x00" in value
                    for value in values
                ):
                    raise ValueError
                if len(values) != len(set(values)):
                    raise ValueError
            if any(value not in {"chapter", "volume"} for value in item["formats"]):
                raise ValueError
            if not item["roles"] or any(value not in {"library", "download"} for value in item["roles"]):
                raise ValueError
            roots.append(LibraryRoot(
                Path(path), formats=tuple(item["formats"]), extensions=tuple(item["extensions"]),
                library_types=tuple(item["library_types"]), roles=tuple(item["roles"]),
            ))
        return normalize_library_roots(tuple(roots))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_child_roots") from None


def _toml_literal(value: object) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_literal(key)} = {_toml_literal(item)}" for key, item in value.items()
        ) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    import tomli_w
    return tomli_w.dumps({"value": value}).removeprefix("value = ").removesuffix("\n")


def child_environment(
    config: Configuration, *, roots: tuple[LibraryRoot, ...] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Propagate resolved settings in environment values, never process arguments."""
    from ..domain.libraries import normalize_library_roots

    environment = {
        name: value for name, value in (os.environ if environ is None else environ).items()
        if not name.startswith("KCE_") and name not in {READY_ENV, ROOTS_ENV}
    }
    for name, value in config.as_dict(redact=False).items():
        environment[f"KCE_{name.upper()}"] = _toml_literal(value)
    if roots:
        payload = {
            "schema_version": 1,
            "roots": [
                {"path": str(root.path), "formats": list(root.formats),
                 "extensions": list(root.extensions), "library_types": list(root.library_types),
                 "roles": list(root.roles)}
                for root in normalize_library_roots(roots)
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > ROOTS_MAX_BYTES:
            raise ValueError("child_root_input_limit")
        environment[ROOTS_ENV] = encoded
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def child_command(
    command: str, request: RunRequest, *, config_path: Path | None = None,
) -> tuple[str, ...]:
    """Use the same interpreter's installed package with isolated module resolution."""
    if command not in {"run", "watch", "qbit"}:
        raise ValueError("invalid_supervised_command")
    arguments = [sys.executable, "-I", "-m", "komga_cover_extractor"]
    if config_path is not None:
        arguments.extend(("--config", os.path.abspath(config_path)))
    arguments.append(command)
    if request.authorization.apply:
        arguments.append("--apply")
    if request.authorization.allow_remote:
        arguments.append("--allow-remote")
    if request.authorization.allow_torrent_removal:
        arguments.append("--allow-torrent-removal")
    if request.interactive and command != "qbit":
        arguments.append("--interactive")
    return tuple(arguments)


def _state_writable(path: Path) -> bool:
    nearest: Path | None = None
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            return False
        if nearest is None:
            nearest = component
    return nearest is not None and os.access(nearest, os.W_OK | os.X_OK, effective_ids=True)


def _set_subreaper(enabled: bool) -> bool:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(prior), 0, 0, 0) != 0:
        raise OSError("cannot_read_descendant_ownership")
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(int(enabled)), 0, 0, 0) != 0:
        raise OSError("cannot_set_descendant_ownership")
    return bool(prior.value)


def _child_pids(parent: int) -> tuple[int, ...]:
    task_root = Path(f"/proc/{parent}/task")
    try:
        tasks = tuple(task_root.iterdir())
    except FileNotFoundError:
        if parent == os.getpid():
            raise OSError("process_ownership_proc_unavailable") from None
        return ()
    children: set[int] = set()
    for task in tasks:
        try:
            with (task / "children").open("r", encoding="ascii") as stream:
                raw = stream.read(1024 * 1024 + 1)
        except FileNotFoundError:
            continue
        if len(raw) > 1024 * 1024:
            raise OSError("process_tree_limit")
        children.update(int(token) for token in raw.split())
    return tuple(children)


def _signal_children(signum: int, sent: set[tuple[str, int]] | None = None) -> None:
    # Archive workers create their own sessions. Subreaper adoption also owns those.
    pending = list(_child_pids(os.getpid()))
    visited: set[int] = set()
    groups: set[int] = set()
    individual: set[int] = set()
    parent_group = os.getpgrp()
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        pending.extend(_child_pids(pid))
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            continue
        if group == parent_group:
            individual.add(pid)
        else:
            groups.add(group)
    for group in groups:
        if sent is not None:
            if ("group", group) in sent:
                continue
            sent.add(("group", group))
        try:
            os.killpg(group, signum)
        except ProcessLookupError:
            pass
    for pid in individual:
        if sent is not None:
            if ("pid", pid) in sent:
                continue
            sent.add(("pid", pid))
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _reap_children(processes: Sequence[subprocess.Popen[bytes]]) -> bool:
    leaders = {process.pid for process in processes}
    for process in processes:
        process.poll()
    for pid in _child_pids(os.getpid()):
        if pid not in leaders:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
    return not _child_pids(os.getpid())


def _stop_children(
    processes: Sequence[subprocess.Popen[bytes]], signum: int,
    *, timeout: float, signals: list[int],
) -> bool:
    if not processes:
        return True
    deadline = time.monotonic() + timeout
    grace_deadline = deadline - min(1.0, timeout / 5)
    sent: set[tuple[str, int]] = set()
    _signal_children(signum, sent)
    while True:
        if _reap_children(processes):
            return True
        now = time.monotonic()
        if now >= deadline:
            _signal_children(signal.SIGKILL)
            return _reap_children(processes)
        if now >= grace_deadline or len(signals) > 1:
            _signal_children(signal.SIGKILL)
        else:
            # Pick up newly orphaned native workers in independent sessions.
            _signal_children(signum, sent)
        time.sleep(min(.02, deadline - now))


def _exit_status(process: subprocess.Popen[bytes]) -> int | None:
    observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if observed is None:
        return None
    return observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status


def serve(
    request: RunRequest, *, config: Configuration, config_path: Path | None = None,
    environ: Mapping[str, str] | None = None, startup_timeout_seconds: float = 300,
    shutdown_timeout_seconds: float = 30,
) -> int:
    """Own run/watch and its optional independent helper until both are reaped."""
    if sys.platform != "linux":
        print("kce serve: Linux process supervision is required", file=sys.stderr)
        return 2
    from threading import current_thread, main_thread
    if current_thread() is not main_thread():
        print("kce serve: requires the process main thread", file=sys.stderr)
        return 2
    if (not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds <= 0
            or not math.isfinite(shutdown_timeout_seconds)
            or not 0 < shutdown_timeout_seconds <= 30):
        print("kce serve: invalid lifecycle deadlines", file=sys.stderr)
        return 2
    try:
        if config.watchdog_toggle and not any(
            "download" in root.roles for root in request.roots
        ) and not config.download_folders:
            print("kce serve: watch requires a download root", file=sys.stderr)
            return 2
        if request.authorization.apply and not _state_writable(config.state_dir):
            print("kce serve: apply requires writable nonsymlink state storage", file=sys.stderr)
            return 2
        environment = child_environment(config, roots=request.roots, environ=environ)
        if _child_pids(os.getpid()):
            print("kce serve: supervisor must own a dedicated process", file=sys.stderr)
            return 2
        prior_subreaper = _set_subreaper(True)
    except (OSError, ValueError):
        print("kce serve: process or configuration prerequisites unavailable", file=sys.stderr)
        return 2
    processes: dict[str, subprocess.Popen[bytes]] = {}
    descriptors: set[int] = set()
    handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    received: list[int] = []
    exit_code = 1
    cleanup_complete = True

    def stop(signum: int, frame: object) -> None:
        if len(received) < 2:
            received.append(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, stop)
        primary = "watch" if config.watchdog_toggle else "run"
        commands = (primary, "qbit") if config.uncheck_non_qbit_upgrades_toggle else (primary,)
        ready: set[str] = set()
        buffers: dict[str, bytes] = {}
        deadline = time.monotonic() + startup_timeout_seconds
        with selectors.DefaultSelector() as selector:
            for command in commands:
                if received:
                    break
                reader, writer = os.pipe()
                descriptors.update((reader, writer))
                os.set_blocking(reader, False)
                child_env = dict(environment)
                child_env[READY_ENV] = str(writer)
                process = subprocess.Popen(
                    child_command(command, request, config_path=config_path),
                    env=child_env, pass_fds=(writer,), start_new_session=True,
                    stdin=subprocess.DEVNULL if command == "qbit" else None,
                )
                processes[command] = process
                os.close(writer)
                descriptors.remove(writer)
                selector.register(reader, selectors.EVENT_READ, command)
                buffers[command] = b""
            if received:
                exit_code = 128 + received[0]
            while processes:
                if received:
                    exit_code = 128 + received[0]
                    break
                readiness_failed = False
                for key, _ in selector.select(.05):
                    command = key.data
                    try:
                        chunk = os.read(key.fd, 64)
                    except BlockingIOError:
                        continue
                    buffers[command] += chunk
                    if buffers[command] == _READY:
                        ready.add(command)
                        selector.unregister(key.fd)
                        os.close(key.fd)
                        descriptors.remove(key.fd)
                    elif not chunk or len(buffers[command]) >= len(_READY):
                        print(f"kce serve: {command} did not provide valid readiness", file=sys.stderr)
                        readiness_failed = True
                        break
                if received:
                    exit_code = 128 + received[0]
                    break
                helper = processes.get("qbit")
                if helper is not None:
                    status = _exit_status(helper)
                    if status is not None:
                        print("kce serve: torrent helper exited unexpectedly", file=sys.stderr)
                        exit_code = status if 1 <= status <= 3 else 1
                        break
                status = _exit_status(processes[primary])
                if status is not None:
                    if primary == "run":
                        if status == 0 and primary not in ready:
                            print("kce serve: run ended without initialized readiness", file=sys.stderr)
                            exit_code = 1
                        else:
                            exit_code = status if 0 <= status <= 3 else 1
                    else:
                        print("kce serve: watcher exited unexpectedly", file=sys.stderr)
                        exit_code = status if 1 <= status <= 3 else 1
                    break
                if readiness_failed:
                    exit_code = 1
                    break
                if ready == set(commands) and math.isfinite(deadline):
                    print("kce serve ready", file=sys.stderr)
                    deadline = math.inf
                if time.monotonic() >= deadline:
                    print("kce serve: initialized service readiness timed out", file=sys.stderr)
                    exit_code = 2
                    break
    except ValueError:
        print("kce serve: invalid supervised invocation", file=sys.stderr)
        exit_code = 2
    except OSError:
        print("kce serve: cannot start or supervise installed service", file=sys.stderr)
        exit_code = 1
    finally:
        cleanup_deadline = time.monotonic() + shutdown_timeout_seconds
        try:
            cleanup_complete = _stop_children(
                tuple(processes.values()), received[0] if received else signal.SIGTERM,
                timeout=shutdown_timeout_seconds, signals=received,
            )
        except OSError:
            cleanup_complete = False
            for process in processes.values():
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        pass
            for process in processes.values():
                try:
                    process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            _set_subreaper(prior_subreaper)
    if not cleanup_complete:
        print("kce serve: owned child shutdown did not complete", file=sys.stderr)
        return 1
    return exit_code

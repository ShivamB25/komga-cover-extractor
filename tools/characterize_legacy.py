#!/usr/bin/env python3
"""Run the legacy characterization probe inside a disposable bubblewrap sandbox.

This module intentionally contains only standard-library imports.  Importing it is
side-effect free: argument parsing, prerequisite checks, process creation, and
report publication happen only from :func:`main` (or the private test seam).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Final, cast

DEFAULT_TIMEOUT_SECONDS: Final[float] = 600.0
MAX_TIMEOUT_SECONDS: Final[float] = 3600.0
# A characterization report is one JSON document.  Field-mismatch details can
# be large, so stdout gets a bounded cap that is ample for the complete report.
MAX_CAPTURE_BYTES: Final[int] = 64 * 1024 * 1024
# stderr is diagnostic-only and intentionally has a much smaller independent cap.
MAX_STDERR_BYTES: Final[int] = 256 * 1024
MODES: Final[tuple[str, ...]] = ("fast", "corpus", "all")
SANDBOX_MARKER: Final[str] = "KCE_CHARACTERIZATION_SANDBOX"
SANDBOX_PREFIX: Final[PurePosixPath] = PurePosixPath("/opt/kce-venv")
BASE_PREFIX: Final[PurePosixPath] = PurePosixPath("/opt/kce-base")

SYSTEM_LIBRARY_TARGETS: Final[tuple[PurePosixPath, ...]] = (
    PurePosixPath("/lib"),
    PurePosixPath("/lib64"),
    PurePosixPath("/usr/lib"),
    PurePosixPath("/usr/lib64"),
)


@dataclass(frozen=True, slots=True)
class _AllowlistedFile:
    """One host file and its exact read-only path in the sandbox."""

    source: Path
    target: PurePosixPath


@dataclass(frozen=True, slots=True)
class _SandboxConfig:
    """Private launch configuration used by production and controlled tests.

    The command-line entry point always constructs this from the fixed production
    allowlist.  Tests may construct a config with synthetic files, but there is
    deliberately no public CLI switch that accepts arbitrary host paths.
    """

    files: tuple[_AllowlistedFile, ...]
    probe_target: PurePosixPath = PurePosixPath("/repo/tools/legacy_probe.py")


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Observed result of one sandbox process."""

    returncode: int
    report: dict[str, Any] | None
    stdout: str
    stderr: str
    error: str | None = None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class _InterpreterLayout:
    executable: Path
    command: PurePosixPath
    prefix: Path
    base_prefix: Path
    base_exec_prefix: Path
    stdlib: Path
    platstdlib: Path
    venv: bool
    remap_base: bool


def _repository_root() -> Path:
    """Return the checkout root, independently of the caller's cwd."""

    return Path(__file__).resolve().parent.parent


def _production_config() -> _SandboxConfig:
    """Build the narrow, fixed allowlist for the real legacy probe."""

    root = _repository_root()
    names = (
        "komga_cover_extractor.py",
        "settings.py",
        "tests.py",
        "manga_novel_dataset.csv",
        "blank_black.png",
        "blank_white.jpg",
    )
    files = tuple(
        _AllowlistedFile(
            root / "tests" / "fixtures" / "legacy" / name,
            PurePosixPath("/repo") / name,
        )
        for name in names
    )
    return _SandboxConfig(
        files=files + (
            _AllowlistedFile(
                root / "tools" / "legacy_probe.py",
                PurePosixPath("/repo/tools/legacy_probe.py"),
            ),
        )
    )


def _timeout_value(value: str) -> float:
    """Argparse converter for a bounded, strictly positive wall timeout."""

    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds") from exc
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"timeout must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely characterize the legacy implementation in a bwrap sandbox."
    )
    parser.add_argument("--mode", choices=MODES, default="all")
    parser.add_argument(
        "--output",
        type=Path,
        help="write the structured report to this explicitly requested path",
    )
    parser.add_argument(
        "--timeout",
        type=_timeout_value,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"wall timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_config(config: _SandboxConfig) -> str | None:
    if not config.files:
        return "the sandbox allowlist is empty"
    seen_targets: set[PurePosixPath] = set()
    probe_seen = False
    for item in config.files:
        if not item.source.is_absolute():
            return f"allowlisted source is not absolute: {item.source}"
        try:
            target = PurePosixPath(item.target)
        except TypeError:
            return "allowlisted destination is invalid"
        if not target.is_absolute() or not _is_repo_target(target):
            return f"allowlisted destination is outside /repo: {target}"
        if target in seen_targets:
            return f"duplicate allowlisted destination: {target}"
        seen_targets.add(target)
        if target == config.probe_target:
            probe_seen = True
        if not item.source.is_file():
            return f"allowlisted source is missing or not a regular file: {item.source}"
        try:
            resolved = item.source.resolve(strict=True)
        except OSError as exc:
            return f"cannot resolve allowlisted source {item.source}: {exc}"
        if not resolved.is_file():
            return f"allowlisted source is not a regular file: {item.source}"
    if not probe_seen:
        return f"probe is not present in the allowlist: {config.probe_target}"
    return None


def _is_repo_target(target: PurePosixPath) -> bool:
    repo = PurePosixPath("/repo")
    try:
        target.relative_to(repo)
    except ValueError:
        return False
    return target != repo and ".." not in target.parts


def _resolve_directory(value: str, label: str) -> tuple[Path | None, str | None]:
    path = Path(value)
    if not path.is_absolute():
        return None, f"{label} is not absolute: {value}"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return None, f"cannot resolve {label} {value}: {exc}"
    if not resolved.is_dir():
        return None, f"{label} is not a directory: {resolved}"
    return resolved, None


def _interpreter_layout() -> tuple[_InterpreterLayout | None, str | None]:
    executable_path = Path(sys.executable)
    if not executable_path.is_absolute():
        return None, f"interpreter path is not absolute: {sys.executable}"
    try:
        executable = executable_path.resolve(strict=True)
    except OSError as exc:
        return None, f"cannot resolve interpreter {sys.executable}: {exc}"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None, f"interpreter is not executable: {executable}"

    prefix, error = _resolve_directory(sys.prefix, "sys.prefix")
    if error is not None or prefix is None:
        return None, error or "sys.prefix is unavailable"
    base_prefix, error = _resolve_directory(sys.base_prefix, "sys.base_prefix")
    if error is not None or base_prefix is None:
        return None, error or "sys.base_prefix is unavailable"
    base_exec_prefix, error = _resolve_directory(
        sys.base_exec_prefix, "sys.base_exec_prefix"
    )
    if error is not None or base_exec_prefix is None:
        return None, error or "sys.base_exec_prefix is unavailable"

    stdlib_value = sysconfig.get_path("stdlib")
    platstdlib_value = sysconfig.get_path("platstdlib")
    if not stdlib_value or not platstdlib_value:
        return None, "Python stdlib paths are unavailable"
    try:
        stdlib = Path(stdlib_value).resolve(strict=True)
        platstdlib = Path(platstdlib_value).resolve(strict=True)
    except OSError as exc:
        return None, f"cannot resolve Python stdlib: {exc}"
    if not stdlib.is_dir() or not platstdlib.is_dir():
        return None, "Python stdlib paths are not directories"

    is_venv = prefix != base_prefix
    # Managed installs must keep their executable, stdlib and shared libraries
    # together even when the caller's HOME differs from the install owner's.
    remap_base = base_prefix not in (Path("/usr"), Path("/usr/local"))
    if is_venv:
        command = SANDBOX_PREFIX / "bin" / "python"
    elif remap_base:
        try:
            relative_executable = executable.relative_to(base_prefix)
        except ValueError:
            return None, "interpreter is outside sys.base_prefix"
        command = BASE_PREFIX / PurePosixPath(relative_executable.as_posix())
    else:
        # Keeping a system interpreter at its absolute path preserves its normal
        # landmark search while only selected files/directories are mounted.
        command = PurePosixPath(executable.as_posix())

    return (
        _InterpreterLayout(
            executable=executable,
            command=command,
            prefix=prefix,
            base_prefix=base_prefix,
            base_exec_prefix=base_exec_prefix,
            stdlib=stdlib,
            platstdlib=platstdlib,
            venv=is_venv,
            remap_base=remap_base,
        ),
        None,
    )


def _safe_target_for_host(
    source: Path, layout: _InterpreterLayout
) -> PurePosixPath | None:
    """Map a host path into a mounted prefix or retain a supported system path."""

    if source == Path("/") or _is_within(source, Path("/media")):
        return None

    # Venv/base paths are intentionally remapped before the host-home check:
    # uv-managed installs may live under home, but only the exact environment
    # directories are mounted and their aliases point at synthetic targets.
    if layout.venv and _is_within(source, layout.prefix):
        relative = source.relative_to(layout.prefix)
        return SANDBOX_PREFIX / PurePosixPath(relative.as_posix())
    if layout.remap_base and _is_within(source, layout.base_prefix):
        relative = source.relative_to(layout.base_prefix)
        return BASE_PREFIX / PurePosixPath(relative.as_posix())

    # Never bind an unrelated path from home or the checkout.  These checks
    # fail closed for unsupported interpreter layouts.
    home = Path.home().resolve()
    repository = _repository_root().resolve()
    if _is_within(source, home) or _is_within(source, repository):
        return None
    return PurePosixPath(source.as_posix())


def _parents_for_target(target: PurePosixPath) -> tuple[PurePosixPath, ...]:
    parents: list[PurePosixPath] = []
    current = target.parent
    while current != PurePosixPath("/"):
        parents.append(current)
        current = current.parent
    parents.reverse()
    return tuple(parents)


def _venv_base_aliases(layout: _InterpreterLayout) -> tuple[Path, ...]:
    aliases: list[Path] = [layout.base_prefix]
    if not layout.venv:
        return tuple(aliases)
    config_path = layout.prefix / "pyvenv.cfg"
    try:
        config_lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return tuple(aliases)
    for line in config_lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "home":
            configured_home = Path(value.strip())
            if configured_home.is_absolute():
                aliases.append(configured_home.parent)
            break
    return tuple(dict.fromkeys(aliases))


def _add_bind(
    args: list[str],
    binds: dict[PurePosixPath, Path],
    source: Path,
    target: PurePosixPath,
) -> str | None:
    existing = binds.get(target)
    if existing is not None:
        if existing != source:
            return f"conflicting mounts for {target}: {existing} and {source}"
        return None
    binds[target] = source
    args.extend(("--ro-bind", str(source), str(target)))
    return None


def _add_alias(
    args: list[str], aliases: set[PurePosixPath], source: Path, target: PurePosixPath
) -> None:
    """Create an in-namespace alias without mounting the host parent tree."""

    if target in aliases:
        return
    for parent in _parents_for_target(target):
        args.extend(("--dir", str(parent)))
    args.extend(("--symlink", str(source), str(target)))
    aliases.add(target)


def _system_library_mounts() -> tuple[
    tuple[tuple[Path, PurePosixPath], ...] | None, str | None
]:
    """Return the fixed system-library mounts needed by native extensions.

    The launcher deliberately mounts only known system library directories and
    their loader aliases.  It does not inspect arbitrary ELF dependencies or
    mount the host root, home, media, or checkout.
    """

    mounts: list[tuple[Path, PurePosixPath]] = []
    home = Path.home().resolve()
    for target in SYSTEM_LIBRARY_TARGETS:
        source_path = Path(target.as_posix())
        try:
            source = source_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return None, f"system library directory is unavailable: {target}: {exc}"
        if not source.is_dir():
            return None, f"system library path is not a directory: {source}"
        if source == Path("/"):
            return None, f"system library path resolves to host root: {target}"
        if _is_within(source, home) or _is_within(source, Path("/media")):
            return (
                None,
                f"system library path is outside the supported layout: {source}",
            )
        mounts.append((source, target))
    return tuple(mounts), None


def _sandbox_command(
    config: _SandboxConfig, layout: _InterpreterLayout, bwrap: str
) -> tuple[list[str] | None, str | None]:
    system_mounts, error = _system_library_mounts()
    if error is not None or system_mounts is None:
        return None, error or "system library directories are unavailable"
    home = Path.home().resolve()
    repository = _repository_root().resolve()
    if layout.venv and (
        layout.prefix in (Path("/"), home, repository)
        or _is_within(layout.prefix, Path("/media"))
    ):
        return None, f"sys.prefix is outside the supported layout: {layout.prefix}"
    if layout.remap_base and (
        layout.base_prefix in (Path("/"), home, repository)
        or _is_within(layout.base_prefix, Path("/media"))
    ):
        return (
            None,
            f"sys.base_prefix is outside the supported layout: {layout.base_prefix}",
        )

    command: list[str] = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-net",
        "--clearenv",
    ]
    # Every destination parent is created explicitly; no broad host directory
    # is mounted.  /proc and /dev are fresh namespace instances.
    created_dirs: set[PurePosixPath] = set()
    for sandbox_directory in (
        PurePosixPath("/repo"),
        PurePosixPath("/repo/tools"),
        PurePosixPath("/tmp"),
        PurePosixPath("/work"),
    ):
        command.extend(("--dir", str(sandbox_directory)))
        created_dirs.add(sandbox_directory)
    command.extend(
        ("--tmpfs", "/tmp", "--tmpfs", "/work", "--proc", "/proc", "--dev", "/dev")
    )

    binds: dict[PurePosixPath, Path] = {}
    aliases: set[PurePosixPath] = set()
    if layout.venv:
        command.extend(("--dir", str(SANDBOX_PREFIX.parent)))
        error = _add_bind(command, binds, layout.prefix, SANDBOX_PREFIX)
        if error is not None:
            return None, error
    if layout.remap_base:
        command.extend(("--dir", str(BASE_PREFIX.parent)))
        error = _add_bind(command, binds, layout.base_prefix, BASE_PREFIX)
        if error is not None:
            return None, error
        # pyvenv.cfg stores uv's absolute home path.  Synthetic symlinks make
        # those paths resolve to the read-only remapped base, without exposing
        # the host home tree.
        for base_alias in _venv_base_aliases(layout):
            _add_alias(
                command,
                aliases,
                Path(BASE_PREFIX.as_posix()),
                PurePosixPath(base_alias.as_posix()),
            )

    for system_source, system_target in system_mounts:
        for parent in _parents_for_target(system_target):
            if parent not in created_dirs:
                command.extend(("--dir", str(parent)))
                created_dirs.add(parent)
        error = _add_bind(command, binds, system_source, system_target)
        if error is not None:
            return None, error

    for stdlib_directory in (layout.stdlib, layout.platstdlib):
        target = _safe_target_for_host(stdlib_directory, layout)
        if target is None:
            return None, (
                "stdlib path unexpectedly resides outside the allowed prefixes: "
                f"{stdlib_directory}"
            )
        for parent in _parents_for_target(target):
            if parent not in created_dirs:
                command.extend(("--dir", str(parent)))
                created_dirs.add(parent)
        error = _add_bind(command, binds, stdlib_directory, target)
        if error is not None:
            return None, error

    if not layout.venv and not layout.remap_base:
        # System interpreters are mounted at their original landmark path.
        if (
            layout.executable == Path("/")
            or _is_within(layout.executable, home)
            or _is_within(layout.executable, Path("/media"))
            or _is_within(layout.executable, repository)
        ):
            return None, (
                "system interpreter is outside the supported layout: "
                f"{layout.executable}"
            )
        executable_target = layout.command
        for parent in _parents_for_target(executable_target):
            if parent not in created_dirs:
                command.extend(("--dir", str(parent)))
                created_dirs.add(parent)
        error = _add_bind(command, binds, layout.executable, executable_target)
        if error is not None:
            return None, error

    for item in config.files:
        target = PurePosixPath(item.target)
        for parent in _parents_for_target(target):
            if parent not in created_dirs:
                command.extend(("--dir", str(parent)))
                created_dirs.add(parent)
        error = _add_bind(command, binds, item.source.resolve(strict=True), target)
        if error is not None:
            return None, error

    command.extend(("--chdir", "/work"))
    command.extend(("--setenv", "PATH", "/opt/kce-venv/bin:/usr/bin:/bin"))
    command.extend(("--setenv", "HOME", "/work/home"))
    command.extend(("--setenv", "TMPDIR", "/tmp"))
    command.extend(("--setenv", "LANG", "C.UTF-8"))
    command.extend(("--setenv", SANDBOX_MARKER, "1"))
    command.extend((str(layout.command), "-B", str(config.probe_target)))
    return command, None


def _minimal_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        SANDBOX_MARKER: "1",
    }


@dataclass(slots=True)
class _CaptureState:
    data: bytearray
    limit: int
    overflowed: bool = False
    error: str | None = None


def _bounded_text(value: str | bytes, limit: int = MAX_STDERR_BYTES) -> str:
    if isinstance(value, bytes):
        truncated = len(value) > limit
        text = value[:limit].decode("utf-8", errors="replace")
    else:
        truncated = len(value) > limit
        text = value[:limit]
    if truncated:
        return text + "\n...[truncated]"
    return text


def _capture_pipe(pipe: BinaryIO, state: _CaptureState) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            previous_size = len(state.data)
            if previous_size < state.limit:
                state.data.extend(chunk[: state.limit - previous_size])
            if previous_size + len(chunk) > state.limit:
                state.overflowed = True
    except Exception as exc:
        # A reader failure must be visible to the launcher.  Returning silently
        # could make a partial report look like a valid characterization.
        state.error = f"{type(exc).__name__}: {exc}"


def _capture_problems(
    stdout_state: _CaptureState, stderr_state: _CaptureState
) -> str | None:
    problems: list[str] = []
    for stream_name, state in (("stdout", stdout_state), ("stderr", stderr_state)):
        if state.error is not None:
            problems.append(f"{stream_name} pipe read failed: {state.error}")
        if state.overflowed:
            problems.append(
                f"{stream_name} capture overflow: output exceeded {state.limit} bytes"
            )
    return "; ".join(problems) or None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> str | None:
    problems: list[str] = []
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        problems.append(f"unable to kill sandbox process group: {exc}")
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        problems.append(f"unable to kill sandbox process: {exc}")

    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # Repeat the group kill after the bounded wait in case the first signal
        # raced process creation or a descendant briefly escaped the group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            problems.append(f"unable to re-kill sandbox process group: {exc}")
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            problems.append(f"unable to re-kill sandbox process: {exc}")
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            problems.append("sandbox process did not exit after SIGKILL")
        except OSError as exc:
            problems.append(f"unable to reap sandbox process: {exc}")
    except OSError as exc:
        problems.append(f"unable to reap sandbox process: {exc}")
    return "; ".join(problems) or None


def _run_process(
    command: Sequence[str], timeout: float
) -> tuple[int, bytes, bytes, bool, str | None]:
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=_minimal_environment(),
            start_new_session=True,
        )
    except OSError as exc:
        return 2, b"", b"", False, f"unable to start sandbox: {exc}"

    stdout_pipe = cast(BinaryIO, process.stdout)
    stderr_pipe = cast(BinaryIO, process.stderr)
    stdout_state = _CaptureState(bytearray(), MAX_CAPTURE_BYTES)
    stderr_state = _CaptureState(bytearray(), MAX_STDERR_BYTES)
    stdout_thread = threading.Thread(
        target=_capture_pipe, args=(stdout_pipe, stdout_state), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_capture_pipe, args=(stderr_pipe, stderr_state), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    bounded_join = False
    process_problems: list[str] = []
    returncode = 1
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        bounded_join = True
        termination_error = _terminate_process_tree(process)
        if termination_error is not None:
            process_problems.append(termination_error)
        returncode = process.returncode if process.returncode is not None else 1
    except OSError as exc:
        bounded_join = True
        process_problems.append(f"unable to wait for sandbox: {exc}")
        termination_error = _terminate_process_tree(process)
        if termination_error is not None:
            process_problems.append(termination_error)
        returncode = process.returncode if process.returncode is not None else 1

    if bounded_join:
        # Once the complete sandbox process group has been killed, do not wait
        # indefinitely for inherited descriptors held by a misbehaving child.
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
    else:
        # Normal completion must drain stdout to EOF before its descriptor is
        # closed.  Closing a BufferedReader while its reader thread owns the
        # lock can otherwise deadlock the launcher.
        stdout_thread.join()
        stderr_thread.join()

    if stdout_thread.is_alive():
        process_problems.append("stdout capture reader did not finish")
    if stderr_thread.is_alive():
        process_problems.append("stderr capture reader did not finish")

    # Never close a pipe while its reader thread may still own the BufferedReader
    # lock.  A timed-out descendant that keeps a descriptor open is reported
    # explicitly and the daemon reader is allowed to terminate independently.
    for pipe, reader in ((stdout_pipe, stdout_thread), (stderr_pipe, stderr_thread)):
        if reader.is_alive():
            continue
        try:
            pipe.close()
        except (OSError, ValueError) as exc:
            process_problems.append(f"unable to close sandbox output pipe: {exc}")

    capture_error = _capture_problems(stdout_state, stderr_state)
    if capture_error is not None:
        process_problems.append(capture_error)
    process_error = "; ".join(process_problems) or None
    return (
        returncode,
        bytes(stdout_state.data),
        bytes(stderr_state.data),
        timed_out,
        process_error,
    )


def _validate_report(value: Any, mode: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "probe output is not a JSON object"
    required = {
        "schema_version",
        "mode",
        "python",
        "source_hashes",
        "tests",
        "corpus",
        "adaptations",
    }
    missing = sorted(required.difference(value))
    if missing:
        return None, f"probe report is missing required fields: {', '.join(missing)}"
    if value.get("schema_version") != 1:
        return None, "probe report has an unsupported schema_version"
    if value.get("mode") != mode:
        return None, f"probe report mode does not match requested mode {mode!r}"
    if not isinstance(value.get("python"), str):
        return None, "probe report python field is not a string"
    if not isinstance(value.get("source_hashes"), dict):
        return None, "probe report source_hashes field is not an object"
    if not isinstance(value.get("tests"), list):
        return None, "probe report tests field is not an array"
    if value.get("corpus") is not None and not isinstance(value.get("corpus"), dict):
        return None, "probe report corpus field is neither an object nor null"
    if not isinstance(value.get("adaptations"), list):
        return None, "probe report adaptations field is not an array"
    return cast(dict[str, Any], value), None


def _error_report(mode: str, detail: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "python": platform_python_version(),
        "source_hashes": {},
        "tests": [],
        "corpus": None,
        "adaptations": [{"name": "launcher", "status": "blocked", "detail": detail}],
        "error": detail,
    }


def platform_python_version() -> str:
    """Return a stable host version without importing the child or originals."""

    return sys.version.split()[0]


def _decode_report(
    stdout: bytes, mode: str
) -> tuple[dict[str, Any] | None, str | None]:
    text = stdout.decode("utf-8", errors="replace")
    if not text.strip():
        return None, "probe produced no structured output"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"probe output is malformed JSON: {exc.msg}"
    return _validate_report(value, mode)


def _run_sandbox(
    mode: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    _config: _SandboxConfig | None = None,
    _bwrap_path: str | None = None,
) -> SandboxResult:
    """Run a probe; ``_config``/``_bwrap_path`` are private test seams only."""

    if mode not in MODES:
        return SandboxResult(2, None, "", "", f"unsupported mode: {mode}")
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        return SandboxResult(2, None, "", "", "timeout is outside the allowed range")
    config = _config if _config is not None else _production_config()
    config_error = _validate_config(config)
    if config_error is not None:
        report = _error_report(mode, config_error)
        return SandboxResult(2, report, "", config_error, config_error)
    if not sys.platform.startswith("linux"):
        detail = "bubblewrap characterization is supported only on Linux"
        report = _error_report(mode, detail)
        return SandboxResult(2, report, "", detail, detail)

    bwrap = _bwrap_path or shutil.which("bwrap")
    if bwrap is None:
        detail = "required sandbox executable bwrap is unavailable"
        report = _error_report(mode, detail)
        return SandboxResult(2, report, "", detail, detail)
    bwrap_path = Path(bwrap)
    if (
        not bwrap_path.is_absolute()
        or not bwrap_path.is_file()
        or not os.access(bwrap_path, os.X_OK)
    ):
        detail = f"required sandbox executable is not usable: {bwrap}"
        report = _error_report(mode, detail)
        return SandboxResult(2, report, "", detail, detail)

    layout, error = _interpreter_layout()
    if error is not None or layout is None:
        detail = error or "interpreter layout is unavailable"
        report = _error_report(mode, detail)
        return SandboxResult(2, report, "", detail, detail)
    command, error = _sandbox_command(config, layout, str(bwrap_path))
    if error is not None or command is None:
        detail = error or "cannot construct sandbox command"
        report = _error_report(mode, detail)
        return SandboxResult(2, report, "", detail, detail)

    returncode, stdout_bytes, stderr_bytes, timed_out, process_error = _run_process(
        [*command, "--mode", mode], timeout
    )
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = _bounded_text(stderr_bytes)
    if process_error is not None:
        detail = process_error
        if timed_out:
            detail = f"sandbox timed out after {timeout:g} seconds; {detail}"
        process_report = _error_report(mode, detail)
        # Preserve a non-zero child status.  A clean process with a capture or
        # termination error is still a launcher failure.
        status = returncode if returncode != 0 else 1
        return SandboxResult(status, process_report, stdout, stderr, detail, timed_out)

    decoded_report, parse_error = _decode_report(stdout_bytes, mode)
    if parse_error is not None:
        detail = parse_error
        if timed_out:
            detail = f"sandbox timed out after {timeout:g} seconds; {detail}"
        elif stderr:
            detail = f"{detail}; sandbox diagnostics: {stderr}"
        error_report = _error_report(mode, detail)
        # A clean process with malformed/empty output is a launcher failure.  A
        # non-zero child status remains observable and is never converted to pass.
        status = returncode if returncode != 0 else 1
        return SandboxResult(status, error_report, stdout, stderr, detail, timed_out)
    return SandboxResult(returncode, decoded_report, stdout, stderr, None, timed_out)


def _write_output(path: Path, report: dict[str, Any]) -> str | None:
    try:
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        return f"cannot write requested output {path}: {exc}"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = _run_sandbox(args.mode, args.timeout)
    report = result.report
    if report is None:
        report = _error_report(
            args.mode, result.error or "sandbox did not produce a report"
        )
    # Valid child JSON is preserved byte-for-byte on stdout.  Error envelopes are
    # generated only for blocked/malformed launcher outcomes.
    if result.error is None:
        sys.stdout.write(result.stdout)
        if result.stdout and not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            sys.stderr.write("\n")
    if args.output is not None:
        output_error = _write_output(args.output, report)
        if output_error is not None:
            sys.stderr.write(output_error + "\n")
            return 1
    if result.timed_out and result.returncode == 0:
        return 1
    return result.returncode if 0 <= result.returncode <= 125 else 1


if __name__ == "__main__":
    raise SystemExit(main())

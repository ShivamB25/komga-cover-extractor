"""Run-owned diagnostics and explicitly authorized local output."""
from __future__ import annotations

import cProfile
import json
import logging
import marshal
import os
import pstats
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic
from types import TracebackType

from ..domain.decisions import Decision, RunAuthorization, RunResult, Status

LOG_FILENAME = "run.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Only registered reason codes cross the persistence boundary. Unknown text may
# contain transport credentials or decoder stderr even when it looks like a code.
_SAFE_REASONS = frozenset({
    "preview_only", "empty_plan", "local_commit_complete", "already_complete",
    "operation_already_complete", "operation_id_requires_recovery",
    "another_operation_requires_recovery", "operation_needs_review",
    "cleanup_pending", "lock_release_failed", "publication_pending",
    "artwork_unchanged", "book_cover_absent", "cover_already_exists",
    "cover_not_found", "source_changed", "destination_changed", "range_limit",
    "invalid_selected_roots", "no_selected_roots", "outside_authorized_root",
    "symlink_traversal", "missing_path", "not_regular_file", "cancelled",
    "manual_choice_required", "unknown_command", "operation_id_required",
    "diagnostic_detail_omitted", "log_open_failed", "log_write_failed",
    "log_close_failed", "observability_not_open", "observability_cancelled",
    "unsafe_output_directory", "unsafe_output_file", "output_path_changed",
    "invalid_output_path", "profile_output_exists", "profile_open_failed",
    "profile_write_failed", "profile_output_changed",
})


def _safe_decision(decision: Decision) -> Decision:
    reason = decision.reason if decision.reason in _SAFE_REASONS else "diagnostic_detail_omitted"
    return Decision(decision.status, reason, decision.path)


def render_diagnostic(decision: Decision) -> None:
    """Render typed local context on stderr without raw error or payload text."""
    safe = _safe_decision(decision)
    path = f" {json.dumps(str(safe.path), ensure_ascii=True)}" if safe.path is not None else ""
    print(f"{safe.status}: {safe.reason}{path}", file=sys.stderr)


class _OutputFailure(RuntimeError):
    def __init__(self, reason: str, status: Status = "review_required") -> None:
        self.reason = reason
        self.status = status
        super().__init__(reason)


def _absolute_path(path: Path) -> Path:
    if ".." in path.parts or not path.name:
        raise _OutputFailure("invalid_output_path", "invalid_input")
    return Path(os.path.abspath(path))


def _open_directory(path: Path, *, create: bool, private: bool = False) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            created = False
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            try:
                if created:
                    os.fchmod(child, 0o700)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise _OutputFailure("unsafe_output_directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_regular(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise _OutputFailure("unsafe_output_file")


def _file_info(directory: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _check_regular(info)
    return info


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_identity(directory: int, name: str, descriptor: int) -> None:
    current = _file_info(directory, name)
    opened = os.fstat(descriptor)
    _check_regular(opened)
    if current is None or _identity(current) != _identity(opened):
        raise _OutputFailure("output_path_changed")


def _open_output(directory: int, name: str, *, exclusive: bool) -> int:
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if not exclusive:
        flags |= os.O_APPEND
    try:
        descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    except FileExistsError:
        if exclusive:
            raise
        before = _file_info(directory, name)
        descriptor = os.open(name, flags | os.O_APPEND, dir_fd=directory)
        try:
            if before is None or _identity(before) != _identity(os.fstat(descriptor)):
                raise _OutputFailure("output_path_changed")
        except BaseException:
            os.close(descriptor)
            raise
    else:
        try:
            os.fchmod(descriptor, 0o600)
        except BaseException:
            try:
                _discard_output(directory, name, os.fstat(descriptor))
            finally:
                os.close(descriptor)
            raise
    try:
        _check_identity(directory, name, descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _discard_output(directory: int, name: str, owned: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and _identity(current) == _identity(owned):
            os.unlink(name, dir_fd=directory)
    except OSError:
        # Never replace or unlink a different writer's file to clean up output.
        pass


class _PrivateRotatingHandler(RotatingFileHandler):
    def __init__(self, path: Path, directory: int) -> None:
        self._directory = directory
        self._log_filename = path.name
        self._path = path.parent
        super().__init__(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                         encoding="utf-8", errors="strict")

    def _check_directory(self) -> None:
        current = _open_directory(self._path, create=False, private=True)
        try:
            if _identity(os.fstat(current)) != _identity(os.fstat(self._directory)):
                raise _OutputFailure("output_path_changed")
        finally:
            os.close(current)

    def _open(self) -> TextIOWrapper:
        self._check_directory()
        descriptor = _open_output(self._directory, self._log_filename, exclusive=False)
        try:
            return os.fdopen(descriptor, "a", encoding="utf-8", errors="strict")
        except BaseException:
            os.close(descriptor)
            raise

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        self._check_directory()
        if self.stream is None:
            self.stream = self._open()
        _check_identity(self._directory, self._log_filename, self.stream.fileno())
        length = len((self.format(record) + self.terminator).encode("utf-8"))
        if length >= self.maxBytes:
            raise _OutputFailure("log_write_failed", "publication_failed")
        return os.fstat(self.stream.fileno()).st_size + length >= self.maxBytes

    def doRollover(self) -> None:
        for index in range(self.backupCount + 1):
            _file_info(self._directory, self._log_filename if index == 0 else f"{self._log_filename}.{index}")
        if self.stream is not None:
            self.stream.flush()
            _check_identity(self._directory, self._log_filename, self.stream.fileno())
            self.stream.close()
            self.stream = None
        oldest = f"{self._log_filename}.{self.backupCount}"
        if _file_info(self._directory, oldest) is not None:
            os.unlink(oldest, dir_fd=self._directory)
        for index in range(self.backupCount - 1, 0, -1):
            source = f"{self._log_filename}.{index}"
            if _file_info(self._directory, source) is not None:
                os.rename(source, f"{self._log_filename}.{index + 1}",
                          src_dir_fd=self._directory, dst_dir_fd=self._directory)
        if _file_info(self._directory, self._log_filename) is not None:
            os.rename(self._log_filename, f"{self._log_filename}.1",
                      src_dir_fd=self._directory, dst_dir_fd=self._directory)
        self.stream = self._open()

    def handleError(self, record: logging.LogRecord) -> None:
        # The default handler prints exception text and entire records to stderr.
        raise _OutputFailure("log_write_failed", "publication_failed") from None


class RunObservability:
    """A scoped consumer of result counts, not a second source of commit truth."""

    def __init__(self, *, log_dir: Path, log_to_file: bool,
                 authorization: RunAuthorization,
                 cancel: Callable[[], bool] | None = None) -> None:
        self._log_dir = log_dir
        self._enabled = authorization.apply and log_to_file
        self._cancel = cancel
        self._counts = {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
        self._started: float | None = None
        self._ended: float | None = None
        self._logger: logging.Logger | None = None
        self._handler: _PrivateRotatingHandler | None = None
        self._directory: int | None = None
        self._failure: Decision | None = None
        self._log_path: Path | None = None

    @property
    def counts(self) -> dict[str, int]:
        return self._counts.copy()

    @property
    def elapsed_seconds(self) -> float:
        if self._started is None:
            return 0.0
        end = self._ended if self._ended is not None else monotonic()
        return max(0.0, end - self._started)

    @property
    def failure(self) -> Decision | None:
        return self._failure

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    def __enter__(self) -> RunObservability:
        if self._started is not None:
            raise RuntimeError("observability scope cannot be reused")
        self._started = monotonic()
        if not self._enabled:
            return self
        if self._cancel is not None and self._cancel():
            self._failure = Decision("cancelled", "observability_cancelled")
            return self
        try:
            path = _absolute_path(self._log_dir) / LOG_FILENAME
            self._directory = _open_directory(path.parent, create=True, private=True)
            self._handler = _PrivateRotatingHandler(path, self._directory)
            self._handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger = logging.Logger("komga_cover_extractor.run", level=logging.INFO)
            self._logger.propagate = False
            self._logger.addHandler(self._handler)
            self._log_path = path
        except _OutputFailure as error:
            self._failure = Decision(error.status, error.reason)
        except OSError:
            self._failure = Decision("publication_failed", "log_open_failed")
        if self._failure is not None and self._directory is not None:
            os.close(self._directory)
            self._directory = None
        return self

    def _write(self, payload: dict[str, object]) -> Decision | None:
        if self._failure is not None:
            return self._failure
        if self._started is None or self._ended is not None:
            return Decision("review_required", "observability_not_open")
        if self._logger is None:
            return None
        try:
            self._logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        except _OutputFailure as error:
            self._failure = Decision(error.status, error.reason)
        except OSError:
            self._failure = Decision("publication_failed", "log_write_failed")
        return self._failure

    def record(self, result: RunResult) -> Decision | None:
        """Accumulate group counts even if optional logging fails after a commit."""
        if self._started is None or self._ended is not None:
            return Decision("review_required", "observability_not_open")
        counts = {"planned": result.planned, "committed": result.committed,
                  "skipped": result.skipped, "failed": result.failed}
        for name, value in counts.items():
            self._counts[name] += value
        if self._logger is None:
            return self._failure
        return self._write({
            "event": "run_result", "counts": counts,
            "run_counts": self._counts, "exit_code": result.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "decisions": [_safe_decision(decision).as_dict() for decision in result.decisions],
        })

    def diagnostic(self, decision: Decision) -> Decision | None:
        render_diagnostic(decision)
        return self._write({"event": "diagnostic", "decision": _safe_decision(decision).as_dict()})

    def close(self) -> Decision | None:
        if self._ended is not None:
            return self._failure
        if self._started is not None:
            self._ended = monotonic()
        try:
            if self._handler is not None:
                if self._logger is not None:
                    self._logger.removeHandler(self._handler)
                self._handler.close()
        except OSError:
            self._failure = Decision("publication_failed", "log_close_failed")
        finally:
            self._handler = None
            self._logger = None
            if self._directory is not None:
                os.close(self._directory)
                self._directory = None
        return self._failure

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()


def run_profiled(function: Callable[[], RunResult], path: Path | None) -> RunResult:
    """Profile an internal service callable into an exclusively requested output."""
    if path is None:
        return function()
    directory: int | None = None
    descriptor: int | None = None
    owned: os.stat_result | None = None
    keep = False
    output: Path | None = None
    try:
        try:
            output = _absolute_path(path)
            directory = _open_directory(output.parent, create=False)
            descriptor = _open_output(directory, output.name, exclusive=True)
            owned = os.fstat(descriptor)
        except FileExistsError:
            return RunResult((Decision("conflict", "profile_output_exists", path),))
        except _OutputFailure as error:
            return RunResult((Decision(error.status, error.reason, path),))
        except OSError:
            return RunResult((Decision("publication_failed", "profile_open_failed", path),))

        profiler = cProfile.Profile()
        result = profiler.runcall(function)
        try:
            current_directory = _open_directory(output.parent, create=False)
            try:
                if _identity(os.fstat(current_directory)) != _identity(os.fstat(directory)):
                    raise _OutputFailure("output_path_changed")
            finally:
                os.close(current_directory)
            _check_identity(directory, output.name, descriptor)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                marshal.dump(getattr(pstats.Stats(profiler), "stats"), stream)
                stream.flush()
                os.fsync(stream.fileno())
                _check_identity(directory, output.name, stream.fileno())
            os.fsync(directory)
            keep = True
        except _OutputFailure:
            decision = Decision("review_required", "profile_output_changed", output)
            return replace(result, decisions=(*result.decisions, decision))
        except OSError:
            decision = Decision("publication_failed", "profile_write_failed", output)
            return replace(result, decisions=(*result.decisions, decision))
        return result
    finally:
        try:
            if not keep and directory is not None and output is not None and owned is not None:
                _discard_output(directory, output.name, owned)
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                if directory is not None:
                    os.close(directory)

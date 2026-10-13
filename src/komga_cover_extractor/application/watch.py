"""A single pipeline owner with bounded intake and fingerprint-based reconciliation."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from ..covers.selection import IMAGE_SUFFIXES
from ..domain.decisions import Decision, RunResult, Status
from ..domain.libraries import LibraryRoot, normalize_library_roots
from ..domain.operations import FileFingerprint
from ..infrastructure.filesystem import (
    BOOK_SUFFIXES,
    FilesystemError,
    checked_path,
    fingerprint,
    matches,
)
from ..infrastructure.state import JobRecord, JobState, StateStore, StateStoreError
from ..infrastructure.watching import (
    WatchEvent,
    WatchSignalError,
    close_observers,
    forward_cancellation,
    start_observer,
    watch_signals,
)
from ..infrastructure.workers import WorkerError, run_worker
from ..infrastructure.zip_archive import ArchivePolicy
from .pipeline import Pipeline, RunRequest

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

WATCH_OPTIONS = (
    "watch_queue_capacity", "watch_max_pending_jobs", "watch_reconcile_interval_seconds",
    "watchdog_file_transferred_check_interval", "watchdog_discover_new_files_check_interval",
    "watch_retry_attempts", "watch_shutdown_timeout_seconds",
)
_DEFAULTS: dict[str, int | float] = dict(zip(WATCH_OPTIONS, (256, 4096, 10, 1, 5, 3, 30), strict=True))
_INTEGER_OPTIONS = frozenset({"watch_queue_capacity", "watch_max_pending_jobs", "watch_retry_attempts"})
Phase = Literal["observed", "waiting_ready", "ready", "running", "committed", "delivery_pending", "complete", "failed", "review_required", "cancelled"]
_PHASES = frozenset({"observed", "waiting_ready", "ready", "running", "committed", "delivery_pending", "complete", "failed", "review_required", "cancelled"})
_JOB_STATES: dict[Phase, JobState] = {
    "observed": "pending", "waiting_ready": "pending", "ready": "pending", "running": "running",
    "committed": "pending", "delivery_pending": "pending", "complete": "complete",
    "failed": "failed", "review_required": "needs_review", "cancelled": "complete",
}
_TRANSIENT_REASONS = frozenset({
    "source_changed", "missing_path", "missing_archive", "invalid_zip_archive",
    "incomplete_multipart_archive", "filesystem_observation_failed", "book_observation_failed",
    "worker_deadline", "worker_terminated", "watch_read_failed", "downloader_not_complete",
})
_REVIEW_STATUSES = frozenset({"ambiguous", "review_required", "conflict", "stale_plan"})
_ERROR_STATUSES = frozenset({"failed", "publication_failed", "permission_denied", "invalid_input", "unsupported", "missing_dependency"})
Sample = tuple[int, int, int, int, int]


class _Job(TypedDict):
    root: Path
    path: Path
    sample: Sample
    fingerprint: FileFingerprint | None
    phase: Phase
    stable_since: float
    next_sample: float
    attempts: int
    operations: tuple[str, ...]
    deferred: tuple[str, ...]
    error: str | None
    interrupted: bool
    reusable: bool


class _Settled(TypedDict):
    fingerprint: FileFingerprint
    operations: tuple[str, ...]
    expires: float
    policy_version: str


def check_archive_readable(observed: FileFingerprint, *, policy: ArchivePolicy, cancel: Event) -> Decision:
    if observed.path.suffix.lower() not in BOOK_SUFFIXES:
        return Decision("observed", "regular_file_ready", observed.path)
    try:
        run_worker(
            "inspect", {"book": str(observed.path), "root": str(observed.root), "archive_policy": policy.as_dict()},
            timeout=policy.conversion_timeout_seconds, cancel=cancel,
        )
        if not matches(observed):
            return Decision("stale_plan", "source_changed", observed.path)
    except WorkerError as error:
        status = cast(Status, error.status if error.status in _ERROR_STATUSES | _REVIEW_STATUSES | {"cancelled", "no_match"} else "failed")
        return Decision(status, error.reason, observed.path)
    except FilesystemError as error:
        return Decision("stale_plan" if error.reason in _TRANSIENT_REASONS else "invalid_input", error.reason, observed.path)
    except PermissionError:
        return Decision("permission_denied", "watch_read_permission_denied", observed.path)
    except OSError:
        return Decision("failed", "watch_read_failed", observed.path)
    return Decision("observed", "archive_readable", observed.path)


def _sample(path: Path, root: Path) -> Sample:
    path = checked_path(path, root)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise FilesystemError("not_regular_file", path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _fingerprint_from_json(value: object, path: Path, root: Path) -> FileFingerprint | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"root", "relative_path", "device", "inode", "size", "mtime_ns", "sha256"}:
        raise ValueError("invalid_watch_job_fingerprint")
    if value["root"] != str(root) or value["relative_path"] != path.relative_to(root).as_posix() or not isinstance(value["sha256"], str):
        raise ValueError("invalid_watch_job_fingerprint")
    integers = [value[name] for name in ("device", "inode", "size", "mtime_ns")]
    if any(type(number) is not int for number in integers):
        raise ValueError("invalid_watch_job_fingerprint")
    numbers = cast(list[int], integers)
    return FileFingerprint(root, path.relative_to(root).as_posix(),
                           numbers[0], numbers[1], numbers[2], numbers[3], value["sha256"])


class WatchCoordinator:
    """Own observers and mutable scheduling state, never a second pipeline."""

    def __init__(
        self, pipeline: Pipeline, request: RunRequest, *, options: Mapping[str, int | float] | None = None,
        state: StateStore | None = None, journal_ready: Callable[[], Decision] | None = None,
        stop: Event | None = None, clock: Callable[[], float] = monotonic,
        wait: Callable[[float], bool] | None = None, polling: bool = False,
        on_ready: Callable[[], None] | None = None, on_result: Callable[[RunResult], None] | None = None,
        downloader_complete: Callable[[Path], bool | None] | None = None,
        candidate_filter: Callable[[Path], bool] | None = None,
        invalidate: Callable[[Path], None] | None = None,
        ignored_roots: tuple[Path, ...] = (),
        readable: Callable[[FileFingerprint], Decision] | None = None,
    ) -> None:
        values = _DEFAULTS | dict(options or {})
        if set(values) != set(WATCH_OPTIONS):
            raise ValueError("unknown_watch_option")
        for name, value in values.items():
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("watch_limits_must_be_positive")
            if name in _INTEGER_OPTIONS and type(value) is not int:
                raise ValueError("watch_counts_must_be_integers")
        self.pipeline = pipeline
        self.request = request
        self.roots = normalize_library_roots(request.roots or pipeline.roots)
        self.watch_roots = tuple(root for root in self.roots if "download" in root.roles) or self.roots
        self.options = values
        self.state = state
        self.journal_ready = journal_ready
        self.stop = pipeline.cancel if stop is None else stop
        self.clock = clock
        self._wake = Event()
        self._wait = self._wake.wait if wait is None else wait
        self.polling = polling
        self.on_ready = on_ready
        self.on_result = on_result
        self.downloader_complete = downloader_complete
        self.candidate_filter = candidate_filter
        self.invalidate = invalidate
        self.ignored_roots = tuple(path.absolute() for path in (pipeline.state_dir, *ignored_roots))
        self._readable = readable or (lambda observed: check_archive_readable(observed, policy=pipeline.archive, cancel=pipeline.cancel))
        self._capacity = int(values["watch_queue_capacity"])
        self._limit = int(values["watch_max_pending_jobs"])
        self._sample_interval = float(values["watchdog_file_transferred_check_interval"])
        self._quiet = float(values["watchdog_discover_new_files_check_interval"])
        self._reconcile_interval = float(values["watch_reconcile_interval_seconds"])
        self._retries = int(values["watch_retry_attempts"])
        self._shutdown = float(values["watch_shutdown_timeout_seconds"])
        self._queue: deque[WatchEvent] = deque()
        self._lock = Lock()
        self._overflow = False
        self._accepting = True
        self._started = False
        self._notified_ready = False
        self._closed = False
        self._persistent = False
        self._pending: dict[Path, _Job] = {}
        self._obligations: dict[str, _Job] = {}
        self._settled: OrderedDict[Path, _Settled] = OrderedDict()
        self._observers: dict[Path, BaseObserver] = {}
        self._root_identities: dict[Path, tuple[int, int] | None] = {}
        self._reconcile_needed: set[Path] = set()
        self._scan_rounds: dict[Path, str] = {}
        self._scan_after: dict[Path, tuple[str, ...]] = {}
        self._preview_done: dict[Path, tuple[str, float]] = {}
        self._issues: OrderedDict[tuple[Path, str], Decision] = OrderedDict()
        self._decisions: deque[Decision] = deque(maxlen=self._capacity)
        self._counts = {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
        self._deferred: OrderedDict[str, None] = OrderedDict()
        self._next_reconcile = self.clock()

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._obligations)

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def phases(self) -> dict[Path, Phase]:
        return {path: job["phase"] for path, job in self._pending.items()}

    def enqueue(self, event: WatchEvent) -> None:
        with self._lock:
            if not self._accepting:
                return
            if event[0] == "overflow" or len(self._queue) >= self._capacity:
                self._overflow = True
            else:
                self._queue.append(event)
        self._wake.set()

    def request_stop(self) -> None:
        with self._lock:
            self._accepting = False
        self.stop.set()
        self.pipeline.cancel.set()
        self._wake.set()

    def _root_for(self, path: Path) -> LibraryRoot | None:
        path = path.absolute()
        return next((root for root in self.watch_roots if root.contains(path)), None)

    def _eligible(self, path: Path, root: Path) -> bool:
        if not path.is_relative_to(root) or path == root:
            return False
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            return False
        if any(path.is_relative_to(ignored) for ignored in self.ignored_roots):
            return False
        if path.name.casefold().endswith((".part", ".tmp", ".!qb")):
            return False
        if path.suffix.lower() in IMAGE_SUFFIXES:
            return False
        return self.candidate_filter(path) if self.candidate_filter is not None else path.suffix.lower() in BOOK_SUFFIXES

    def _job_id(self, path: Path, root: Path) -> str:
        value = json.dumps([str(root), path.relative_to(root).as_posix(), self.pipeline.policy_version], ensure_ascii=True, separators=(",", ":"))
        return "watch:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _marker_id(self, root: Path) -> str:
        return "watch-reconcile:" + hashlib.sha256((str(root) + "\0" + self.pipeline.policy_version).encode("utf-8")).hexdigest()

    def _obligation_id(self, job: _Job) -> str:
        observed = job["fingerprint"]
        value = json.dumps([self._job_id(job["path"], job["root"]),
                            observed.as_dict() if observed is not None else None,
                            job["operations"]], sort_keys=True, separators=(",", ":"))
        return "watch-delivery:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _detach_obligation(self, job: _Job) -> None:
        identity = self._obligation_id(job)
        self._obligations[identity] = job
        self._add_deferred(job["deferred"])
        self._save(job)
        if self._pending.get(job["path"]) is job:
            del self._pending[job["path"]]

    def _save(self, job: _Job) -> None:
        if not self._persistent:
            return
        assert self.state is not None
        obligation_id = self._obligation_id(job)
        detached = self._obligations.get(obligation_id) is job
        identity = obligation_id if detached else self._job_id(job["path"], job["root"])
        self.state.put_job(identity, "watch-delivery" if detached else "watch", {
            "schema_version": 1, "policy_version": self.pipeline.policy_version,
            "root": str(job["root"]), "path": str(job["path"]), "sample": list(job["sample"]),
            "fingerprint": job["fingerprint"].as_dict() if job["fingerprint"] is not None else None,
            "phase": job["phase"], "attempts": job["attempts"], "operations": list(job["operations"]),
            "deferred": list(job["deferred"]), "error": job["error"],
            "interrupted": job["interrupted"], "reusable": job["reusable"],
        }, state=_JOB_STATES[job["phase"]], error=job["error"])

    def _marker(self, root: Path, reason: str | None) -> None:
        if self._persistent:
            assert self.state is not None
            self.state.put_job(self._marker_id(root), "watch-reconcile", {
                "schema_version": 1, "root": str(root), "policy_version": self.pipeline.policy_version,
                "phase": "reconciliation_required" if reason is not None else "complete",
            }, state="pending" if reason is not None else "complete", error=reason)

    def _issue(self, path: Path, reason: str, status: Status = "delivery_deferred", candidates: tuple[str, ...] = ()) -> None:
        key = (path, reason)
        self._issues[key] = Decision(status, reason, path, candidates)
        self._issues.move_to_end(key)
        while len(self._issues) > self._limit:
            self._issues.popitem(last=False)

    def _add_deferred(self, identities: tuple[str, ...]) -> None:
        for identity in identities:
            self._deferred[identity] = None
            if len(self._deferred) > self._limit:
                self._deferred.popitem(last=False)
                self._issue(self.pipeline.state_dir, "watch_more_deferred_deliveries_in_journal")

    def _record(self, result: RunResult) -> None:
        self._counts["planned"] += result.planned
        self._counts["committed"] += result.committed
        self._counts["skipped"] += result.skipped
        self._counts["failed"] += result.failed
        self._decisions.extend(result.decisions)
        self._add_deferred(result.deferred_deliveries)
        if self.on_result is not None:
            self.on_result(result)

    def _decode_job(self, record: JobRecord) -> _Job:
        data = record.payload
        if type(data.get("schema_version")) is not int or data.get("schema_version") != 1 or data.get("policy_version") != self.pipeline.policy_version:
            raise ValueError("invalid_watch_job_version")
        root_value, path_value = data.get("root"), data.get("path")
        if not isinstance(root_value, str) or not isinstance(path_value, str):
            raise ValueError("invalid_watch_job_path")
        root, path = Path(root_value), Path(path_value)
        if root not in {item.path for item in self.watch_roots} or not path.is_absolute() or ".." in path.parts or not self._eligible(path, root):
            raise ValueError("invalid_watch_job_path")
        phase, attempts, raw_sample = data.get("phase"), data.get("attempts"), data.get("sample")
        if phase not in _PHASES or type(attempts) is not int or attempts < 0 or not isinstance(raw_sample, list) or len(raw_sample) != 5 or any(type(value) is not int for value in raw_sample):
            raise ValueError("invalid_watch_job_state")
        operations, deferred = data.get("operations"), data.get("deferred")
        if not isinstance(operations, list) or not isinstance(deferred, list) or any(not isinstance(value, str) for value in (*operations, *deferred)):
            raise ValueError("invalid_watch_job_results")
        error = data.get("error")
        if error is not None and not isinstance(error, str) or type(data.get("interrupted")) is not bool or type(data.get("reusable")) is not bool:
            raise ValueError("invalid_watch_job_state")
        now = self.clock()
        return {
            "root": root, "path": path, "sample": cast(Sample, tuple(raw_sample)),
            "fingerprint": _fingerprint_from_json(data.get("fingerprint"), path, root),
            "phase": cast(Phase, phase), "stable_since": now, "next_sample": now + self._sample_interval,
            "attempts": attempts, "operations": tuple(operations), "deferred": tuple(deferred),
            "error": error, "interrupted": cast(bool, data["interrupted"]), "reusable": cast(bool, data["reusable"]),
        }

    def _restore(self) -> None:
        assert self.state is not None
        for kind in ("watch-delivery", "watch"):
            after: str | None = None
            while not self.stop.is_set():
                records = self.state.list_jobs(
                    ("pending", "running", "needs_review") if kind == "watch-delivery" else ("pending", "running"),
                    kind=kind, after_job_id=after, limit=128,
                )
                if not records:
                    break
                for record in records:
                    after = record.job_id
                    if record.payload.get("policy_version") != self.pipeline.policy_version:
                        continue
                    if kind == "watch-delivery" and record.job_id in self._obligations:
                        continue
                    if kind == "watch" and Path(str(record.payload.get("path"))) in self._pending:
                        continue
                    try:
                        job = self._decode_job(record)
                    except (ValueError, TypeError):
                        self._issue(self.pipeline.state_dir, "invalid_durable_watch_job", "review_required", (record.job_id,))
                        continue
                    self._add_deferred(job["deferred"])
                    if self.pending_count >= self._limit:
                        self._reconcile_needed.add(job["root"])
                        self._marker(job["root"], "pending_limit")
                        continue
                    if job["phase"] == "running":
                        job["interrupted"] = True
                        job["phase"] = "review_required"
                        job["error"] = "watch_interrupted_job"
                        self._issue(job["path"], "watch_interrupted_job", "review_required", job["operations"])
                        self._save(job)
                        continue
                    if kind == "watch-delivery":
                        self._obligations[record.job_id] = job
                    elif self._obligation_id(job) not in self._obligations:
                        if job["phase"] not in {"committed", "delivery_pending"}:
                            job["phase"] = "waiting_ready"
                        self._pending[job["path"]] = job

    def start(self) -> Decision:
        if self._closed or self._started:
            return Decision("review_required", "watch_scope_not_reusable")
        if not self.watch_roots:
            return Decision("invalid_input", "no_watch_roots")
        if self.stop.is_set() or self.pipeline.cancel.is_set():
            return Decision("cancelled", "shutdown_requested")
        if self.request.authorization.apply:
            if self.state is None or self.journal_ready is None:
                return Decision("missing_dependency", "watch_state_initialization_required")
            decision = self.journal_ready()
            if decision.status != "observed":
                return decision
            self._persistent = True
            self._restore()
        self._started = True
        self.reconcile()
        self._maybe_ready()
        return Decision("observed", "watch_started")

    def _maybe_ready(self) -> None:
        if self._started and not self._notified_ready and not self.stop.is_set() and not self._reconcile_needed and all(
            root.path in self._observers and self._root_identities.get(root.path) is not None
            for root in self.watch_roots
        ):
            self._notified_ready = True
            if self.on_ready is not None:
                self.on_ready()

    def _refresh_observers(self) -> None:
        for root in self.watch_roots:
            path = root.path
            previous = self._root_identities.get(path)
            try:
                checked_path(path, path)
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise FilesystemError("watch_root_not_directory", path)
                current: tuple[int, int] | None = (info.st_dev, info.st_ino)
            except (FileNotFoundError, PermissionError):
                current = None
            except FilesystemError as error:
                if error.reason == "missing_path":
                    current = None
                else:
                    self._issue(path, error.reason, "invalid_input")
                    current = None
            except OSError:
                current = None
            observer = self._observers.get(path)
            dead = observer is not None and (not observer.is_alive() or not all(emitter.is_alive() for emitter in observer.emitters))
            if observer is not None and (current != previous or dead):
                self._decisions.extend(close_observers((observer,), timeout=self._shutdown))
                del self._observers[path]
                observer = None
            self._root_identities[path] = current
            if current is None:
                self._issue(path, "watch_root_unavailable")
                self._marker(path, "root_unavailable")
                self._reconcile_needed.add(path)
                for job in self._pending.values():
                    if job["root"] == path and job["phase"] not in {"committed", "delivery_pending"}:
                        job["stable_since"] = self.clock()
                continue
            self._issues.pop((path, "watch_root_unavailable"), None)
            if previous != current or dead:
                self._scan_rounds.pop(path, None)
                self._scan_after.pop(path, None)
                self._preview_done.pop(path, None)
                for candidate in tuple(self._settled):
                    if candidate.is_relative_to(path):
                        del self._settled[candidate]
                if self.invalidate is not None:
                    self.invalidate(path)
            if self._started and observer is None:
                try:
                    self._observers[path] = start_observer(path, self.enqueue, capacity=self._capacity, polling=self.polling, interval=self._sample_interval)
                except ImportError:
                    self._issue(path, "watch_extra_required", "missing_dependency")
                    self.request_stop()
                except NotImplementedError:
                    self._issue(path, "native_watch_requires_linux_or_explicit_polling", "unsupported")
                    self.request_stop()
                except (OSError, RuntimeError):
                    self._issue(path, "watch_observer_start_failed")
                    self._marker(path, "observer_start_failed")
                    self._reconcile_needed.add(path)

    def _outputs_current(self, operations: tuple[str, ...]) -> bool:
        if not operations:
            return True
        if self.state is None:
            return False
        expected: dict[Path, FileFingerprint | None] = {}
        for identity in operations:
            record = self.state.get_operation(identity)
            if record is None or record.phase not in {"committed", "cleanup_pending", "complete"}:
                return False
            for member in self.state.list_members(identity):
                if member.kind in {"publish", "move"}:
                    if member.published_fingerprint is None:
                        return False
                    expected[member.destination] = member.published_fingerprint
                if member.kind in {"move", "quarantine"} and member.source is not None:
                    expected[member.source.path] = None
        try:
            return all(not path.exists() if observed is None else matches(observed) for path, observed in expected.items())
        except OSError:
            return False

    def _remember(self, job: _Job) -> None:
        observed = job["fingerprint"]
        if observed is None:
            return
        self._settled[job["path"]] = {
            "fingerprint": observed, "operations": job["operations"],
            "expires": self.clock() + 300, "policy_version": self.pipeline.policy_version,
        }
        self._settled.move_to_end(job["path"])
        while len(self._settled) > self._limit:
            self._settled.popitem(last=False)

    def _discover(self, path: Path, root: Path, *, observed: FileFingerprint | None = None) -> None:
        if self.stop.is_set() or not self._eligible(path, root):
            return
        try:
            sample = _sample(path, root)
            if observed is None:
                observed = fingerprint(path, root)
        except FilesystemError as error:
            if error.reason not in _TRANSIENT_REASONS:
                self._issue(path, error.reason, "invalid_input")
                return
            try:
                sample = _sample(path, root)
            except OSError:
                return
            observed = None
        except FileNotFoundError:
            return
        except PermissionError:
            self._issue(path, "watch_read_permission_denied", "permission_denied")
            return
        except OSError:
            self._issue(path, "watch_read_failed", "failed")
            return
        pending = self._pending.get(path)
        if pending is not None and pending["sample"] == sample and (observed is None or pending["fingerprint"] == observed):
            return
        if pending is not None and pending["phase"] in {"committed", "delivery_pending"}:
            self._detach_obligation(pending)
        settled = self._settled.get(path)
        if observed is not None and settled is not None and settled["policy_version"] == self.pipeline.policy_version and settled["fingerprint"] == observed and settled["expires"] > self.clock() and self._outputs_current(settled["operations"]):
            self._settled.move_to_end(path)
            return
        if self.state is not None and observed is not None:
            artifact = self.state.committed_artifact(path)
            if artifact is not None and artifact.published == observed and json.loads(artifact.plan_json).get("policy_version") == self.pipeline.policy_version:
                return
            record = self.state.get_job(self._job_id(path, root))
            if record is not None:
                try:
                    prior = self._decode_job(record)
                except (ValueError, TypeError):
                    self._issue(path, "invalid_durable_watch_job", "review_required", (record.job_id,))
                    return
                if prior["interrupted"] or prior["phase"] == "running":
                    if prior["fingerprint"] is None or prior["fingerprint"] == observed:
                        self._issue(path, "watch_interrupted_job", "review_required", prior["operations"])
                        return
                    if self._obligation_id(prior) not in self._obligations:
                        if self.pending_count >= self._limit:
                            self._reconcile_needed.add(root)
                            self._marker(root, "pending_limit")
                            return
                        prior["interrupted"] = True
                        prior["phase"] = "review_required"
                        prior["error"] = prior["error"] or "watch_interrupted_job"
                        self._detach_obligation(prior)
                if prior["fingerprint"] == observed:
                    if prior["phase"] in {"committed", "delivery_pending"}:
                        if self._obligation_id(prior) in self._obligations:
                            return
                        if self.pending_count < self._limit:
                            self._pending[path] = prior
                            self._add_deferred(prior["deferred"])
                        else:
                            self._reconcile_needed.add(root)
                            self._marker(root, "pending_limit")
                        return
                    if prior["phase"] in {"failed", "review_required"}:
                        self._issue(path, prior["error"] or "watch_job_requires_review", "review_required" if prior["phase"] == "review_required" else "failed", prior["operations"])
                        return
                    if prior["phase"] == "complete" and prior["reusable"] and self._outputs_current(prior["operations"]):
                        self._remember(prior)
                        return
                elif prior["phase"] in {"committed", "delivery_pending"} and self._obligation_id(prior) not in self._obligations:
                    if self.pending_count < self._limit:
                        self._detach_obligation(prior)
                    else:
                        self._reconcile_needed.add(root)
                        self._marker(root, "pending_limit")
                        return
        if self.pending_count >= self._limit and path not in self._pending:
            self._issue(root, "watch_pending_limit")
            self._reconcile_needed.add(root)
            self._marker(root, "pending_limit")
            return
        if self.invalidate is not None:
            self.invalidate(path)
        self._settled.pop(path, None)
        for key in tuple(self._issues):
            if key[0] == path and not key[1].startswith("watch_delivery"):
                del self._issues[key]
        now = self.clock()
        job: _Job = {
            "root": root, "path": path, "sample": sample, "fingerprint": observed,
            "phase": "waiting_ready", "stable_since": now, "next_sample": now + self._sample_interval,
            "attempts": 0, "operations": (), "deferred": (), "error": None,
            "interrupted": False, "reusable": False,
        }
        self._pending[path] = job
        self._save(job)

    def _remove(self, path: Path, *, directory: bool = False) -> None:
        candidates = tuple(candidate for candidate in self._pending if candidate == path or directory and candidate.is_relative_to(path))
        for candidate in candidates:
            job = self._pending[candidate]
            if job["phase"] in {"committed", "delivery_pending"}:
                continue
            del self._pending[candidate]
            job["phase"] = "cancelled"
            job["error"] = "watch_source_removed"
            self._save(job)
        for candidate in tuple(self._settled):
            if candidate == path or directory and candidate.is_relative_to(path):
                del self._settled[candidate]
        if self.invalidate is not None:
            self.invalidate(path)

    def _walk_candidates(self, root: Path, scope: Path) -> Iterator[Path]:
        checked_path(scope, root)
        def walk_error(error: OSError) -> None:
            raise error
        for directory, folders, files in os.walk(scope, followlinks=False, onerror=walk_error):
            if self.stop.is_set():
                return
            folders[:] = sorted(name for name in folders if not name.startswith(".") and not any(
                (Path(directory) / name).is_relative_to(ignored) for ignored in self.ignored_roots
            ))
            for name in sorted(files):
                path = Path(directory) / name
                if self._eligible(path, root):
                    yield path

    @staticmethod
    def _cursor_key(path: Path, root: Path) -> tuple[str, ...]:
        relative = path.relative_to(root)
        return (*tuple(value for part in relative.parent.parts for value in ("1", part)), "0", relative.name)

    def _scan(self, root: Path, scope: Path) -> None:
        if not scope.is_relative_to(root) or any(scope.is_relative_to(ignored) for ignored in self.ignored_roots):
            return
        missing = {path for path, job in self._pending.items() if job["root"] == root and path.is_relative_to(scope)}
        saturated = False
        root_round = scope == root
        preview_round = not self.request.authorization.apply and root_round
        digest: str | None = None
        try:
            if preview_round:
                hasher = hashlib.sha256(self.pipeline.policy_version.encode("utf-8"))
                for path in self._walk_candidates(root, root):
                    try:
                        observed = fingerprint(path, root)
                    except FilesystemError as error:
                        self._issue(path, error.reason, "delivery_deferred" if error.reason in _TRANSIENT_REASONS else "invalid_input")
                        hasher.update(json.dumps([str(path), error.reason], ensure_ascii=True).encode("utf-8"))
                    except PermissionError:
                        self._issue(path, "watch_read_permission_denied", "permission_denied")
                        hasher.update(json.dumps([str(path), "permission_denied"], ensure_ascii=True).encode("utf-8"))
                    except OSError:
                        self._issue(path, "watch_read_failed", "failed")
                        hasher.update(json.dumps([str(path), "read_failed"], ensure_ascii=True).encode("utf-8"))
                    else:
                        hasher.update(json.dumps(observed.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
                    hasher.update(b"\\n")
                digest = hasher.hexdigest()
                completed = self._preview_done.get(root)
                if completed is not None and completed[0] == digest and completed[1] > self.clock():
                    return
                if self._scan_rounds.get(root) != digest:
                    self._scan_rounds[root] = digest
                    self._scan_after.pop(root, None)
            after = self._scan_after.get(root) if root_round else None
            for path in self._walk_candidates(root, scope):
                missing.discard(path)
                cursor = self._cursor_key(path, root)
                if after is not None and cursor <= after:
                    continue
                if self.pending_count >= self._limit:
                    saturated = True
                    break
                self._discover(path, root)
                if self.pending_count >= self._limit:
                    if root_round:
                        self._scan_after[root] = cursor
                    saturated = True
                    break
        except (OSError, ValueError):
            self._issue(scope, "watch_reconciliation_incomplete")
            self._reconcile_needed.add(root)
            self._marker(root, "reconciliation_incomplete")
            return
        if saturated:
            self._reconcile_needed.add(root)
            self._marker(root, "pending_limit")
            return
        if preview_round and digest is not None:
            self._preview_done[root] = (digest, self.clock() + 300)
        if root_round:
            self._scan_after.pop(root, None)
        for path in missing:
            self._remove(path)
        self._issues.pop((root, "watch_pending_limit"), None)
        self._issues.pop((scope, "watch_reconciliation_incomplete"), None)
        self._marker(root, None)

    def reconcile(self, scopes: tuple[Path, ...] = ()) -> None:
        if self._persistent:
            self._restore()
        self._refresh_observers()
        selected = scopes or tuple(root.path for root in self.watch_roots)
        for scope in selected:
            root = self._root_for(scope)
            if root is None or self._root_identities.get(root.path) is None:
                continue
            self._reconcile_needed.discard(root.path)
            self._scan(root.path, scope)
        self._maybe_ready()
        self._next_reconcile = self.clock() + self._reconcile_interval

    def _drain(self) -> None:
        with self._lock:
            events = tuple(self._queue)
            self._queue.clear()
            overflow = self._overflow
            self._overflow = False
            self._wake.clear()
        scopes: set[Path] = set()
        for kind, source, destination, directory in events:
            source = source.absolute()
            root = self._root_for(source)
            if kind in {"deleted", "moved"} and root is not None:
                if source == root.path:
                    scopes.add(root.path)
                else:
                    self._remove(source, directory=directory)
            if kind in {"created", "modified"} and root is not None:
                if directory:
                    if kind == "created":
                        scopes.add(source)
                else:
                    self._discover(source, root.path)
            if kind == "moved" and destination is not None:
                destination = destination.absolute()
                target_root = self._root_for(destination)
                if target_root is not None:
                    if directory:
                        scopes.add(destination)
                    else:
                        self._discover(destination, target_root.path)
        if overflow:
            for root in self.watch_roots:
                self._marker(root.path, "event_overflow")
                self._reconcile_needed.add(root.path)
            scopes = {root.path for root in self.watch_roots}
        if scopes:
            self.reconcile(tuple(sorted(scopes)))

    def _finish(self, job: _Job, phase: Phase, *, error: str | None = None) -> None:
        job["phase"] = phase
        job["error"] = error
        self._save(job)
        obligation_id = self._obligation_id(job)
        if self._obligations.get(obligation_id) is job:
            del self._obligations[obligation_id]
        elif self._pending.get(job["path"]) is job:
            self._remember(job)
            del self._pending[job["path"]]

    def _retry(self, job: _Job, decision: Decision) -> None:
        job["attempts"] += 1
        if decision.reason in _TRANSIENT_REASONS and job["attempts"] < self._retries:
            job["phase"] = "waiting_ready"
            job["stable_since"] = self.clock()
            job["next_sample"] = self.clock() + self._sample_interval
            job["error"] = decision.reason
            self._save(job)
            return
        phase: Phase = "review_required" if decision.status in _REVIEW_STATUSES else "failed"
        self._finish(job, phase, error=decision.reason)
        self._issue(job["path"], decision.reason, decision.status, job["operations"])
        self._record(RunResult((decision,), failed=1 if phase == "failed" else 0))

    def _settle_obligations(self, job: _Job) -> None:
        if job["phase"] == "review_required":
            self._issue(job["path"], job["error"] or "watch_job_requires_review",
                        "review_required", job["operations"])
            return
        if self.state is None or not job["operations"]:
            self._issue(job["path"], "watch_delivery_pending", candidates=job["deferred"])
            return
        for identity in job["operations"]:
            operation = self.state.get_operation(identity)
            if operation is None or operation.phase not in {"committed", "cleanup_pending", "complete"}:
                self._issue(job["path"], "watch_operation_requires_recovery", "review_required", job["operations"])
                return
            if operation.phase == "cleanup_pending" and self.request.authorization.apply:
                if self.clock() < job["next_sample"]:
                    return
                if job["attempts"] >= self._retries:
                    self._issue(job["path"], "watch_cleanup_retry_limit", "review_required", job["operations"])
                    return
                job["attempts"] += 1
                job["next_sample"] = self.clock() + self._reconcile_interval
                self._save(job)
                try:
                    recovery = self.pipeline.run(RunRequest(
                        command="recover", operation_id=identity, roots=self.roots,
                        authorization=self.request.authorization,
                        watch=True,
                    ))
                except Exception:
                    self._issue(job["path"], "watch_operation_requires_recovery", "review_required", job["operations"])
                    return
                if self.on_result is not None:
                    self.on_result(RunResult(
                        decisions=recovery.decisions, skipped=recovery.skipped, failed=recovery.failed,
                        deferred_deliveries=recovery.deferred_deliveries, planned_count=0,
                    ))
                operation = self.state.get_operation(identity)
                if operation is None:
                    self._issue(job["path"], "watch_operation_requires_recovery", "review_required", job["operations"])
                    return
            if operation.phase != "complete" or self.state.list_outbox(operation_id=identity, states=("queued", "pending", "unknown", "failed")):
                self._issue(job["path"], "watch_delivery_or_cleanup_pending", candidates=job["operations"])
                return
        for identity in job["deferred"]:
            self._deferred.pop(identity, None)
        self._issues.pop((job["path"], "watch_delivery_pending"), None)
        self._issues.pop((job["path"], "watch_delivery_or_cleanup_pending"), None)
        self._finish(job, "complete")

    def _run_job(self, job: _Job) -> None:
        observed = job["fingerprint"]
        assert observed is not None
        job["phase"] = "running"
        self._save(job)
        try:
            result = self.pipeline.run(RunRequest(
                command="run", books=(job["path"],), roots=self.roots,
                authorization=self.request.authorization, interactive=self.request.interactive,
                watch=True,
            ))
        except Exception:
            job["interrupted"] = True
            self._finish(job, "review_required", error="watch_pipeline_interrupted")
            self._record(RunResult((Decision("review_required", "watch_pipeline_interrupted", job["path"]),), failed=1))
            return
        job["operations"] = tuple(dict.fromkeys([plan.operation_id for plan in result.plans] + [report.operation_id for report in result.plan_reports])) if self.request.authorization.apply else ()
        job["deferred"] = result.deferred_deliveries
        job["reusable"] = bool(result.committed and job["operations"])
        decisions = tuple(decision for decision in result.decisions if decision.status in _ERROR_STATUSES | _REVIEW_STATUSES | {"cancelled", "delivery_deferred"})
        journalled = self.state is not None and any(self.state.get_operation(identity) is not None for identity in job["operations"])
        if result.committed:
            job["attempts"] = 0
            job["error"] = None
            job["phase"] = "committed"
            self._save(job)
            if any(decision.status in _ERROR_STATUSES | _REVIEW_STATUSES for decision in decisions) or result.failed:
                job["interrupted"] = True
                job["phase"] = "review_required"
                review_reason = decisions[0].reason if decisions else "watch_partial_commit"
                job["error"] = review_reason
                self._detach_obligation(job)
                self._issue(job["path"], review_reason, "review_required", job["operations"])
            else:
                job["phase"] = "delivery_pending"
                job["error"] = decisions[0].reason if decisions else None
                self._save(job)
                self._settle_obligations(job)
            self._record(result)
            return
        elif decisions or result.failed:
            first = decisions[0] if decisions else Decision("failed", "watch_pipeline_failed", job["path"])
            if first.status == "cancelled" and not journalled:
                job["phase"] = "waiting_ready"
                job["error"] = first.reason
                self._save(job)
            elif not journalled and first.reason in _TRANSIENT_REASONS:
                self._retry(job, first)
            else:
                phase: Phase = "review_required" if journalled or first.status in _REVIEW_STATUSES else "failed"
                self._finish(job, phase, error=first.reason)
                self._issue(job["path"], first.reason, "review_required" if journalled else first.status, job["operations"])
                self._record(result)
            return
        self._finish(job, "complete")
        self._record(result)

    def tick(self) -> None:
        if self._closed or self.stop.is_set() or self.pipeline.cancel.is_set():
            return
        self._drain()
        if self.clock() >= self._next_reconcile:
            self.reconcile()
        elif self._reconcile_needed and self.pending_count < self._limit:
            self.reconcile(tuple(sorted(self._reconcile_needed)))
        for job in tuple(self._obligations.values()):
            self._settle_obligations(job)
        for job in tuple(self._pending.values()):
            if self.stop.is_set() or self.pipeline.cancel.is_set():
                break
            if job["phase"] in {"committed", "delivery_pending"}:
                self._settle_obligations(job)
                continue
            now = self.clock()
            if now < job["next_sample"] or self._root_identities.get(job["root"]) is None:
                continue
            try:
                sample = _sample(job["path"], job["root"])
            except FilesystemError as error:
                if error.reason == "missing_path":
                    self._refresh_observers()
                    if self._root_identities.get(job["root"]) is not None:
                        self._remove(job["path"])
                else:
                    self._retry(job, Decision("invalid_input", error.reason, job["path"]))
                continue
            except FileNotFoundError:
                self._refresh_observers()
                if self._root_identities.get(job["root"]) is not None:
                    self._remove(job["path"])
                continue
            except OSError:
                self._retry(job, Decision("failed", "watch_read_failed", job["path"]))
                continue
            job["next_sample"] = now + self._sample_interval
            if sample != job["sample"]:
                self._discover(job["path"], job["root"])
                continue
            if now - job["stable_since"] < self._quiet:
                continue
            if self.downloader_complete is not None:
                try:
                    complete = self.downloader_complete(job["path"])
                except OSError:
                    complete = False
                if complete is False:
                    job["phase"] = "waiting_ready"
                    job["error"] = "downloader_not_complete"
                    self._save(job)
                    continue
            try:
                observed = fingerprint(job["path"], job["root"])
            except OSError:
                self._retry(job, Decision("failed", "watch_read_failed", job["path"]))
                continue
            if job["fingerprint"] is not None and observed != job["fingerprint"]:
                self._discover(job["path"], job["root"], observed=observed)
                continue
            job["fingerprint"] = observed
            job["phase"] = "ready"
            self._save(job)
            readable = self._readable(observed)
            if readable.status != "observed":
                if readable.status == "cancelled":
                    job["phase"] = "waiting_ready"
                    self._save(job)
                    break
                self._retry(job, readable)
                continue
            try:
                unchanged = matches(observed)
            except OSError:
                unchanged = False
            if not unchanged:
                self._discover(job["path"], job["root"])
                continue
            self._run_job(job)
            break

    def snapshot(self) -> RunResult:
        return RunResult(
            decisions=(*self._decisions, *self._issues.values()), planned_count=self._counts["planned"],
            committed=self._counts["committed"], skipped=self._counts["skipped"], failed=self._counts["failed"],
            deferred_deliveries=tuple(self._deferred),
        )

    def run(self) -> RunResult:
        decision = self.start()
        if decision.status != "observed":
            self._record(RunResult((decision,)))
            return self.snapshot()
        while not self.stop.is_set() and not self.pipeline.cancel.is_set():
            self.tick()
            if self.request.once and (not self._pending or any(value is None for value in self._root_identities.values()) or all(job["phase"] in {"committed", "delivery_pending"} for job in self._pending.values())):
                break
            self._wait(min(self._sample_interval, 0.25))
        if self.stop.is_set() or self.pipeline.cancel.is_set():
            self._decisions.append(Decision("cancelled", "shutdown_requested"))
        return self.snapshot()

    def close(self) -> None:
        if self._closed:
            return
        self.request_stop()
        self._closed = True
        try:
            for job in (*self._pending.values(), *self._obligations.values()):
                self._save(job)
        finally:
            self._decisions.extend(close_observers(tuple(self._observers.values()), timeout=self._shutdown))
            self._observers.clear()


def run_watch(
    pipeline: Pipeline, request: RunRequest, *, options: Mapping[str, int | float] | None = None,
    state: StateStore | None = None, journal_ready: Callable[[], Decision] | None = None,
    stop: Event | None = None, clock: Callable[[], float] = monotonic,
    wait: Callable[[float], bool] | None = None, polling: bool = False,
    on_ready: Callable[[], None] | None = None, on_result: Callable[[RunResult], None] | None = None,
    downloader_complete: Callable[[Path], bool | None] | None = None,
    candidate_filter: Callable[[Path], bool] | None = None, invalidate: Callable[[Path], None] | None = None,
    ignored_roots: tuple[Path, ...] = (), install_signal_handlers: bool = True,
) -> RunResult:
    try:
        coordinator = WatchCoordinator(
            pipeline, request, options=options, state=state, journal_ready=journal_ready,
            stop=stop, clock=clock, wait=wait, polling=polling, on_ready=on_ready, on_result=on_result,
            downloader_complete=downloader_complete, candidate_filter=candidate_filter,
            invalidate=invalidate, ignored_roots=ignored_roots,
        )
    except (ValueError, TypeError):
        return RunResult((Decision("invalid_input", "invalid_watch_options"),))
    try:
        with watch_signals(pipeline.cancel, coordinator.stop, enabled=install_signal_handlers), forward_cancellation(coordinator.stop, pipeline.cancel):
            try:
                coordinator.run()
            finally:
                coordinator.close()
    except StateStoreError:
        coordinator._decisions.append(Decision("review_required", "watch_state_unavailable"))
    except WatchSignalError:
        coordinator._decisions.append(Decision("invalid_input", "watch_signal_handlers_require_main_thread"))
    except Exception:
        coordinator._decisions.append(Decision("failed", "watch_lifecycle_failed"))
        coordinator._counts["failed"] += 1
    finally:
        try:
            coordinator.close()
        except (StateStoreError, OSError):
            coordinator._decisions.append(Decision("review_required", "watch_checkpoint_failed"))
    return coordinator.snapshot()

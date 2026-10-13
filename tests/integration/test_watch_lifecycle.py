from __future__ import annotations

import builtins
import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Any

import pytest

from komga_cover_extractor.application.pipeline import Pipeline, RunRequest
from komga_cover_extractor.application.watch import (
    WatchCoordinator,
    check_archive_readable,
    run_watch,
)
from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore, StateStoreError
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.watching import (
    WatchEvent,
    close_observers,
    forward_cancellation,
    start_observer,
)
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FixturePipeline(Pipeline):
    def __init__(self, root: Path, state: Path, *, publish: bool = False, defer: bool = False,
                 terminal_hook: bool = False) -> None:
        self.roots = (LibraryRoot(root, roles=("download",)),)
        self.state_dir = state
        self.archive = ArchivePolicy()
        self.policy_version = "watch-integration-v1"
        self.cancel = Event()
        self.publish = publish
        self.defer = defer
        self.terminal_hook = terminal_hook
        self.calls: list[tuple[str, tuple[Path, ...]]] = []

    def run(self, request: RunRequest) -> RunResult:
        self.calls.append((request.command, request.books))
        if request.command == "recover":
            assert request.operation_id is not None
            return TransactionExecutor(self.state_dir, tuple(root.path for root in self.roots)).recover(request.operation_id, authorization=request.authorization)
        if not self.publish:
            return RunResult((Decision("planned", "fixture_preview", request.books[0]),), planned_count=1)
        book = request.books[0]
        source = fingerprint(book, self.roots[0].path)
        target = book.with_name(book.stem + "." + source.sha256[:12] + ".cover.bin")
        plan = OperationPlan(
            self.policy_version, "watch-fixture-" + source.sha256,
            (Operation("cover", "publish", target, artifact=ArtifactSpec(content=b"verified fixture publication")),),
            (RootPermission(self.roots[0].path),),
        )
        result = TransactionExecutor(self.state_dir, (self.roots[0].path,)).apply(plan, authorization=request.authorization)
        if self.defer:
            return replace(result, decisions=(*result.decisions, Decision(
                "review_required" if self.terminal_hook else "delivery_deferred",
                "fixture_remote_unavailable", book,
            )), deferred_deliveries=("fixture-discord-" + source.sha256,))
        return result


def archive(path: Path, data: bytes = b"archive asset") -> None:
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("asset.txt", data)


def ready(state: StateStore) -> Decision:
    state.initialize()
    return Decision("observed", "fixture_state_ready")


def pump(coordinator: WatchCoordinator, clock: Clock, seconds: int) -> None:
    for _ in range(seconds):
        clock.now += 1
        coordinator.tick()


def await_event(events: Queue[WatchEvent], kind: str, source: Path, *, timeout: float = 5) -> WatchEvent:
    deadline = time.monotonic() + timeout
    while True:
        event = events.get(timeout=max(0.001, deadline - time.monotonic()))
        if event[0] == kind and event[1] == source:
            return event
        if time.monotonic() >= deadline:
            raise AssertionError(f"missing {kind} event for owned fixture")


@pytest.mark.parametrize("polling", (False, True))
def test_real_observer_reports_create_modify_move_delete_and_closes_all_emitters(tmp_path: Path, polling: bool) -> None:
    events: Queue[WatchEvent] = Queue()
    observer = start_observer(tmp_path, events.put, polling=polling, interval=0.02)
    emitters = tuple(observer.emitters)
    source = tmp_path / "Series v01.cbz"
    destination = tmp_path / "Series v02.cbz"
    try:
        source.write_bytes(b"first bytes")
        created = await_event(events, "created", source)
        source.write_bytes(b"changed and longer bytes")
        modified = await_event(events, "modified", source)
        source.rename(destination)
        moved = await_event(events, "moved", source)
        destination.unlink()
        deleted = await_event(events, "deleted", destination)
        assert (created, modified, moved, deleted) == (
            ("created", source, None, False), ("modified", source, None, False),
            ("moved", source, destination, False), ("deleted", destination, None, False),
        )
    finally:
        closure = close_observers((observer,), timeout=3)
    assert closure == ()
    assert not observer.is_alive() and all(not emitter.is_alive() for emitter in emitters)


def test_actual_readability_worker_distinguishes_valid_archive_and_unsafe_member(tmp_path: Path) -> None:
    valid = tmp_path / "Series v01.cbz"
    unsafe = tmp_path / "Series v02.cbz"
    archive(valid)
    with zipfile.ZipFile(unsafe, "w") as book:
        book.writestr("../outside.txt", b"not extracted")
    good = check_archive_readable(fingerprint(valid, tmp_path), policy=ArchivePolicy(), cancel=Event())
    bad = check_archive_readable(fingerprint(unsafe, tmp_path), policy=ArchivePolicy(), cancel=Event())
    assert (good.status, good.reason) == ("observed", "archive_readable")
    assert (bad.status, bad.reason) == ("invalid_input", "unsafe_member_name")
    assert valid.read_bytes().startswith(b"PK") and unsafe.read_bytes().startswith(b"PK")
    assert not (tmp_path / "outside.txt").exists()


def test_preview_real_watch_once_streams_one_generation_without_state_or_sidecars(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    archive(book)
    before = book.read_bytes()
    pipeline = FixturePipeline(root, tmp_path / "state")
    results: list[RunResult] = []
    readiness: list[str] = []
    result = run_watch(
        pipeline, RunRequest("watch", once=True),
        options={"watchdog_file_transferred_check_interval": 0.01, "watchdog_discover_new_files_check_interval": 0.03},
        state=StateStore(pipeline.state_dir), journal_ready=lambda: ready(StateStore(pipeline.state_dir)),
        on_ready=lambda: readiness.append("ready"), on_result=results.append,
    )
    assert readiness == ["ready"]
    assert pipeline.calls == [("run", (book,))]
    assert result.planned == 1 and result.committed == 0 and result.exit_code == 0
    assert [item.planned for item in results] == [1]
    assert tuple(path.name for path in root.iterdir()) == ("Series v01.cbz",)
    assert book.read_bytes() == before
    assert not pipeline.state_dir.exists()


def test_apply_pending_jobs_resume_with_a_new_quiet_window_and_real_publication(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    archive(book)
    before = book.read_bytes()
    state = StateStore(tmp_path / "state")
    clock = Clock()
    first = FixturePipeline(root, state.state_dir, publish=True)
    coordinator = WatchCoordinator(first, RunRequest("watch", authorization=RunAuthorization(apply=True)), state=state, journal_ready=lambda: ready(state), clock=clock)
    assert coordinator.start().status == "observed"
    pump(coordinator, clock, 2)
    coordinator.close()
    pending = state.list_jobs(("pending",), kind="watch", limit=10)
    assert len(pending) == 1 and pending[0].payload["phase"] == "waiting_ready"
    second = FixturePipeline(root, state.state_dir, publish=True)
    coordinator = WatchCoordinator(second, RunRequest("watch", authorization=RunAuthorization(apply=True)), state=state, journal_ready=lambda: ready(state), clock=clock)
    try:
        assert coordinator.start().status == "observed"
        pump(coordinator, clock, 4)
        assert second.calls == []
        pump(coordinator, clock, 1)
        assert second.calls == [("run", (book,))]
        assert coordinator.snapshot().committed == 1
        generated = tuple(root.glob("*.cover.bin"))
        assert len(generated) == 1 and generated[0].read_bytes() == b"verified fixture publication"
        assert book.read_bytes() == before
        for event in state.list_outbox(limit=10):
            state.update_outbox(event.event_id, "complete")
        pump(coordinator, clock, 1)
        record = state.list_jobs(kind="watch", limit=10)[0]
        assert record.payload["phase"] == "complete"
    finally:
        coordinator.close()
    third = FixturePipeline(root, state.state_dir, publish=True)
    coordinator = WatchCoordinator(third, RunRequest("watch", authorization=RunAuthorization(apply=True)), state=state, journal_ready=lambda: ready(state), clock=clock)
    try:
        coordinator.start()
        pump(coordinator, clock, 6)
        assert third.calls == []
        assert coordinator.snapshot().committed == 0
        assert generated[0].read_bytes() == b"verified fixture publication"
    finally:
        coordinator.close()


def test_remote_deferral_never_replays_local_commit_and_new_generation_is_independent(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    archive(book, b"first generation")
    state = StateStore(tmp_path / "state")
    clock = Clock()
    pipeline = FixturePipeline(root, state.state_dir, publish=True, defer=True)
    coordinator = WatchCoordinator(pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True)), state=state, journal_ready=lambda: ready(state), clock=clock)
    try:
        coordinator.start()
        pump(coordinator, clock, 20)
        assert pipeline.calls == [("run", (book,))]
        assert coordinator.snapshot().committed == 1
        assert coordinator.phases == {book: "delivery_pending"}
        archive(book, b"second generation")
        coordinator.enqueue(("modified", book, None, False))
        pump(coordinator, clock, 6)
        assert pipeline.calls == [("run", (book,)), ("run", (book,))]
        assert coordinator.snapshot().committed == 2
        assert len(tuple(root.glob("*.cover.bin"))) == 2
        obligations = state.list_jobs(("pending",), kind="watch-delivery", limit=10)
        assert len(obligations) == 1 and obligations[0].payload["phase"] == "delivery_pending"
        assert obligations[0].payload["fingerprint"] != state.list_jobs(kind="watch", limit=10)[0].payload["fingerprint"]
        deferred = coordinator.snapshot().deferred_deliveries
        assert len(deferred) == 2 and deferred[0] != deferred[1]
    finally:
        coordinator.close()
    resumed = FixturePipeline(root, state.state_dir, publish=True, defer=True)
    result = run_watch(
        resumed, RunRequest("watch", authorization=RunAuthorization(apply=True), once=True),
        state=state, journal_ready=lambda: ready(state),
    )
    assert result.deferred_deliveries == deferred
    assert result.committed == 0 and resumed.calls == []
    assert len(tuple(root.glob("*.cover.bin"))) == 2


@pytest.mark.parametrize("settle_outbox", [False, True])
def test_post_commit_review_is_bounded_and_survives_delivery_settlement_and_restart(
    tmp_path: Path, settle_outbox: bool,
) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    for number in range(6):
        archive(root / f"Series v{number:02}.cbz", str(number).encode())
    originals = {path: path.read_bytes() for path in root.iterdir()}
    state = StateStore(tmp_path / "state")
    clock = Clock()
    pipeline = FixturePipeline(root, state.state_dir, publish=True, defer=True, terminal_hook=True)
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True)),
        options={"watch_queue_capacity": 2, "watch_max_pending_jobs": 2},
        state=state, journal_ready=lambda: ready(state), clock=clock,
    )
    try:
        coordinator.start()
        pump(coordinator, clock, 40)
        assert coordinator.snapshot().committed == 2 and coordinator.pending_count == 2
        assert pipeline.calls == [("run", (root / f"Series v{number:02}.cbz",)) for number in range(2)]
        obligations = state.list_jobs(("needs_review",), kind="watch-delivery", limit=10)
        assert len(obligations) == 2
        assert all(record.payload["phase"] == "review_required" for record in obligations)
        pending = state.list_outbox(states=("queued", "pending", "unknown", "failed"), limit=10)
        assert len(pending) == 2
        if settle_outbox:
            for event in pending:
                state.update_outbox(event.event_id, "complete")
            assert all(state.get_operation(event.operation_id).phase == "complete" for event in pending)
            pump(coordinator, clock, 20)
            assert not state.list_outbox(states=("queued", "pending", "unknown", "failed"), limit=10)
        assert coordinator.pending_count == 2 and coordinator.snapshot().committed == 2
        retained = state.list_jobs(kind="watch-delivery", limit=10)
        assert {record.job_id for record in retained} == {record.job_id for record in obligations}
        assert all(record.state == "needs_review" and record.payload["phase"] == "review_required"
                   for record in retained)
        assert all(path.read_bytes() == data for path, data in originals.items())
    finally:
        coordinator.close()
    resumed = FixturePipeline(root, state.state_dir, publish=True, defer=True, terminal_hook=True)
    result = run_watch(
        resumed, RunRequest("watch", authorization=RunAuthorization(apply=True), once=True),
        options={"watch_queue_capacity": 2, "watch_max_pending_jobs": 2},
        state=state, journal_ready=lambda: ready(state),
    )
    assert result.committed == 0 and resumed.calls == []
    assert any(decision.status == "review_required" for decision in result.decisions)
    retained = state.list_jobs(kind="watch-delivery", limit=10)
    assert {record.job_id for record in retained} == {record.job_id for record in obligations}
    assert all(record.state == "needs_review" and record.payload["phase"] == "review_required"
               for record in retained)



def test_parent_journal_rejection_prevents_observers_or_job_writes(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    archive(book)
    pipeline = FixturePipeline(root, tmp_path / "state")
    initialized: list[str] = []
    def rejected() -> Decision:
        initialized.append("authorized apply")
        return Decision("conflict", "fixture_state_lock_owned")
    result = run_watch(pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True), once=True), state=StateStore(pipeline.state_dir), journal_ready=rejected)
    assert result.exit_code == 3
    assert [decision.reason for decision in result.decisions] == ["fixture_state_lock_owned"]
    assert initialized == ["authorized apply"] and pipeline.calls == []
    assert not pipeline.state_dir.exists()


def test_missing_watch_extra_does_not_emit_false_service_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    pipeline = FixturePipeline(root, tmp_path / "state")
    readiness: list[str] = []
    original = builtins.__import__
    def unavailable(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("watchdog"):
            raise ImportError("fixture optional dependency absent")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    result = run_watch(pipeline, RunRequest("watch", once=True), on_ready=lambda: readiness.append("ready"))
    assert any(decision.status == "missing_dependency" and decision.reason == "watch_extra_required" for decision in result.decisions)
    assert readiness == [] and not pipeline.state_dir.exists()


def test_observers_still_close_when_durable_shutdown_checkpoint_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    archive(root / "Series v01.cbz")
    state = StateStore(tmp_path / "state")
    pipeline = FixturePipeline(root, state.state_dir)
    coordinator = WatchCoordinator(pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True)), state=state, journal_ready=lambda: ready(state))
    coordinator.start()
    observers = tuple(coordinator._observers.values())
    def failure(*args: Any, **kwargs: Any) -> None:
        raise StateStoreError("fixture checkpoint failure")
    monkeypatch.setattr(state, "put_job", failure)
    with pytest.raises(StateStoreError):
        coordinator.close()
    assert coordinator.pending_count == 1
    assert all(not observer.is_alive() and all(not emitter.is_alive() for emitter in observer.emitters) for observer in observers)


def child_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("KCE_") and key != "PYTHONPATH"}


def read_line(process: subprocess.Popen[str], *, timeout: float = 10) -> dict[str, Any]:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), "owned watch subprocess did not produce its expected lifecycle record"
    line = process.stdout.readline()
    assert line, "owned watch subprocess exited before lifecycle record"
    return json.loads(line)


@pytest.mark.parametrize("signal_number", (signal.SIGINT, signal.SIGTERM))
def test_real_watch_subprocess_handles_signals_and_leaves_no_observer_threads(tmp_path: Path, signal_number: signal.Signals) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    source = textwrap.dedent(f"""
        import json
        import threading
        from pathlib import Path
        from komga_cover_extractor.bootstrap import open_pipeline
        from komga_cover_extractor.config import validate_config
        from komga_cover_extractor.application.pipeline import RunRequest
        from komga_cover_extractor.application.watch import run_watch
        config = validate_config({{'paths': [], 'download_folders': [{str(root)!r}], 'state_dir': {str(tmp_path / 'state')!r}, 'match_through_image_similarity': False, 'compare_detected_cover_to_blank_images': False}})
        with open_pipeline(config) as pipeline:
            result = run_watch(pipeline, RunRequest('watch'), options={{'watchdog_file_transferred_check_interval': .01, 'watchdog_discover_new_files_check_interval': .1, 'watch_shutdown_timeout_seconds': 2}}, on_ready=lambda: print(json.dumps({{'ready': True}}), flush=True))
            print(json.dumps({{'result': result.as_dict(), 'threads': [thread.name for thread in threading.enumerate()]}}), flush=True)
    """)
    process = subprocess.Popen([sys.executable, "-I", "-u", "-c", source], cwd=tmp_path, env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert read_line(process) == {"ready": True}
        process.send_signal(signal_number)
        payload = read_line(process, timeout=5)
        process.wait(timeout=5)
        assert process.returncode == 0
        assert payload["result"]["exit_code"] == 3
        assert payload["result"]["counts"] == {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
        assert payload["threads"] == ["MainThread"]
        assert not (tmp_path / "state").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_external_stop_cancels_an_active_owned_worker_and_closes_the_forwarder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from komga_cover_extractor.infrastructure import workers
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    archive(book)
    stop = Event()
    cancel = Event()
    original = subprocess.Popen
    child_pids: list[int] = []
    worker_code = "import sys,time;sys.stdin.buffer.read();time.sleep(60)"
    def owned_worker(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original([sys.executable, "-I", "-c", worker_code], **kwargs)
        child_pids.append(process.pid)
        return process
    monkeypatch.setattr(workers.subprocess, "Popen", owned_worker)
    class ActivePipeline(FixturePipeline):
        def run(self, request: RunRequest) -> RunResult:
            timer = threading.Timer(0.2, stop.set)
            timer.start()
            try:
                response = check_archive_readable(fingerprint(book, root), policy=self.archive, cancel=self.cancel)
                return RunResult((response,))
            finally:
                timer.join(timeout=2)
    pipeline = ActivePipeline(root, tmp_path / "state")
    pipeline.cancel = cancel
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch"), stop=stop,
        options={"watchdog_file_transferred_check_interval": .01, "watchdog_discover_new_files_check_interval": .02},
        readable=lambda observed: Decision("observed", "fixture_ready", observed.path),
    )
    with forward_cancellation(stop, cancel):
        try:
            coordinator.run()
        finally:
            coordinator.close()
    result = coordinator.snapshot()
    assert result.exit_code == 3
    assert cancel.is_set() and stop.is_set()
    assert child_pids
    for pid in child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert all(thread.name != "kce-watch-cancellation" for thread in threading.enumerate())
    assert book.read_bytes().startswith(b"PK") and not pipeline.state_dir.exists()


def test_apply_saturation_persists_root_reconciliation_and_eventually_commits_each_source_once(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    for number in range(5):
        archive(root / f"Series v{number:02}.cbz", str(number).encode("ascii"))
    originals = {path: path.read_bytes() for path in root.iterdir()}
    state = StateStore(tmp_path / "state")
    clock = Clock()
    pipeline = FixturePipeline(root, state.state_dir, publish=True)
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True)),
        options={"watch_queue_capacity": 2, "watch_max_pending_jobs": 2},
        state=state, journal_ready=lambda: ready(state), clock=clock,
    )
    try:
        coordinator.start()
        markers = state.list_jobs(("pending",), kind="watch-reconcile", limit=10)
        assert len(markers) == 1 and markers[0].payload["phase"] == "reconciliation_required"
        for _ in range(40):
            clock.now += 1
            coordinator.tick()
            assert coordinator.pending_count <= 2
            for event in state.list_outbox(states=("queued", "pending"), limit=10):
                state.update_outbox(event.event_id, "complete")
        assert pipeline.calls == [("run", (root / f"Series v{number:02}.cbz",)) for number in range(5)]
        assert coordinator.snapshot().committed == 5
        assert len(tuple(root.glob("*.cover.bin"))) == 5
        assert all(path.read_bytes() == data for path, data in originals.items())
        markers = state.list_jobs(kind="watch-reconcile", limit=10)
        assert len(markers) == 1 and markers[0].state == "complete"
    finally:
        coordinator.close()


def test_actual_polling_observer_is_rebuilt_when_the_watched_root_is_replaced(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    archive(root / "Series v01.cbz")
    clock = Clock()
    pipeline = FixturePipeline(root, tmp_path / "state")
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch"), polling=True, clock=clock,
        options={"watchdog_file_transferred_check_interval": .02},
    )
    try:
        coordinator.start()
        initial = tuple(coordinator._observers.values())
        root.rename(tmp_path / "disconnected")
        pump(coordinator, clock, 1)
        assert all(not observer.is_alive() for observer in initial)
        assert any(decision.reason == "watch_root_unavailable" for decision in coordinator.snapshot().decisions)
        root.mkdir()
        book = root / "Series v02.cbz"
        archive(book)
        pump(coordinator, clock, 6)
        assert pipeline.calls == [("run", (book,))]
        replacement = tuple(coordinator._observers.values())
        assert len(replacement) == 1 and replacement[0] is not initial[0]
        assert replacement[0].is_alive()
    finally:
        coordinator.close()
    assert all(not observer.is_alive() for observer in replacement)

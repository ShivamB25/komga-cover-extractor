from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event

import pytest

from komga_cover_extractor.application.pipeline import Pipeline, RunRequest
from komga_cover_extractor.application.watch import WatchCoordinator
from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot
from komga_cover_extractor.domain.operations import FileFingerprint
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingPipeline(Pipeline):
    def __init__(self, root: Path, state: Path, result: Callable[[RunRequest], RunResult] | None = None) -> None:
        self.roots = (LibraryRoot(root, roles=("download",)),)
        self.state_dir = state
        self.archive = ArchivePolicy()
        self.policy_version = "watch-unit-v1"
        self.cancel = Event()
        self.received: list[tuple[str, tuple[Path, ...], tuple[bytes, ...], RunAuthorization]] = []
        self.result = result

    def run(self, request: RunRequest) -> RunResult:
        self.received.append((request.command, request.books, tuple(path.read_bytes() for path in request.books), request.authorization))
        if self.result is not None:
            return self.result(request)
        return RunResult(tuple(Decision("planned", "fixture_preview", path) for path in request.books), planned_count=1)


def readable(observed: FileFingerprint) -> Decision:
    return Decision("observed", "injected_readability", observed.path)


def make_coordinator(
    tmp_path: Path, clock: Clock, *, options: Mapping[str, int | float] | None = None,
    invalidate: Callable[[Path], None] | None = None,
    downloader_complete: Callable[[Path], bool | None] | None = None,
    state: StateStore | None = None, journal_ready: Callable[[], Decision] | None = None,
) -> tuple[WatchCoordinator, RecordingPipeline, Path]:
    root = tmp_path / "downloads"
    root.mkdir(exist_ok=True)
    pipeline = RecordingPipeline(root, tmp_path / "state")
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch"), clock=clock, readable=readable, options=options,
        invalidate=invalidate, downloader_complete=downloader_complete,
        state=state, journal_ready=journal_ready,
    )
    return coordinator, pipeline, root


def advance(coordinator: WatchCoordinator, clock: Clock, seconds: int) -> None:
    for _ in range(seconds):
        clock.now += 1
        coordinator.tick()


def test_growth_resets_quiet_window_and_processes_the_observed_generation(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    book = root / "Series v01.cbz"
    book.write_bytes(b"first")
    coordinator.reconcile()
    advance(coordinator, clock, 3)
    book.write_bytes(b"first-complete")
    coordinator.enqueue(("modified", book, None, False))
    advance(coordinator, clock, 4)
    assert pipeline.received == []
    advance(coordinator, clock, 2)
    assert pipeline.received == [("run", (book,), (b"first-complete",), RunAuthorization())]
    assert coordinator.snapshot().planned == 1
    assert not pipeline.state_dir.exists()
    coordinator.close()


def test_same_size_edit_with_restored_file_and_unchanged_directory_mtime_is_reconciled(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    book = root / "Series v01.cbz"
    book.write_bytes(b"AAAA")
    coordinator.reconcile()
    advance(coordinator, clock, 5)
    directory_time = root.stat().st_mtime_ns
    file_time = book.stat().st_mtime_ns
    book.write_bytes(b"BBBB")
    os.utime(book, ns=(book.stat().st_atime_ns, file_time))
    assert root.stat().st_mtime_ns == directory_time
    advance(coordinator, clock, 10)
    assert [item[2] for item in pipeline.received] == [(b"AAAA",), (b"BBBB",)]
    assert coordinator.snapshot().planned == 2
    coordinator.close()


def test_move_in_from_temporary_name_and_repeated_events_do_not_duplicate_work(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    temporary = root / "Series v01.cbz.!qB"
    temporary.write_bytes(b"finished")
    coordinator.enqueue(("created", temporary, None, False))
    coordinator.tick()
    assert coordinator.pending_count == 0
    book = root / "Series v01.cbz"
    temporary.rename(book)
    coordinator.enqueue(("moved", temporary, book, False))
    for _ in range(25):
        coordinator.enqueue(("modified", book, None, False))
    advance(coordinator, clock, 6)
    for _ in range(25):
        coordinator.enqueue(("created", book, None, False))
    advance(coordinator, clock, 10)
    assert pipeline.received == [("run", (book,), (b"finished",), RunAuthorization())]
    assert coordinator.snapshot().planned == 1
    coordinator.close()


def test_intake_and_pending_saturation_reconcile_all_inputs_without_retaining_unbounded_history(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock, options={"watch_queue_capacity": 2, "watch_max_pending_jobs": 3})
    for number in range(10):
        book = root / f"Series v{number:02}.cbz"
        book.write_bytes(str(number).encode("ascii"))
        coordinator.enqueue(("created", book, None, False))
    assert coordinator.queued_count == 2
    for _ in range(100):
        clock.now += 1
        coordinator.tick()
        assert coordinator.pending_count <= 3
    assert [item[2] for item in pipeline.received] == [(str(number).encode("ascii"),) for number in range(10)]
    assert coordinator.snapshot().planned == 10
    assert len(coordinator.snapshot().decisions) <= 5
    assert not pipeline.state_dir.exists()
    coordinator.close()


def test_delete_cancels_only_that_observation_and_invalidation_receives_real_paths(tmp_path: Path) -> None:
    clock = Clock()
    invalidated: list[Path] = []
    coordinator, pipeline, root = make_coordinator(tmp_path, clock, invalidate=invalidated.append)
    deleted, retained = root / "Series v01.cbz", root / "Other v01.cbz"
    deleted.write_bytes(b"gone")
    retained.write_bytes(b"kept")
    coordinator.reconcile()
    deleted.unlink()
    coordinator.enqueue(("deleted", deleted, None, False))
    advance(coordinator, clock, 5)
    assert [item[1:] for item in pipeline.received] == [((retained,), (b"kept",), RunAuthorization())]
    assert deleted in invalidated and retained in invalidated
    assert coordinator.pending_count == 0
    coordinator.close()


def test_owned_artifacts_hidden_names_and_state_are_not_pipeline_inputs(tmp_path: Path) -> None:
    clock = Clock()
    root = tmp_path / "downloads"
    root.mkdir()
    state = root / "state"
    state.mkdir()
    (state / "untrusted.cbz").write_bytes(b"state")
    (root / ".kce-quarantine").mkdir()
    (root / ".kce-quarantine" / "retained.cbz").write_bytes(b"backup")
    for name in (".hidden.cbz", ".kce-stage.cbz", "Series v01.cbz.part", "Series v01.cbz.tmp", "Series v01.jpg"):
        (root / name).write_bytes(b"ignored")
    book = root / "Series v01.cbz"
    book.write_bytes(b"book")
    pipeline = RecordingPipeline(root, state)
    coordinator = WatchCoordinator(pipeline, RunRequest("watch"), clock=clock, readable=readable)
    coordinator.reconcile()
    advance(coordinator, clock, 5)
    assert pipeline.received == [("run", (book,), (b"book",), RunAuthorization())]
    assert not (state / "state.sqlite3").exists()
    coordinator.close()


def test_downloader_completion_is_a_readiness_gate_not_a_retryable_pipeline_failure(tmp_path: Path) -> None:
    clock = Clock()
    completion = {"done": False}
    coordinator, pipeline, root = make_coordinator(tmp_path, clock, downloader_complete=lambda path: completion["done"])
    book = root / "Series v01.cbz"
    book.write_bytes(b"unchanged")
    coordinator.reconcile()
    advance(coordinator, clock, 25)
    assert pipeline.received == []
    assert coordinator.phases == {book: "waiting_ready"}
    completion["done"] = True
    advance(coordinator, clock, 1)
    assert pipeline.received == [("run", (book,), (b"unchanged",), RunAuthorization())]
    assert coordinator.snapshot().failed == 0
    coordinator.close()


def test_readability_retries_are_bounded_and_source_change_resets_attempts(tmp_path: Path) -> None:
    clock = Clock()
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    book.write_bytes(b"incomplete")
    probes: list[bytes] = []
    def probe(observed: FileFingerprint) -> Decision:
        data = observed.path.read_bytes()
        probes.append(data)
        return Decision("observed", "finished", observed.path) if data == b"complete" else Decision("invalid_input", "invalid_zip_archive", observed.path)
    pipeline = RecordingPipeline(root, tmp_path / "state")
    coordinator = WatchCoordinator(pipeline, RunRequest("watch"), clock=clock, readable=probe)
    coordinator.reconcile()
    advance(coordinator, clock, 25)
    assert probes == [b"incomplete", b"incomplete", b"incomplete"]
    assert pipeline.received == []
    assert coordinator.snapshot().failed == 1
    book.write_bytes(b"complete")
    coordinator.enqueue(("modified", book, None, False))
    advance(coordinator, clock, 6)
    assert probes[-1] == b"complete"
    assert pipeline.received == [("run", (book,), (b"complete",), RunAuthorization())]
    coordinator.close()


def test_unsafe_archive_failure_is_permanent_for_the_same_generation(tmp_path: Path) -> None:
    clock = Clock()
    root = tmp_path / "downloads"
    root.mkdir()
    book = root / "Series v01.cbz"
    book.write_bytes(b"unsafe")
    probes: list[Path] = []
    def probe(observed: FileFingerprint) -> Decision:
        probes.append(observed.path)
        return Decision("invalid_input", "unsafe_member_name", observed.path)
    pipeline = RecordingPipeline(root, tmp_path / "state")
    coordinator = WatchCoordinator(pipeline, RunRequest("watch"), clock=clock, readable=probe)
    coordinator.reconcile()
    advance(coordinator, clock, 30)
    assert probes == [book]
    assert coordinator.snapshot().failed == 1
    assert any(decision.reason == "unsafe_member_name" for decision in coordinator.snapshot().decisions)
    coordinator.close()


def test_missing_root_is_deferred_then_reconciled_after_recreation(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    book = root / "Series v01.cbz"
    book.write_bytes(b"old-mount")
    coordinator.reconcile()
    disconnected = tmp_path / "disconnected"
    root.rename(disconnected)
    advance(coordinator, clock, 1)
    assert coordinator.pending_count == 1
    assert any(decision.reason == "watch_root_unavailable" for decision in coordinator.snapshot().decisions)
    root.mkdir()
    book.write_bytes(b"new-mount")
    advance(coordinator, clock, 6)
    assert pipeline.received == [("run", (book,), (b"new-mount",), RunAuthorization())]
    assert all(decision.reason != "watch_root_unavailable" for decision in coordinator.snapshot().decisions)
    coordinator.close()


def test_one_active_pipeline_preserves_all_selected_roots_and_serial_order(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    library = tmp_path / "library"
    library.mkdir()
    pipeline.roots = (*pipeline.roots, LibraryRoot(library))
    coordinator = WatchCoordinator(pipeline, RunRequest("watch"), clock=clock, readable=readable)
    for name, data in (("A v01.cbz", b"a1"), ("A v02.cbz", b"a2"), ("B v01.cbz", b"b1")):
        (root / name).write_bytes(data)
    (library / "Existing v01.cbz").write_bytes(b"existing")
    coordinator.reconcile()
    advance(coordinator, clock, 7)
    assert [item[2] for item in pipeline.received] == [(b"a1",), (b"a2",), (b"b1",)]
    assert coordinator.roots == pipeline.roots
    assert coordinator.watch_roots == (pipeline.roots[0],)
    assert coordinator.snapshot().planned == 3
    coordinator.close()


def test_changed_policy_reassesses_unchanged_input_instead_of_reusing_previous_decision(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    book = root / "Series v01.cbz"
    book.write_bytes(b"book")
    coordinator.reconcile()
    advance(coordinator, clock, 5)
    pipeline.policy_version = "watch-unit-v2"
    advance(coordinator, clock, 10)
    assert [item[2] for item in pipeline.received] == [(b"book",), (b"book",)]
    assert coordinator.snapshot().planned == 2
    coordinator.close()


def test_preview_never_invokes_journal_initialization_even_with_apply_state_available(tmp_path: Path) -> None:
    clock = Clock()
    initialized: list[str] = []
    coordinator, pipeline, root = make_coordinator(tmp_path, clock, state=StateStore(tmp_path / "state"), journal_ready=lambda: initialized.append("called") or Decision("observed", "state_ready"))
    book = root / "Series v01.cbz"
    book.write_bytes(b"book")
    coordinator.reconcile()
    advance(coordinator, clock, 5)
    assert pipeline.received == [("run", (book,), (b"book",), RunAuthorization())]
    assert initialized == []
    assert not pipeline.state_dir.exists()
    coordinator.close()


@pytest.mark.parametrize("options", ({"watch_queue_capacity": 0}, {"watch_retry_attempts": 1.5}, {"watch_reconcile_interval_seconds": float("nan")}, {"unknown": 1}, {"watch_max_pending_jobs": True}))
def test_invalid_limits_fail_at_the_watch_boundary(tmp_path: Path, options: dict[str, int | float]) -> None:
    pipeline = RecordingPipeline(tmp_path, tmp_path / "state")
    with pytest.raises(ValueError):
        WatchCoordinator(pipeline, RunRequest("watch"), options=options)
    assert not pipeline.state_dir.exists()


def test_wall_clock_jump_does_not_shorten_the_monotonic_quiet_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    book = root / "Series v01.cbz"
    book.write_bytes(b"stable")
    coordinator.reconcile()
    advance(coordinator, clock, 2)
    monkeypatch.setattr(time, "time", lambda: 10_000_000_000.0)
    advance(coordinator, clock, 2)
    assert pipeline.received == []
    advance(coordinator, clock, 1)
    assert pipeline.received == [("run", (book,), (b"stable",), RunAuthorization())]
    assert coordinator.snapshot().planned == 1
    coordinator.close()


def test_component_containment_ignores_an_event_from_a_similarly_named_root(tmp_path: Path) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    neighbor = tmp_path / "downloads-old"
    neighbor.mkdir()
    outside = neighbor / "Series v01.cbz"
    outside.write_bytes(b"outside")
    selected = root / "Series v01.cbz"
    selected.write_bytes(b"inside")
    coordinator.enqueue(("created", outside, None, False))
    coordinator.enqueue(("created", selected, None, False))
    advance(coordinator, clock, 6)
    assert pipeline.received == [("run", (selected,), (b"inside",), RunAuthorization())]
    assert outside.read_bytes() == b"outside"
    coordinator.close()


def test_custom_candidate_filter_cannot_admit_sidecar_images_or_owned_temporary_files(tmp_path: Path) -> None:
    clock = Clock()
    root = tmp_path / "downloads"
    root.mkdir()
    pipeline = RecordingPipeline(root, tmp_path / "state")
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch"), clock=clock, readable=readable,
        candidate_filter=lambda path: True,
    )
    for name in ("Series v01.jpg", "Series v01.jpeg", "poster.png", "cover.webp", "cover.tbn", ".kce-owned.cbz", "Series v02.cbz.tmp"):
        (root / name).write_bytes(b"ignored")
    book = root / "Series v01.cbz"
    book.write_bytes(b"book")
    rejected = root / "unacceptable.txt"
    rejected.write_bytes(b"regular input for the canonical filtering stage")
    coordinator.reconcile()
    advance(coordinator, clock, 6)
    assert [item[1] for item in pipeline.received] == [(book,), (rejected,)]
    assert coordinator.snapshot().planned == 2
    assert (root / "poster.png").read_bytes() == b"ignored"
    coordinator.close()


def test_apply_reconciliation_visits_every_no_match_input_past_the_settled_cache(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    originals = {root / f"Unmatched v{number:02}.cbz": str(number).encode() for number in range(8)}
    for path, content in originals.items():
        path.write_bytes(content)
    clock = Clock()
    pipeline = RecordingPipeline(root, tmp_path / "state", result=lambda request: RunResult(
        tuple(Decision("no_match", "no_matching_evidence", path) for path in request.books), planned_count=0,
    ))
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True)),
        clock=clock, readable=readable,
        options={"watch_queue_capacity": 2, "watch_max_pending_jobs": 2},
    )
    try:
        for _ in range(50):
            clock.now += 1
            coordinator.tick()
            assert coordinator.pending_count <= 2
        assert {path for _, books, _, _ in pipeline.received for path in books} == set(originals)
        assert coordinator.snapshot().committed == 0
        assert {path: path.read_bytes() for path in originals} == originals
    finally:
        coordinator.close()


@pytest.mark.parametrize("remaining_parts", (0, 1), ids=("single-file", "shorter-multipart"))
def test_refreshed_manifest_releases_obsolete_parts_without_stalling_dispatch(
    tmp_path: Path, remaining_parts: int,
) -> None:
    clock = Clock()
    coordinator, pipeline, root = make_coordinator(tmp_path, clock)
    primary = root / "Series v01.cbz"
    parts = (root / "secondary.bin", root / "obsolete.bin")
    primary.write_bytes(b"unchanged primary")
    for path in parts:
        path.write_bytes(path.name.encode())
    sources = tuple(fingerprint(path, root) for path in (primary, *parts))
    current = list(sources)
    coordinator.downloader_complete = lambda path: path != parts[-1]

    def observe_manifest(observed: FileFingerprint) -> Decision:
        coordinator._remember_parts(tuple(current))
        return Decision("observed", "injected_manifest", observed.path)

    coordinator._readable = observe_manifest
    try:
        coordinator.reconcile()
        advance(coordinator, clock, 10)
        assert pipeline.received == []
        current[:] = sources[:1 + remaining_parts]
        for path in parts[remaining_parts:]:
            path.unlink()
        advance(coordinator, clock, 20)
        assert pipeline.received == [("run", (primary,), (b"unchanged primary",), RunAuthorization())]
        assert coordinator.snapshot().planned == 1
        assert fingerprint(primary, root) == sources[0]
        advance(coordinator, clock, 10)
        assert pipeline.received == [("run", (primary,), (b"unchanged primary",), RunAuthorization())]
    finally:
        coordinator.close()

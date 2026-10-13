from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from komga_cover_extractor.application.pipeline import Pipeline, RunRequest
from komga_cover_extractor.application.watch import WatchCoordinator, run_watch
from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy


class NeverReplayPipeline(Pipeline):
    def __init__(self, root: Path, state: Path) -> None:
        self.roots = (LibraryRoot(root, roles=("download",)),)
        self.state_dir = state
        self.archive = ArchivePolicy()
        self.policy_version = "watch-crash-v1"
        self.cancel = Event()
        self.calls: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        self.calls.append(request)
        return RunResult((Decision("failed", "unexpected_replay"),), failed=1)


def environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("KCE_") and key != "PYTHONPATH"}


def create_book(root: Path) -> Path:
    root.mkdir()
    book = root / "Series v01.cbz"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("asset.txt", b"retained source asset")
    return book


def child_source(root: Path, state: Path, *, after_commit: bool, graceful: bool = False) -> str:
    return textwrap.dedent(f"""
        import json
        import os
        import threading
        from pathlib import Path
        from threading import Event
        from komga_cover_extractor.application.pipeline import Pipeline, RunRequest
        from komga_cover_extractor.application.watch import run_watch
        from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
        from komga_cover_extractor.domain.libraries import LibraryRoot
        from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
        from komga_cover_extractor.infrastructure.state import StateStore
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
        from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
        root = Path({str(root)!r})
        state = StateStore(Path({str(state)!r}))
        class CrashPipeline(Pipeline):
            def __init__(self):
                self.roots = (LibraryRoot(root, roles=('download',)),)
                self.state_dir = state.state_dir
                self.archive = ArchivePolicy()
                self.policy_version = 'watch-crash-v1'
                self.cancel = Event()
            def run(self, request):
                if {after_commit!r}:
                    plan = OperationPlan(self.policy_version, 'watch-crash-publication', (Operation('cover', 'publish', root / 'cover.bin', artifact=ArtifactSpec(content=b'retained verified publication')),), (RootPermission(root),))
                    result = TransactionExecutor(state.state_dir, (root,)).apply(plan, authorization=request.authorization)
                    if result.committed != 1:
                        print(json.dumps(result.as_dict()), flush=True)
                        os._exit(84)
                os._exit(83)
        pipeline = CrashPipeline()
        def journal_ready():
            state.initialize()
            return Decision('observed', 'fixture_journal_ready')
        result = run_watch(pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)), state=state, journal_ready=journal_ready, options={{'watchdog_file_transferred_check_interval': .01, 'watchdog_discover_new_files_check_interval': {5.0 if graceful else .03!r}, 'watch_shutdown_timeout_seconds': 2}}, on_ready=lambda: print(json.dumps({{'ready': True}}), flush=True))
        print(json.dumps({{'result': result.as_dict(), 'threads': [thread.name for thread in threading.enumerate()]}}), flush=True)
    """)


@pytest.mark.parametrize("after_commit", (False, True))
def test_fresh_process_never_replays_interrupted_watch_pipeline_before_or_after_verified_commit(tmp_path: Path, after_commit: bool) -> None:
    root = tmp_path / "downloads"
    book = create_book(root)
    before = book.read_bytes()
    state = StateStore(tmp_path / "state")
    crashed = subprocess.run([sys.executable, "-I", "-u", "-c", child_source(root, state.state_dir, after_commit=after_commit)], cwd=tmp_path, env=environment(), capture_output=True, text=True, timeout=15)
    assert crashed.returncode == 83, crashed.stderr + crashed.stdout
    rows = state.list_jobs(kind="watch", limit=10)
    assert len(rows) == 1 and rows[0].state == "running" and rows[0].payload["phase"] == "running"
    pipeline = NeverReplayPipeline(root, state.state_dir)
    def initialized() -> Decision:
        state.initialize()
        return Decision("observed", "fixture_journal_ready")
    result = run_watch(pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True), once=True), state=state, journal_ready=initialized)
    assert result.exit_code == 3 and pipeline.calls == []
    assert any(decision.reason == "watch_interrupted_job" and decision.status == "review_required" for decision in result.decisions)
    rows = state.list_jobs(kind="watch", limit=10)
    assert rows[0].state == "needs_review" and rows[0].payload["interrupted"] is True
    assert book.read_bytes() == before
    if after_commit:
        assert (root / "cover.bin").read_bytes() == b"retained verified publication"
        events = state.list_outbox(operation_id="watch-crash-publication", limit=10)
        assert len(events) == 1
    else:
        assert not (root / "cover.bin").exists()
        assert state.list_outbox(operation_id="watch-crash-publication", limit=10) == ()


@pytest.mark.parametrize("after_commit", [False, True])
def test_changed_source_after_interruption_retains_old_review_and_observes_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool,
) -> None:
    root = tmp_path / "downloads"
    book = create_book(root)
    state = StateStore(tmp_path / "state")
    crashed = subprocess.run(
        [sys.executable, "-I", "-u", "-c", child_source(root, state.state_dir, after_commit=after_commit)],
        cwd=tmp_path, env=environment(), capture_output=True, text=True, timeout=15,
    )
    assert crashed.returncode == 83, crashed.stderr + crashed.stdout
    old = state.list_jobs(kind="watch", limit=10)[0]
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("asset.txt", b"new independently observed generation")
    changed_bytes = book.read_bytes()
    current = fingerprint(book, root)
    assert current.as_dict() != old.payload["fingerprint"]
    pipeline = NeverReplayPipeline(root, state.state_dir)
    def observe_new(request: RunRequest) -> RunResult:
        pipeline.calls.append(request)
        assert fingerprint(book, root) == current
        return RunResult((Decision("no_match", "fixture_no_matching_series", book),), skipped=1)
    monkeypatch.setattr(pipeline, "run", observe_new)
    clock = [0.0]
    def initialized() -> Decision:
        state.initialize()
        return Decision("observed", "fixture_journal_ready")
    coordinator = WatchCoordinator(
        pipeline, RunRequest("watch", authorization=RunAuthorization(apply=True)),
        state=state, journal_ready=initialized, clock=lambda: clock[0],
        options={"watchdog_file_transferred_check_interval": .01, "watchdog_discover_new_files_check_interval": .01},
    )
    try:
        coordinator.start()
        for _ in range(6):
            clock[0] += 1
            coordinator.tick()
        assert len(pipeline.calls) == 1 and pipeline.calls[0].books == (book,)
        assert coordinator.snapshot().committed == 0
        fresh = state.list_jobs(kind="watch", limit=10)[0]
        assert fresh.payload["fingerprint"] == current.as_dict() and fresh.state == "complete"
        retained = state.list_jobs(("needs_review",), kind="watch-delivery", limit=10)
        assert len(retained) == 1 and retained[0].payload["fingerprint"] == old.payload["fingerprint"]
        assert retained[0].payload["interrupted"] is True
        assert book.read_bytes() == changed_bytes
        if after_commit:
            assert (root / "cover.bin").read_bytes() == b"retained verified publication"
    finally:
        coordinator.close()


def next_line(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(5), "owned watch subprocess did not reach the expected lifecycle boundary"
    return json.loads(process.stdout.readline())


def test_sigterm_during_apply_readiness_checkpoints_pending_job_and_reaps_observers(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    book = create_book(root)
    before = book.read_bytes()
    state = StateStore(tmp_path / "state")
    process = subprocess.Popen([sys.executable, "-I", "-u", "-c", child_source(root, state.state_dir, after_commit=False, graceful=True)], cwd=tmp_path, env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert next_line(process) == {"ready": True}
        process.send_signal(signal.SIGTERM)
        payload = next_line(process)
        process.wait(timeout=5)
        assert process.returncode == 0
        assert payload["result"]["exit_code"] == 3
        assert payload["result"]["counts"]["committed"] == 0
        assert payload["threads"] == ["MainThread"]
        rows = state.list_jobs(("pending",), kind="watch", limit=10)
        assert len(rows) == 1 and rows[0].payload["phase"] == "waiting_ready"
        assert rows[0].payload["attempts"] == 0
        assert book.read_bytes() == before and not (root / "cover.bin").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

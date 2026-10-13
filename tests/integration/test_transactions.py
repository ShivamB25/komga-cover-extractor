from __future__ import annotations

import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from komga_cover_extractor.application.recovery import recover_operation
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure import transactions
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor


def _plan(
    operation_id: str,
    operations: tuple[Operation, ...],
    *roots: Path,
) -> OperationPlan:
    return OperationPlan(
        policy_version="transaction-tests-v1",
        operation_id=operation_id,
        operations=operations,
        roots=tuple(RootPermission(root) for root in roots),
    )


def _snapshot(root: Path) -> dict[str, bytes | tuple[str, str]]:
    snapshot: dict[str, bytes | tuple[str, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", str(path.readlink()))
        elif path.is_file():
            snapshot[relative] = path.read_bytes()
        elif path.is_dir():
            snapshot[relative] = ("directory", "")
    return snapshot


def _retained_files(root: Path, excluded: set[Path]) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path not in excluded
    ]


def test_preview_does_not_create_state_or_change_library(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    source = library / "book.cbz"
    source.write_bytes(b"original archive")
    target = library / "cover.webp"
    state = tmp_path / "state"
    before = _snapshot(library)
    operation = Operation(
        "cover",
        "publish",
        target,
        artifact=ArtifactSpec(content=b"encoded cover"),
    )

    result = TransactionExecutor(state, (library,)).apply(
        _plan("preview-only", (operation,), library),
        authorization=RunAuthorization(),
    )

    assert result.exit_code == 0
    assert result.plans == (_plan("preview-only", (operation,), library),)
    assert result.committed == 0
    assert _snapshot(library) == before
    assert not state.exists()


def test_plan_cannot_authorize_a_root_not_configured_on_executor(tmp_path: Path) -> None:
    library = tmp_path / "library"
    outside = tmp_path / "outside"
    library.mkdir()
    outside.mkdir()
    target = outside / "must-not-appear.bin"
    state = tmp_path / "state"
    before_library = _snapshot(library)
    before_outside = _snapshot(outside)
    operation = Operation(
        "outside-write",
        "publish",
        target,
        artifact=ArtifactSpec(content=b"unauthorized"),
    )

    result = TransactionExecutor(state, (library,)).apply(
        _plan("outside-root", (operation,), library, outside),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 0
    assert any(
        decision.status == "permission_denied" for decision in result.decisions
    )
    assert _snapshot(library) == before_library
    assert _snapshot(outside) == before_outside
    assert not target.exists()
    assert not state.exists()


def test_symlinked_parent_is_rejected_before_publication(tmp_path: Path) -> None:
    library = tmp_path / "library"
    outside = tmp_path / "outside"
    library.mkdir()
    outside.mkdir()
    linked = library / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    target = linked / "cover.jpg"
    state = tmp_path / "state"
    before_library = _snapshot(library)
    before_outside = _snapshot(outside)
    operation = Operation(
        "through-link",
        "publish",
        target,
        artifact=ArtifactSpec(content=b"must not escape"),
    )

    result = TransactionExecutor(state, (library,)).apply(
        _plan("symlink-parent", (operation,), library),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 0
    assert any(
        decision.status == "permission_denied" for decision in result.decisions
    )
    assert _snapshot(library) == before_library
    assert _snapshot(outside) == before_outside
    assert not target.exists()
    assert not state.exists()


def test_stale_source_fingerprint_preserves_current_source_and_absent_target(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    series = library / "Series"
    series.mkdir()
    source = library / "incoming.cbz"
    target = series / "incoming.cbz"
    source.write_bytes(b"old")
    observed = fingerprint(source, library)
    source.write_bytes(b"new")
    state = tmp_path / "state"
    operation = Operation("move-incoming", "move", target, source=observed)

    result = TransactionExecutor(state, (library,)).apply(
        _plan("stale-source", (operation,), library),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 0
    assert any(
        decision.status in {"stale_plan", "conflict"}
        for decision in result.decisions
    )
    assert source.read_bytes() == b"new"
    assert not target.exists()



def test_stale_replacement_target_is_not_overwritten_or_quarantined(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    target = library / "cover.jpg"
    target.write_bytes(b"observed-target")
    observed = fingerprint(target, library)
    operation = Operation(
        "replace-cover",
        "publish",
        target,
        destination_before=observed,
        artifact=ArtifactSpec(content=b"planned replacement"),
        collision="replace",
    )
    target.write_bytes(b"external target update")

    result = TransactionExecutor(tmp_path / "state", (library,)).apply(
        _plan("stale-target", (operation,), library),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 0
    assert any(
        decision.status in {"stale_plan", "conflict"}
        for decision in result.decisions
    )
    assert target.read_bytes() == b"external target update"
    assert all(
        path.read_bytes() != b"planned replacement"
        for path in _retained_files(library, {target})
    )

def test_absent_collision_preserves_existing_target_and_new_artifact(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    target = library / "cover.jpg"
    target.write_bytes(b"existing bytes")
    state = tmp_path / "state"
    operation = Operation(
        "colliding-cover",
        "publish",
        target,
        artifact=ArtifactSpec(content=b"different bytes"),
    )

    result = TransactionExecutor(state, (library,)).apply(
        _plan("absent-collision", (operation,), library),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 0
    assert any(decision.status == "conflict" for decision in result.decisions)
    assert target.read_bytes() == b"existing bytes"
    assert all(
        path.read_bytes() != b"different bytes"
        for path in _retained_files(library, {target})
    )


def test_authorized_replacement_publishes_verified_bytes_and_retains_backup(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    target = library / "cover.jpg"
    target.write_bytes(b"prior artwork")
    observed = fingerprint(target, library)
    replacement = b"verified replacement artwork"
    operation = Operation(
        "replace-cover",
        "publish",
        target,
        destination_before=observed,
        artifact=ArtifactSpec(content=replacement, media_type="image/jpeg"),
        collision="replace",
    )

    result = TransactionExecutor(tmp_path / "state", (library,)).apply(
        _plan("safe-replacement", (operation,), library),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 1
    assert result.failed == 0
    assert target.read_bytes() == replacement
    retained = _retained_files(library, {target})
    original_backups = [path for path in retained if path.read_bytes() == b"prior artwork"]
    assert len(original_backups) == 1
    assert all(path.is_relative_to(library) for path in original_backups)


def test_move_cleans_source_only_after_verified_publish_and_retains_original(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    series = library / "Series"
    series.mkdir()
    source = library / "incoming.cbz"
    target = series / "incoming.cbz"
    original = b"complete source archive bytes"
    source.write_bytes(original)
    observed = fingerprint(source, library)
    operation = Operation("move-book", "move", target, source=observed)

    result = TransactionExecutor(tmp_path / "state", (library,)).apply(
        _plan("move-with-retention", (operation,), library),
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 1
    assert target.read_bytes() == original
    assert not source.exists()
    retained = _retained_files(library, {target})
    original_backups = [path for path in retained if path.read_bytes() == original]
    assert len(original_backups) == 1
    assert all(path.is_relative_to(library) for path in original_backups)


def test_two_file_swap_stages_both_sources_before_replacing_either_target(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    first = library / "first.cbz"
    second = library / "second.cbz"
    first.write_bytes(b"first-original")
    second.write_bytes(b"second-original")
    first_observed = fingerprint(first, library)
    second_observed = fingerprint(second, library)
    plan = _plan(
        "two-file-swap",
        (
            Operation(
                "first-to-second",
                "move",
                second,
                source=first_observed,
                destination_before=second_observed,
                collision="replace",
            ),
            Operation(
                "second-to-first",
                "move",
                first,
                source=second_observed,
                destination_before=first_observed,
                collision="replace",
            ),
        ),
        library,
    )

    result = TransactionExecutor(tmp_path / "state", (library,)).apply(
        plan,
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 1
    assert first.read_bytes() == b"second-original"
    assert second.read_bytes() == b"first-original"
    retained = _retained_files(library, {first, second})
    assert sum(path.read_bytes() == b"first-original" for path in retained) == 1
    assert sum(path.read_bytes() == b"second-original" for path in retained) == 1


def test_cross_root_move_uses_the_exdev_safe_copy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = tmp_path / "downloads"
    library = tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    series = library / "Series"
    series.mkdir()
    source = downloads / "book.cbz"
    target = series / "book.cbz"
    original = b"archive copied between independently authorized roots"
    source.write_bytes(original)
    real_replace = os.replace
    direct_rename_attempts: list[tuple[Path, Path]] = []

    def exdev_for_direct_move(source_path, destination_path, *args, **kwargs):
        source_candidate = Path(source_path)
        destination_candidate = Path(destination_path)
        if source_candidate == source and destination_candidate == target:
            direct_rename_attempts.append((source_candidate, destination_candidate))
            raise OSError(errno.EXDEV, "simulated cross-device source move")
        return real_replace(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(transactions.os, "replace", exdev_for_direct_move)
    observed = fingerprint(source, downloads)
    operation = Operation("copy-between-roots", "move", target, source=observed)
    plan = _plan("cross-root-copy", (operation,), downloads, library)

    result = TransactionExecutor(
        tmp_path / "state", (downloads, library)
    ).apply(plan, authorization=RunAuthorization(apply=True))

    assert direct_rename_attempts == []
    assert result.committed == 1
    assert target.read_bytes() == original
    assert not source.exists()
    retained_originals = [path for path in _retained_files(downloads, set()) if path.read_bytes() == original]
    assert len(retained_originals) == 1


@pytest.mark.parametrize("change_at", ["before_apply", "before_publish", "member_committed"])
def test_publication_rejects_changed_source_book(tmp_path: Path, change_at: str) -> None:
    library = tmp_path / "library"
    library.mkdir()
    book = library / "book.cbz"
    book.write_bytes(b"observed book")
    observed = fingerprint(book, library)
    target = library / "book.webp"
    plan = _plan("changed-book", (
        Operation("art", "publish", target, source=observed,
                  artifact=ArtifactSpec(content=b"art from observed book")),
    ), library)

    def change_source(stage: str, member_id: str) -> None:
        if stage == change_at and member_id == "art":
            book.write_bytes(b"external replacement")

    if change_at == "before_apply":
        book.write_bytes(b"external replacement")
    result = TransactionExecutor(tmp_path / "state", (library,), checkpoint=change_source).apply(
        plan, authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 0
    assert any(decision.status in {"stale_plan", "review_required"} for decision in result.decisions)
    assert book.read_bytes() == b"external replacement"
    assert not target.exists()


@pytest.mark.parametrize("change_at", ["before_publish", "member_committed"])
@pytest.mark.parametrize("change_kind", ["unlink", "symlink"])
def test_missing_or_linked_book_cannot_commit_generated_art(
    tmp_path: Path, change_at: str, change_kind: str,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    book = library / "book.cbz"
    book.write_bytes(b"observed book")
    outside = tmp_path / "external.cbz"
    outside.write_bytes(b"external book")
    target = library / "book.webp"
    state = tmp_path / "state"
    plan = _plan("unavailable-book", (
        Operation("art", "publish", target, source=fingerprint(book, library),
                  artifact=ArtifactSpec(content=b"generated art")),
    ), library)

    def replace_source(stage: str, member_id: str) -> None:
        if stage == change_at and member_id == "art":
            book.unlink()
            if change_kind == "symlink":
                book.symlink_to(outside)

    executor = TransactionExecutor(state, (library,), checkpoint=replace_source)
    result = executor.apply(plan, authorization=RunAuthorization(apply=True))
    assert (result.committed, result.exit_code) == (0, 3)
    assert not target.exists()
    assert outside.read_bytes() == b"external book"
    record = executor.state_store.get_operation(plan.operation_id)
    assert record is not None and record.phase == "needs_review"
    if change_at == "member_committed":
        candidates = tuple(library.glob(".kce-quarantine-*/rollback-candidate"))
        assert len(candidates) == 1 and candidates[0].read_bytes() == b"generated art"


def test_uncommitted_generation_witness_preserves_external_replacement(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    book = library / "book.cbz"
    book.write_bytes(b"observed book")
    target = library / "book.webp"
    plan = _plan("external-generation", (
        Operation("art", "publish", target, source=fingerprint(book, library),
                  artifact=ArtifactSpec(content=b"generated art")),
    ), library)
    replacement_identity: tuple[int, int] | None = None

    def replace_generation(stage: str, member_id: str) -> None:
        nonlocal replacement_identity
        if stage == "member_committed" and member_id == "art":
            before = target.stat()
            target.unlink()
            target.write_bytes(b"generated art")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement_identity = (target.stat().st_dev, target.stat().st_ino)
            assert replacement_identity != (before.st_dev, before.st_ino)
            book.write_bytes(b"changed book")

    executor = TransactionExecutor(tmp_path / "state", (library,), checkpoint=replace_generation)
    result = executor.apply(plan, authorization=RunAuthorization(apply=True))
    assert (result.committed, result.exit_code) == (0, 3)
    assert target.read_bytes() == b"generated art"
    assert (target.stat().st_dev, target.stat().st_ino) == replacement_identity
    assert book.read_bytes() == b"changed book"


def test_remote_action_claim_has_one_owner_and_cannot_reclaim_completed_intent(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    state.initialize()
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = tuple(pool.map(
            lambda index: state.claim_job("same-remote-action", "torrent-priority", {"owner": index}),
            range(4),
        ))
    assert sorted(claims) == [False, False, False, True]
    owner = claims.index(True)
    job = state.list_jobs()[0]
    assert job.state == "running" and job.payload == {"owner": owner}
    state.put_job(job.job_id, job.kind, job.payload, state="complete")
    assert state.claim_job(job.job_id, job.kind, {"owner": "replacement"}) is False
    settled = state.list_jobs()[0]
    assert settled.state == "complete" and settled.payload == {"owner": owner}


def test_job_pagination_keeps_pending_watch_generations_separate_from_remote_and_completed_jobs(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    assert state.get_job("absent") is None
    assert state.list_jobs(kind="watch", limit=2) == ()
    assert not state.state_dir.exists()
    state.initialize()
    for job_id, kind, phase in (
        ("a", "watch", "pending"), ("b", "torrent", "pending"),
        ("c", "watch", "complete"), ("d", "watch", "pending"), ("e", "watch", "pending"),
    ):
        state.put_job(job_id, kind, {"path": job_id}, state=phase)
    first = state.list_jobs(("pending",), kind="watch", limit=2)
    assert [job.job_id for job in first] == ["a", "d"]
    second = state.list_jobs(("pending",), kind="watch", after_job_id=first[-1].job_id, limit=2)
    assert [job.job_id for job in second] == ["e"]
    completed = state.get_job("c")
    assert completed is not None and completed.state == "complete" and completed.payload == {"path": "c"}


@pytest.mark.parametrize("protected_phase", ["running", "complete", "failed", "needs_review"])
def test_known_rejection_retry_cannot_reclaim_other_action_states_or_changed_intent(tmp_path: Path, protected_phase: str) -> None:
    state = StateStore(tmp_path / "state")
    state.initialize()
    payload = {"channel": "discord/normal/0", "intent": "original"}
    assert state.claim_job("message", "delivery", payload)
    state.put_job("message", "delivery", payload, state="pending", error="http_retry_budget")
    assert not state.claim_job("message", "delivery", {"intent": "different"}, retry_pending=True)
    assert state.claim_job("message", "delivery", payload, retry_pending=True)
    state.put_job("message", "delivery", payload, state=protected_phase)
    assert not state.claim_job("message", "delivery", payload, retry_pending=True)
    assert state.get_job("message").state == protected_phase


def test_cancelled_recovery_can_resume_without_poisoning_the_operation(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    state = tmp_path / "state"
    target = library / "published.bin"
    plan = _plan(
        "cancel-recovery",
        (Operation("publish", "publish", target, artifact=ArtifactSpec(content=b"verified final bytes")),),
        library,
    )

    def stop_after_stage(event: str, member: str) -> None:
        if event == "staged":
            raise OSError("injected staging failure")

    interrupted = TransactionExecutor(state, (library,), checkpoint=stop_after_stage).apply(
        plan, authorization=RunAuthorization(apply=True),
    )
    assert interrupted.committed == 0
    assert not target.exists()
    cancel = Event()
    cancel.set()
    stopped = recover_operation(
        plan.operation_id, state_dir=state, roots=(library,),
        authorization=RunAuthorization(apply=True), cancel=cancel,
    )
    assert stopped.committed == 0
    assert any(decision.status == "cancelled" for decision in stopped.decisions)
    assert not target.exists()
    cancel.clear()
    recovered = recover_operation(
        plan.operation_id, state_dir=state, roots=(library,),
        authorization=RunAuthorization(apply=True), cancel=cancel,
    )
    assert recovered.committed == 1
    assert target.read_bytes() == b"verified final bytes"
    before = target.stat()
    events = tuple(event.event_id for event in StateStore(state).list_outbox())
    repeated = recover_operation(
        plan.operation_id, state_dir=state, roots=(library,),
        authorization=RunAuthorization(apply=True), cancel=cancel,
    )
    assert repeated.failed == 0
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert tuple(event.event_id for event in StateStore(state).list_outbox()) == events


@pytest.mark.parametrize("mode", ["apply", "recover", "complete", "apply-complete"])
def test_lock_release_fsync_failure_preserves_committed_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    library = tmp_path / "books"
    library.mkdir()
    state = tmp_path / "state"
    target = library / "published.bin"
    plan = _plan(
        "release-accounting",
        (Operation("publish", "publish", target, artifact=ArtifactSpec(content=b"verified final bytes")),),
        library,
    )
    if mode != "apply":
        def stop_after_stage(event: str, member: str) -> None:
            if event == "staged":
                raise OSError("injected staging failure")

        prepared = TransactionExecutor(
            state, (library,), checkpoint=stop_after_stage if mode == "recover" else None,
        ).apply(plan, authorization=RunAuthorization(apply=True))
        assert prepared.committed == (0 if mode == "recover" else 1)
    if mode == "apply-complete":
        plan = _plan(
            plan.operation_id,
            (Operation("publish", "publish", target, destination_before=fingerprint(target, library),
                       artifact=ArtifactSpec(content=b"verified final bytes"), collision="replace"),),
            library,
        )
    real_fsync = os.fsync
    failed_locks: list[Path] = []
    complete = mode in {"complete", "apply-complete"}
    lock_path = state / "apply.lock" if mode == "apply-complete" else library / ".kce-transaction.lock"
    releasing = False

    def fail_release_flush(descriptor: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if releasing and path == lock_path and target.exists():
            record = json.loads(path.read_bytes())
            if record["owner"] is None:
                failed_locks.append(path)
                raise OSError(errno.EIO, "injected lock release flush failure")
        real_fsync(descriptor)

    executor = TransactionExecutor(state, (library,))
    real_release = executor._release_locks

    def release_with_flush_fault(locks: list[transactions._HeldLock], operation_id: str | None) -> str | None:
        nonlocal releasing
        releasing = True
        return real_release(locks, operation_id)

    with monkeypatch.context() as patch:
        patch.setattr(transactions.os, "fsync", fail_release_flush)
        patch.setattr(executor, "_release_locks", release_with_flush_fault)
        outcome = (
            executor.apply(plan, authorization=RunAuthorization(apply=True))
            if mode in {"apply", "apply-complete"} else executor.recover(plan.operation_id, authorization=RunAuthorization(apply=True))
        )
    assert failed_locks == [lock_path]
    assert (outcome.committed, outcome.skipped, outcome.failed, outcome.exit_code) == (
        0 if complete else 1, 1 if complete else 0, 1, 1,
    )
    assert any(decision.reason == "lock_release_failed" for decision in outcome.decisions)
    assert outcome.plans == (plan,)
    assert target.read_bytes() == b"verified final bytes"
    record = executor.state_store.get_operation(plan.operation_id)
    assert record is not None and record.phase == "complete"
    assert record.owner_pid is None and record.owner_start is None
    events = executor.state_store.list_outbox(operation_id=plan.operation_id)
    assert len(events) == 1
    published = fingerprint(target, library)
    repeated = executor.recover(plan.operation_id, authorization=RunAuthorization(apply=True))
    assert (repeated.committed, repeated.skipped, repeated.failed) == (0, 1, 0)
    assert fingerprint(target, library) == published
    assert executor.state_store.list_outbox(operation_id=plan.operation_id) == events


@pytest.mark.skipif(os.geteuid() == 0, reason="read-only filesystem proof requires an unprivileged user")
@pytest.mark.parametrize("nested_state", [False, True])
@pytest.mark.parametrize("recover_after_stage", [False, True])
def test_readonly_publication_source_is_not_a_lock_boundary(
    tmp_path: Path, nested_state: bool, recover_after_stage: bool,
) -> None:
    source_root = tmp_path / "import"
    source_root.mkdir()
    source = source_root / "publishers.txt"
    source.write_bytes(b"Literal.[Publisher]+\n")
    state = source_root / "state" if nested_state else tmp_path / "state"
    state.mkdir()
    source.chmod(0o444)
    source_root.chmod(0o555)
    def source_snapshot() -> tuple[bytes, int, int, int, int]:
        return (source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns,
                source_root.stat().st_mode, source_root.stat().st_mtime_ns)

    before = source_snapshot()
    observed = fingerprint(source, source_root)
    destination = state / source.name
    plan = OperationPlan(
        "readonly-import", "readonly-import",
        (Operation("publishers", "publish", destination, source=observed,
                   artifact=ArtifactSpec(source=observed)),),
        (RootPermission(state), RootPermission(source_root, write=False)),
    )

    def stop_after_stage(phase: str, member_id: str) -> None:
        if phase == "staged":
            raise OSError("injected interruption after staging")

    roots = (source_root,) if nested_state else (state, source_root)
    try:
        assert not os.access(source_root, os.W_OK)
        applied = TransactionExecutor(
            state, roots, checkpoint=stop_after_stage if recover_after_stage else None,
        ).apply(plan, authorization=RunAuthorization(apply=True))
        if recover_after_stage:
            assert (applied.committed, applied.failed) == (0, 1)
            assert not destination.exists()
            result = TransactionExecutor(state, roots).recover(
                plan.operation_id, authorization=RunAuthorization(apply=True),
            )
        else:
            result = applied
        assert (result.exit_code, result.committed, result.failed) == (0, 1, 0)
        assert destination.read_bytes() == before[0]
        assert source_snapshot() == before
        assert not (source_root / ".kce-transaction.lock").exists()
        repeated = TransactionExecutor(state, roots).recover(
            plan.operation_id, authorization=RunAuthorization(apply=True),
        )
        assert (repeated.committed, repeated.skipped, repeated.failed) == (0, 1, 0)
        assert source_snapshot() == before
    finally:
        source_root.chmod(0o755)
        source.chmod(0o644)


def test_state_preparation_release_failure_does_not_report_success_or_retain_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    original_fsync = os.fsync
    failures: list[Path] = []

    def fail_lock_flush(descriptor: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if path == state / "apply.lock" and path.read_bytes():
            failures.append(path)
            raise OSError(errno.EIO, "injected preparation lock flush failure")
        original_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(transactions.os, "fsync", fail_lock_flush)
        failed = TransactionExecutor(state, (tmp_path,)).prepare_state_root(
            authorization=RunAuthorization(apply=True),
        )
    assert failures == [state / "apply.lock"]
    assert (failed.status, failed.reason) == ("failed", "state_root_lock_release_failed")
    prepared = TransactionExecutor(state, (tmp_path,)).prepare_state_root(
        authorization=RunAuthorization(apply=True),
    )
    assert (prepared.status, prepared.reason) == ("observed", "state_root_prepared")

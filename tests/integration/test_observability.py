from __future__ import annotations

import errno
import json
import logging
import os
import pstats
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure import observability
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.observability import (
    RunObservability,
    render_diagnostic,
    run_profiled,
)
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor


def _plan(root: Path, *, operation_id: str = "observability-commit") -> OperationPlan:
    return OperationPlan("observability-tests-v1", operation_id, (
        Operation("book", "publish", root / "book.cbz", artifact=ArtifactSpec(content=b"verified book bytes")),
    ), (RootPermission(root),))


def _commit(tmp_path: Path) -> RunResult:
    root = tmp_path / "library"
    root.mkdir()
    return TransactionExecutor(tmp_path / "state", (root,)).apply(
        _plan(root), authorization=RunAuthorization(apply=True),
    )


def _logs(directory: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (directory / "run.log").read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(("apply", "log_to_file"), [(False, False), (False, True), (True, False)])
def test_logging_requires_both_local_apply_and_logging_toggle(
    tmp_path: Path, apply: bool, log_to_file: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    log_dir = tmp_path / "absent" / "logs"
    result = RunResult((Decision("skipped", "artwork_unchanged", tmp_path / "book.cbz"),), skipped=1)
    with RunObservability(log_dir=log_dir, log_to_file=log_to_file,
                          authorization=RunAuthorization(apply=apply)) as scope:
        failure = scope.record(result)
        diagnostic_failure = scope.diagnostic(result.decisions[0])
        assert scope.counts == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
        assert scope.log_path is None
    output = capsys.readouterr()
    assert failure is None and diagnostic_failure is None
    assert output.out == ""
    assert output.err == f'skipped: artwork_unchanged {json.dumps(str(tmp_path / "book.cbz"))}\n'
    assert not log_dir.parent.exists()


def test_applied_results_use_private_logs_and_source_counts_without_global_handlers(tmp_path: Path) -> None:
    result = _commit(tmp_path)
    skipped = RunResult((Decision("skipped", "artwork_unchanged", tmp_path / "library" / "book.cbz"),), skipped=1)
    log_dir = tmp_path / "logs"
    root_logger = logging.getLogger()
    old_handlers = tuple(root_logger.handlers)
    old_level = root_logger.level
    with RunObservability(log_dir=log_dir, log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        assert scope.record(result) is None
        assert scope.record(skipped) is None
        assert scope.counts == {"planned": 1, "committed": 1, "skipped": 1, "failed": 0}
        assert scope.log_path == log_dir / "run.log"
    assert result.committed == 1
    assert (tmp_path / "library" / "book.cbz").read_bytes() == b"verified book bytes"
    records = _logs(log_dir)
    assert records[0]["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert records[1]["counts"] == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
    assert records[1]["run_counts"] == {"planned": 1, "committed": 1, "skipped": 1, "failed": 0}
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((log_dir / "run.log").stat().st_mode) == 0o600
    assert tuple(root_logger.handlers) == old_handlers and root_logger.level == old_level
    assert scope.failure is None


def test_scopes_keep_counts_and_elapsed_time_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(observability, "monotonic", lambda: clock[0])
    with RunObservability(log_dir=tmp_path / "one", log_to_file=False,
                          authorization=RunAuthorization()) as first:
        assert first.record(RunResult(skipped=2)) is None
        clock[0] = 12.0
        with RunObservability(log_dir=tmp_path / "two", log_to_file=False,
                              authorization=RunAuthorization()) as second:
            assert second.record(RunResult(failed=1)) is None
            clock[0] = 15.0
        assert second.elapsed_seconds == 3.0
        clock[0] = 18.0
    assert first.counts == {"planned": 0, "committed": 0, "skipped": 2, "failed": 0}
    assert second.counts == {"planned": 0, "committed": 0, "skipped": 0, "failed": 1}
    assert first.elapsed_seconds == 8.0
    clock[0] = 20.0
    assert first.elapsed_seconds == 8.0 and second.elapsed_seconds == 3.0


def test_rotation_uses_five_mib_and_retains_three_private_backups(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    result = RunResult((Decision("skipped", "artwork_unchanged"),), skipped=1)
    with RunObservability(log_dir=log_dir, log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        path = log_dir / "run.log"
        for index in range(4):
            with path.open("wb") as stream:
                stream.write(f"generation-{index}\n".encode())
                stream.truncate(5 * 1024 * 1024 - 1)
            assert scope.record(result) is None
    assert sorted(path.name for path in log_dir.iterdir()) == ["run.log", "run.log.1", "run.log.2", "run.log.3"]
    for index, generation in [(1, 3), (2, 2), (3, 1)]:
        path = log_dir / f"run.log.{index}"
        with path.open("rb") as stream:
            assert stream.read(13) == f"generation-{generation}\n".encode()
        assert path.stat().st_size == 5 * 1024 * 1024 - 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _logs(log_dir)[0]["run_counts"] == {"planned": 0, "committed": 0, "skipped": 4, "failed": 0}
    assert (log_dir / "run.log").stat().st_size < 5 * 1024 * 1024


def test_registered_reasons_and_schema_exclude_credentials_and_artifact_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "fictional-private-password"
    endpoint = "https://user:fictional-private-password@discord.invalid/api/webhooks/123/private-token"
    structured = json.dumps({"Authorization": f"Bearer {secret}", "webhookSource": endpoint,
                             "password": secret, "stderr": f"worker failed with {secret}"})
    plan = OperationPlan(endpoint, "secret-safe", (
        Operation("book", "publish", tmp_path / "book.cbz", artifact=ArtifactSpec(content=structured.encode()),
                  reason=structured),
    ), (RootPermission(tmp_path),), reason=structured)
    result = RunResult((
        Decision("committed", "local_commit_complete", tmp_path / "reader-id" / "book.cbz", (structured,)),
        Decision("failed", f"RuntimeError: {endpoint}"),
        Decision("review_required", "a_credential_shaped_like_a_code"),
    ), plans=(plan,), committed=1, failed=1, deferred_deliveries=(endpoint,))

    def reject_artifact_serialization(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("observability must not inspect encoded publication artifacts")

    monkeypatch.setattr(ArtifactSpec, "as_dict", reject_artifact_serialization)
    log_dir = tmp_path / "logs"
    with RunObservability(log_dir=log_dir, log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        assert scope.record(result) is None
        assert scope.diagnostic(result.decisions[1]) is None
    text = (log_dir / "run.log").read_text(encoding="utf-8")
    assert secret not in text and endpoint not in text and "private-token" not in text
    assert "Authorization" not in text and "webhookSource" not in text and "stderr" not in text
    assert "a_credential_shaped_like_a_code" not in text
    records = _logs(log_dir)
    assert records[0]["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 1}
    assert records[0]["decisions"] == [
        {"status": "committed", "reason": "local_commit_complete", "path": str(tmp_path / "reader-id" / "book.cbz"), "candidates": []},
        {"status": "failed", "reason": "diagnostic_detail_omitted", "path": None, "candidates": []},
        {"status": "review_required", "reason": "diagnostic_detail_omitted", "path": None, "candidates": []},
    ]
    output = capsys.readouterr()
    assert output.out == "" and output.err == "failed: diagnostic_detail_omitted\n"


def test_diagnostics_escape_local_filename_controls_and_use_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    render_diagnostic(Decision("review_required", "cleanup_pending", tmp_path / "book\n\x1b[31m.cbz"))
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f'review_required: cleanup_pending {json.dumps(str(tmp_path / "book\n\x1b[31m.cbz"))}\n'


@pytest.mark.parametrize("unsafe", ["parent_symlink", "root_symlink", "root_permissions", "log_symlink", "log_fifo", "log_hardlink"])
def test_unsafe_log_locations_fail_without_following_links_or_blocking(tmp_path: Path, unsafe: str) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    protected = actual / "untouched"
    protected.write_bytes(b"do not modify")
    protected.chmod(0o600)
    log_dir = tmp_path / "logs"
    if unsafe == "parent_symlink":
        parent = tmp_path / "linked"
        parent.symlink_to(actual, target_is_directory=True)
        log_dir = parent / "logs"
    elif unsafe == "root_symlink":
        log_dir.symlink_to(actual, target_is_directory=True)
    else:
        log_dir.mkdir(mode=0o755 if unsafe == "root_permissions" else 0o700)
        log_dir.chmod(0o755 if unsafe == "root_permissions" else 0o700)
        log = log_dir / "run.log"
        if unsafe == "log_symlink":
            log.symlink_to(protected)
        elif unsafe == "log_fifo":
            os.mkfifo(log, mode=0o600)
        elif unsafe == "log_hardlink":
            os.link(protected, log)
    with RunObservability(log_dir=log_dir, log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        failure = scope.record(RunResult(skipped=1))
    assert failure is not None and failure.status in {"review_required", "publication_failed"}
    assert protected.read_bytes() == b"do not modify"
    assert not (actual / "logs").exists()
    assert not (actual / "run.log").exists()
    if unsafe == "root_permissions":
        assert not (log_dir / "run.log").exists()
    assert scope.counts == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}


def test_log_failure_after_verified_commit_does_not_rewrite_commit_counts(tmp_path: Path) -> None:
    result = _commit(tmp_path)
    log_dir = tmp_path / "logs"
    with RunObservability(log_dir=log_dir, log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        log = log_dir / "run.log"
        log.rename(log_dir / "previous.log")
        os.mkfifo(log, mode=0o600)
        diagnostic = scope.record(result)
    assert diagnostic == Decision("publication_failed", "log_write_failed")
    assert scope.counts == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert result.exit_code == 0 and result.committed == 1
    assert (tmp_path / "library" / "book.cbz").read_bytes() == b"verified book bytes"
    assert stat.S_ISFIFO((log_dir / "run.log").lstat().st_mode)
    assert (log_dir / "previous.log").read_bytes() == b""


def test_cleanup_pending_is_visible_without_inventing_member_publication(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "incoming.cbz"
    source.write_bytes(b"move bytes")
    target = root / "book.cbz"
    plan = OperationPlan("observability-tests-v1", "pending-cleanup", (
        Operation("book", "move", target, source=fingerprint(source, root)),
    ), (RootPermission(root),))

    def fail_cleanup(checkpoint: str, member: str) -> None:
        if checkpoint == "before_cleanup":
            raise OSError(errno.EACCES, "fictional-credential-that-must-not-be-logged")

    result = TransactionExecutor(tmp_path / "state", (root,), checkpoint=fail_cleanup).apply(
        plan, authorization=RunAuthorization(apply=True),
    )
    with RunObservability(log_dir=tmp_path / "logs", log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        assert scope.record(result) is None
    record = _logs(tmp_path / "logs")[0]
    assert record["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert record["exit_code"] == 3
    assert record["decisions"] == [{"status": "review_required", "reason": "cleanup_pending", "path": None, "candidates": []}]
    assert "fictional-credential-that-must-not-be-logged" not in (tmp_path / "logs" / "run.log").read_text()
    assert source.read_bytes() == b"move bytes" and target.read_bytes() == b"move bytes"


def test_profile_records_the_selected_service_and_does_not_evaluate_source(tmp_path: Path) -> None:
    untouched = tmp_path / "sentinel"
    untouched.write_bytes(b"preserved")
    source_text = f"__import__('os').unlink({str(untouched)!r})"
    root = tmp_path / "library"
    root.mkdir()
    artifact = ArtifactSpec(content=source_text.encode())
    plan = replace(_plan(root), operations=(Operation("book", "publish", root / "book.cbz", artifact=artifact),))

    def selected_service() -> RunResult:
        return TransactionExecutor(tmp_path / "state", (root,)).apply(plan, authorization=RunAuthorization(apply=True))

    profile = tmp_path / "selected.prof"
    result = run_profiled(selected_service, profile)
    stats = pstats.Stats(str(profile))
    calls = [values[:2] for key, values in stats.stats.items() if key[2] == "selected_service"]
    assert calls == [(1, 1)]
    assert result.committed == 1 and result.exit_code == 0
    assert (root / "book.cbz").read_text() == source_text
    assert untouched.read_bytes() == b"preserved"
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600


@pytest.mark.parametrize("collision", ["file", "symlink", "fifo", "parent_symlink", "missing_parent"])
def test_profile_preflight_blocks_service_effects_for_unsafe_output(tmp_path: Path, collision: str) -> None:
    protected = tmp_path / "protected"
    protected.write_bytes(b"preserved")
    profile = tmp_path / "chosen.prof"
    if collision == "file":
        profile.write_bytes(b"existing profile")
    elif collision == "symlink":
        profile.symlink_to(protected)
    elif collision == "fifo":
        os.mkfifo(profile)
    elif collision == "parent_symlink":
        parent = tmp_path / "linked"
        parent.symlink_to(tmp_path, target_is_directory=True)
        profile = parent / "chosen.prof"
    else:
        profile = tmp_path / "missing" / "chosen.prof"

    def service() -> RunResult:
        protected.write_bytes(b"wrongly executed")
        return RunResult(committed=1)

    result = run_profiled(service, profile)
    assert result.committed == 0 and result.planned == 0
    assert result.exit_code in {1, 3}
    assert protected.read_bytes() == b"preserved"
    if collision == "file":
        assert profile.read_bytes() == b"existing profile"
        assert result.decisions == (Decision("conflict", "profile_output_exists", profile),)
    if collision == "missing_parent":
        assert not profile.parent.exists()


def test_unrequested_profile_executes_service_without_output(tmp_path: Path) -> None:
    result = run_profiled(lambda: _commit(tmp_path), None)
    assert result.committed == 1 and result.exit_code == 0
    assert (tmp_path / "library" / "book.cbz").read_bytes() == b"verified book bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["library", "state"]


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_profile_cleans_owned_output_and_descriptor_on_service_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException],
) -> None:
    profile = tmp_path / "interrupted.prof"
    opened: list[int] = []
    real_open = os.open

    def capture_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == profile.name:
            opened.append(descriptor)
        return descriptor

    def service() -> RunResult:
        (tmp_path / "effect").write_bytes(b"service started")
        raise error("fictional-private-credential")

    monkeypatch.setattr(observability.os, "open", capture_open)
    with pytest.raises(error):
        run_profiled(service, profile)
    assert (tmp_path / "effect").read_bytes() == b"service started"
    assert not profile.exists()
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_profile_write_failure_retains_actual_committed_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "failed.prof"
    original: list[RunResult] = []

    def fail_flush(descriptor: int) -> None:
        raise OSError(errno.EIO, "fictional-private-credential")

    def service() -> RunResult:
        result = _commit(tmp_path)
        original.append(result)
        monkeypatch.setattr(observability.os, "fsync", fail_flush)
        return result

    result = run_profiled(service, profile)
    assert result.committed == 1 and result.planned == 1 and result.failed == 0
    assert result.plans is original[0].plans
    assert result.exit_code == 1
    assert result.decisions[-1] == Decision("publication_failed", "profile_write_failed", profile)
    assert result.as_dict()["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert (tmp_path / "library" / "book.cbz").read_bytes() == b"verified book bytes"
    assert not profile.exists()
    assert "fictional-private-credential" not in json.dumps(result.as_dict())


def test_profile_replaced_by_another_writer_is_preserved(tmp_path: Path) -> None:
    profile = tmp_path / "replaced.prof"

    def service() -> RunResult:
        profile.unlink()
        profile.write_bytes(b"other writer")
        profile.chmod(0o600)
        return RunResult(skipped=1)

    result = run_profiled(service, profile)
    assert result.exit_code == 3 and result.skipped == 1
    assert result.decisions == (Decision("review_required", "profile_output_changed", profile),)
    assert profile.read_bytes() == b"other writer"


def test_profile_permission_setup_failure_closes_descriptor_and_blocks_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "permission.prof"
    descriptors: list[int] = []

    def fail_permissions(descriptor: int, mode: int) -> None:
        descriptors.append(descriptor)
        raise OSError(errno.EPERM, "fictional-private-credential")

    monkeypatch.setattr(observability.os, "fchmod", fail_permissions)
    result = run_profiled(lambda: _commit(tmp_path), profile)
    assert result.decisions == (Decision("publication_failed", "profile_open_failed", profile),)
    assert result.committed == 0 and not (tmp_path / "library").exists()
    assert not profile.exists()
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF


def test_explicit_profile_output_is_allowed_in_preview_without_implicit_logs_or_state(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    profile = tmp_path / "preview.prof"

    def preview_service() -> RunResult:
        return TransactionExecutor(tmp_path / "state", (root,)).apply(
            _plan(root), authorization=RunAuthorization(),
        )

    with RunObservability(log_dir=tmp_path / "logs", log_to_file=True,
                          authorization=RunAuthorization()) as scope:
        result = run_profiled(preview_service, profile)
        assert scope.record(result) is None
    assert result.exit_code == 0
    assert scope.counts == {"planned": 1, "committed": 0, "skipped": 0, "failed": 0}
    calls = [values[:2] for key, values in pstats.Stats(str(profile)).stats.items() if key[2] == "preview_service"]
    assert calls == [(1, 1)]
    assert list(root.iterdir()) == []
    assert not (tmp_path / "logs").exists() and not (tmp_path / "state").exists()


def test_logger_refuses_a_replaced_parent_before_recording(tmp_path: Path) -> None:
    original = tmp_path / "logs"
    retained = tmp_path / "retained"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with RunObservability(log_dir=original, log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        original.rename(retained)
        original.symlink_to(outside, target_is_directory=True)
        diagnostic = scope.record(RunResult(skipped=1))
    assert diagnostic == Decision("publication_failed", "log_write_failed")
    assert (retained / "run.log").read_bytes() == b""
    assert list(outside.iterdir()) == []
    assert scope.counts == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}


def test_cancelled_logging_scope_does_not_create_outputs(tmp_path: Path) -> None:
    with RunObservability(log_dir=tmp_path / "logs", log_to_file=True,
                          authorization=RunAuthorization(apply=True), cancel=lambda: True) as scope:
        diagnostic = scope.record(RunResult((Decision("cancelled", "cancelled"),)))
    assert diagnostic == Decision("cancelled", "observability_cancelled")
    assert scope.failure == diagnostic
    assert scope.counts == {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
    assert list(tmp_path.iterdir()) == []


def test_closed_scopes_reject_results_without_changing_counts(tmp_path: Path) -> None:
    with RunObservability(log_dir=tmp_path / "logs", log_to_file=True,
                          authorization=RunAuthorization(apply=True)) as scope:
        assert scope.record(RunResult(skipped=1)) is None
    original_bytes = (tmp_path / "logs" / "run.log").read_bytes()
    assert scope.close() is None
    assert scope.record(RunResult(committed=1)) == Decision("review_required", "observability_not_open")
    assert scope.counts == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
    assert (tmp_path / "logs" / "run.log").read_bytes() == original_bytes


def test_profile_stream_setup_failure_preserves_commit_and_closes_owned_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "stream-failure.prof"
    descriptors: list[int] = []

    def fail_stream(descriptor: int, *args: object, **kwargs: object) -> None:
        descriptors.append(descriptor)
        raise OSError(errno.EIO, "fictional-private-credential")

    def service() -> RunResult:
        result = _commit(tmp_path)
        monkeypatch.setattr(observability.os, "fdopen", fail_stream)
        return result

    result = run_profiled(service, profile)
    assert result.exit_code == 1 and result.committed == 1 and result.failed == 0
    assert result.decisions[-1] == Decision("publication_failed", "profile_write_failed", profile)
    assert (tmp_path / "library" / "book.cbz").read_bytes() == b"verified book bytes"
    assert not profile.exists()
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF


def test_count_snapshots_cannot_mutate_the_scoped_counters(tmp_path: Path) -> None:
    with RunObservability(log_dir=tmp_path / "logs", log_to_file=False,
                          authorization=RunAuthorization()) as scope:
        snapshot = scope.counts
        snapshot["committed"] = 99
        assert scope.record(RunResult(skipped=1)) is None
        assert scope.counts == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
        assert snapshot == {"planned": 0, "committed": 99, "skipped": 0, "failed": 0}

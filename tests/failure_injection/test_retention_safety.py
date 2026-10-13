from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from komga_cover_extractor.domain.decisions import RunAuthorization, RunResult
from komga_cover_extractor.domain.operations import (
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure import transactions
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

_PACKAGE_SOURCE = Path(__file__).resolve().parents[2] / "src"
_CRASH_EXIT = 83


def _run_python(directory: Path, source: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(_PACKAGE_SOURCE), environment.get("PYTHONPATH", "")) if path
    )
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _crash_apply(
    directory: Path,
    root: Path,
    state: Path,
    plan_source: str,
    *,
    checkpoint_phase: str,
    checkpoint_member: str,
) -> subprocess.CompletedProcess[str]:
    plan_lines = textwrap.dedent(plan_source).strip().splitlines()
    source = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "from komga_cover_extractor.domain.decisions import RunAuthorization",
            "from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission",
            "from komga_cover_extractor.infrastructure.filesystem import fingerprint",
            "from komga_cover_extractor.infrastructure.transactions import TransactionExecutor",
            "",
            f"root = Path({str(root)!r})",
            f"state = Path({str(state)!r})",
            *plan_lines,
            "",
            "def checkpoint(phase, member):",
            f"    if phase == {checkpoint_phase!r} and member == {checkpoint_member!r}:",
            f"        os._exit({_CRASH_EXIT})",
            "",
            "TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(",
            "    plan, authorization=RunAuthorization(apply=True)",
            ")",
            "raise SystemExit(1)",
        ]
    )
    return _run_python(directory, source)


def _recover(root: Path, state: Path, operation_id: str) -> RunResult:
    return TransactionExecutor(state, (root,)).recover(
        operation_id,
        authorization=RunAuthorization(apply=True),
    )


def _has_status(result: RunResult, *statuses: str) -> bool:
    return any(decision.status in statuses for decision in result.decisions)


def test_recovery_keeps_unique_move_output_when_source_changed_and_later_member_conflicts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    state = tmp_path / "state"
    source = root / "source.bin"
    source.write_bytes(b"observed-source-bytes")
    first_output = root / "first-output.bin"
    later_output = root / "later-output.bin"

    crashed = _crash_apply(
        tmp_path,
        root,
        state,
        """
        plan = OperationPlan("retention-safety-v1", "changed-source-conflict", (
            Operation("first-move", "move", root / "first-output.bin", source=fingerprint(root / "source.bin", root)),
            Operation("later-publish", "publish", root / "later-output.bin", artifact=ArtifactSpec(content=b"planned-later")),
        ), (RootPermission(root),))
        """,
        checkpoint_phase="member_committed",
        checkpoint_member="first-move",
    )
    assert crashed.returncode == _CRASH_EXIT
    assert first_output.read_bytes() == b"observed-source-bytes"

    source.write_bytes(b"external-source-update")
    later_output.write_bytes(b"external-destination")
    result = _recover(root, state, "changed-source-conflict")

    assert _has_status(result, "review_required", "conflict")
    assert source.read_bytes() == b"external-source-update"
    assert first_output.read_bytes() == b"observed-source-bytes"
    assert later_output.read_bytes() == b"external-destination"


def test_replacement_rollback_retains_new_candidate_and_restores_independent_backup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    state = tmp_path / "state"
    target = root / "replace.bin"
    target.write_bytes(b"original-target")
    later_target = root / "later.bin"

    crashed = _crash_apply(
        tmp_path,
        root,
        state,
        """
        target = root / "replace.bin"
        plan = OperationPlan("retention-safety-v1", "replacement-conflict", (
            Operation(
                "first-replace", "publish", target,
                destination_before=fingerprint(target, root),
                artifact=ArtifactSpec(content=b"published-candidate"),
                collision="replace",
            ),
            Operation("later-publish", "publish", root / "later.bin", artifact=ArtifactSpec(content=b"planned-later")),
        ), (RootPermission(root),))
        """,
        checkpoint_phase="member_committed",
        checkpoint_member="first-replace",
    )
    assert crashed.returncode == _CRASH_EXIT
    assert target.read_bytes() == b"published-candidate"

    later_target.write_bytes(b"external-destination")
    result = _recover(root, state, "replacement-conflict")

    assert _has_status(result, "review_required", "conflict")
    assert target.read_bytes() == b"original-target"
    backup = next(root.glob(".kce-quarantine-*/backup"))
    candidate = backup.parent / "rollback-candidate"
    assert backup.read_bytes() == b"original-target"
    assert candidate.read_bytes() == b"published-candidate"
    assert (backup.stat().st_dev, backup.stat().st_ino) != (
        target.stat().st_dev,
        target.stat().st_ino,
    )

@pytest.mark.parametrize(
    ("checkpoint_phase", "retained_pattern", "retained_bytes"),
    [
        ("staged", ".kce-stage-*", b"published-candidate"),
        ("backed_up", ".kce-quarantine-*/backup", b"original-target"),
    ],
)
def test_recovery_refuses_to_use_unflushed_stage_or_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_phase: str,
    retained_pattern: str,
    retained_bytes: bytes,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    target = root / "replace.bin"
    target.write_bytes(b"original-target")
    state = tmp_path / "state"

    crashed = _crash_apply(
        tmp_path,
        root,
        state,
        """
        target = root / "replace.bin"
        plan = OperationPlan("retention-safety-v1", "unflushed-recovery-copy", (
            Operation(
                "replace-target", "publish", target,
                destination_before=fingerprint(target, root),
                artifact=ArtifactSpec(content=b"published-candidate"),
                collision="replace",
            ),
        ), (RootPermission(root),))
        """,
        checkpoint_phase=checkpoint_phase,
        checkpoint_member="replace-target",
    )
    assert crashed.returncode == _CRASH_EXIT
    retained_path = next(root.glob(retained_pattern))
    assert retained_path.read_bytes() == retained_bytes

    real_fsync = os.fsync
    failed_flushes = 0

    def fail_recovered_copy_fsync(descriptor: int) -> None:
        nonlocal failed_flushes
        try:
            opened_path = os.path.abspath(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            opened_path = ""
        if opened_path == str(retained_path):
            failed_flushes += 1
            raise OSError(errno.EIO, "injected recovered-copy flush failure", str(retained_path))
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(transactions.os, "fsync", fail_recovered_copy_fsync)
        result = _recover(root, state, "unflushed-recovery-copy")

    assert failed_flushes >= 1
    assert result.committed == 0
    assert target.read_bytes() == b"original-target"
    assert retained_path.read_bytes() == retained_bytes


def test_repeated_retained_file_fsync_failure_never_removes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    state = tmp_path / "state"
    source = root / "source.bin"
    target = root / "target.bin"
    source.write_bytes(b"source-to-retain")
    plan = OperationPlan(
        "retention-safety-v1",
        "retained-fsync-failure",
        (Operation("move-source", "move", target, source=fingerprint(source, root)),),
        (RootPermission(root),),
    )
    executor = TransactionExecutor(state, (root,))
    real_unlink = os.unlink

    def fail_source_unlink(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
        if Path(path) == source:
            raise OSError(errno.EIO, "injected source unlink failure", str(source))
        if dir_fd is None:
            real_unlink(path)
        else:
            real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(transactions.os, "unlink", fail_source_unlink)
        first = executor.apply(plan, authorization=RunAuthorization(apply=True))
    assert _has_status(first, "review_required", "publication_failed")
    retained = next(root.glob(".kce-quarantine-*/source"))
    assert source.read_bytes() == b"source-to-retain"
    assert retained.read_bytes() == b"source-to-retain"

    real_fsync = os.fsync
    failed_flushes = 0

    def fail_retained_fsync(descriptor: int) -> None:
        nonlocal failed_flushes
        try:
            opened_path = os.path.abspath(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            opened_path = ""
        if opened_path == str(retained):
            failed_flushes += 1
            raise OSError(errno.EIO, "persistent retained-copy flush failure", str(retained))
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(transactions.os, "fsync", fail_retained_fsync)
        for _ in range(2):
            result = executor.recover("retained-fsync-failure", authorization=RunAuthorization(apply=True))
            assert _has_status(result, "review_required", "publication_failed")
            assert source.read_bytes() == b"source-to-retain"
            assert target.read_bytes() == b"source-to-retain"
            assert retained.read_bytes() == b"source-to-retain"

    assert failed_flushes >= 2


def test_recovery_accepts_journaled_nested_rmdirs_after_parent_cleanup_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    source_parent = root / "Old"
    source_directory = source_parent / "child"
    source_directory.mkdir(parents=True)
    (root / "destination").mkdir()
    source = source_directory / "book.cbz"
    source.write_bytes(b"nested-source")
    state = tmp_path / "state"

    crashed = _crash_apply(
        tmp_path,
        root,
        state,
        """
        old = root / "Old"
        child = old / "child"
        source = child / "book.cbz"
        plan = OperationPlan("retention-safety-v1", "nested-rmdir-recovery", (
            Operation("move-book", "move", root / "destination" / "book.cbz", source=fingerprint(source, old)),
            Operation("remove-child", "rmdir", child, depends_on=("move-book",)),
            Operation("remove-parent", "rmdir", old, depends_on=("remove-child",)),
        ), (RootPermission(root),))
        """,
        checkpoint_phase="cleanup_done",
        checkpoint_member="remove-parent",
    )
    assert crashed.returncode == _CRASH_EXIT
    assert not source_parent.exists()

    result = _recover(root, state, "nested-rmdir-recovery")

    assert result.committed == 0
    assert not _has_status(result, "review_required", "conflict", "publication_failed")
    assert not source_parent.exists()
    assert (root / "destination" / "book.cbz").read_bytes() == b"nested-source"
    assert _recover(root, state, "nested-rmdir-recovery").committed == 0


def test_narrow_source_fingerprint_can_move_then_remove_its_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    old = root / "Old"
    old.mkdir(parents=True)
    destination_parent = root / "destination"
    destination_parent.mkdir()
    source = old / "book.cbz"
    source.write_bytes(b"narrowly-rooted-source")
    target = destination_parent / "book.cbz"
    plan = OperationPlan(
        "retention-safety-v1",
        "narrow-source-root",
        (
            Operation("move-book", "move", target, source=fingerprint(source, old)),
            Operation("remove-old", "rmdir", old, depends_on=("move-book",)),
        ),
        (RootPermission(root),),
    )

    result = TransactionExecutor(tmp_path / "state", (root,)).apply(
        plan,
        authorization=RunAuthorization(apply=True),
    )

    assert result.committed == 1
    assert not _has_status(result, "review_required", "conflict", "publication_failed")
    assert not old.exists()
    assert target.read_bytes() == b"narrowly-rooted-source"
    retained = next(root.glob(".kce-quarantine-*/source"))
    assert retained.read_bytes() == b"narrowly-rooted-source"
    assert (retained.stat().st_dev, retained.stat().st_ino) != (
        target.stat().st_dev,
        target.stat().st_ino,
    )

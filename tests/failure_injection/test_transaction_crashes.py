from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure import transactions
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_SOURCE = _SOURCE_ROOT / "src"
_CRASH_EXIT = 83


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    paths = [str(_PACKAGE_SOURCE), environment.get("PYTHONPATH", "")]
    environment["PYTHONPATH"] = os.pathsep.join(path for path in paths if path)
    return environment


def _run_python(directory: Path, source: str, *, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=directory,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _crash_apply(
    directory: Path,
    root: Path,
    state: Path,
    operation_id: str,
    checkpoint_phase: str,
    kind: str,
    *,
    member_id: str = "item",
) -> subprocess.CompletedProcess[str]:
    return _run_python(
        directory,
        f"""
        import os
        import sys
        from pathlib import Path
        from komga_cover_extractor.domain.decisions import RunAuthorization
        from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
        from komga_cover_extractor.infrastructure.filesystem import fingerprint
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

        root = Path({str(root)!r})
        state = Path({str(state)!r})
        target = root / 'output.bin'
        operation_id = {operation_id!r}
        member_id = {member_id!r}
        checkpoint_member = '' if {checkpoint_phase!r} == 'group_committed' else member_id
        kind = {kind!r}
        if kind == 'move':
            source = root / 'source.bin'
            operation = Operation(member_id, 'move', target, source=fingerprint(source, root))
        elif kind == 'replace':
            prior = fingerprint(target, root)
            operation = Operation(
                member_id, 'publish', target, destination_before=prior,
                artifact=ArtifactSpec(content=b'committed-new-bytes'), collision='replace',
            )
        else:
            operation = Operation(
                member_id, 'publish', target,
                artifact=ArtifactSpec(content=b'committed-new-bytes'),
            )
        plan = OperationPlan(
            'failure-injection-v1', operation_id, (operation,), (RootPermission(root),)
        )
        def checkpoint(phase, observed_member):
            if phase == {checkpoint_phase!r} and observed_member == checkpoint_member:
                os._exit({_CRASH_EXIT})
        result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(
            plan, authorization=RunAuthorization(apply=True)
        )
        print(result.as_dict())
        sys.exit(1)
        """,
    )


def _recover(directory: Path, root: Path, state: Path, operation_id: str) -> dict[str, Any]:
    process = _run_python(
        directory,
        f"""
        import json
        from pathlib import Path
        from komga_cover_extractor.domain.decisions import RunAuthorization
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
        result = TransactionExecutor(Path({str(state)!r}), (Path({str(root)!r}),)).recover(
            {operation_id!r}, authorization=RunAuthorization(apply=True)
        )
        print(json.dumps(result.as_dict(), sort_keys=True))
        """,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    return json.loads(process.stdout.strip().splitlines()[-1])


def _retained_originals(root: Path, expected: bytes, excluded: set[Path]) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path not in excluded
        and path.read_bytes() == expected
    ]


@pytest.mark.parametrize(
    ("phase", "kind", "old_bytes"),
    [
        ("published", "replace", b"prior-bytes"),
        ("backed_up", "replace", b"prior-bytes"),
        ("group_committed", "replace", b"prior-bytes"),
        ("before_cleanup", "move", b"move-source-bytes"),
    ],
)
def test_real_process_crash_recovers_from_durable_checkpoint_idempotently(
    tmp_path: Path, phase: str, kind: str, old_bytes: bytes
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    target = root / "output.bin"
    if kind == "replace":
        target.write_bytes(old_bytes)
    else:
        (root / "source.bin").write_bytes(old_bytes)
    state = tmp_path / "state"
    operation_id = f"crash-{phase}"

    crashed = _crash_apply(
        tmp_path, root, state, operation_id, phase, kind
    )

    assert crashed.returncode == _CRASH_EXIT
    first_recovery = _recover(tmp_path, root, state, operation_id)
    expected_output = old_bytes if kind == "move" else b"committed-new-bytes"
    assert target.read_bytes() == expected_output
    if kind == "move":
        assert not (root / "source.bin").exists()
    retained = _retained_originals(root, old_bytes, {target, root / "source.bin"})
    assert len(retained) == 1
    target_after_recovery = fingerprint(target, root)

    second_recovery = _recover(tmp_path, root, state, operation_id)

    assert target.read_bytes() == expected_output
    assert fingerprint(target, root) == target_after_recovery
    assert _retained_originals(root, old_bytes, {target, root / "source.bin"}) == retained
    for recovery in (first_recovery, second_recovery):
        assert recovery["counts"]["failed"] == 0
        statuses = {decision["status"] for decision in recovery["decisions"]}
        assert not statuses & {
            "review_required",
            "stale_plan",
            "conflict",
            "publication_failed",
        }


def test_recovery_finishes_partial_group_without_republishing_committed_member(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    state = tmp_path / "state"
    first = root / "first.bin"
    second = root / "second.bin"
    crash = _run_python(
        tmp_path,
        f"""
        import os
        import sys
        from pathlib import Path
        from komga_cover_extractor.domain.decisions import RunAuthorization
        from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
        root, state = Path({str(root)!r}), Path({str(state)!r})
        plan = OperationPlan('partial-group-v1', 'partial-group', (
            Operation('first', 'publish', root / 'first.bin', artifact=ArtifactSpec(content=b'first-new')),
            Operation('second', 'publish', root / 'second.bin', artifact=ArtifactSpec(content=b'second-new')),
        ), (RootPermission(root),))
        def checkpoint(phase, member):
            if phase == 'published' and member == 'first':
                os._exit({_CRASH_EXIT})
        TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(
            plan, authorization=RunAuthorization(apply=True)
        )
        sys.exit(1)
        """,
    )

    assert crash.returncode == _CRASH_EXIT
    assert first.read_bytes() == b"first-new"
    assert not second.exists()
    first_fingerprint = fingerprint(first, root)

    result = _recover(tmp_path, root, state, "partial-group")

    assert first.read_bytes() == b"first-new"
    assert result["counts"]["failed"] == 0
    assert fingerprint(first, root) == first_fingerprint
    statuses = {decision["status"] for decision in result["decisions"]}
    assert not statuses & {
        "review_required",
        "stale_plan",
        "conflict",
        "publication_failed",
    }


def test_recovery_requires_review_when_published_target_changed_after_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    target = root / "output.bin"
    target.write_bytes(b"prior-bytes")
    state = tmp_path / "state"
    crashed = _crash_apply(
        tmp_path, root, state, "changed-target", "published", "replace"
    )
    assert crashed.returncode == _CRASH_EXIT

    target.write_bytes(b"external-writer-bytes")
    result = _recover(tmp_path, root, state, "changed-target")

    statuses = {decision["status"] for decision in result["decisions"]}
    assert "review_required" in statuses
    assert target.read_bytes() == b"external-writer-bytes"
    assert len(_retained_originals(root, b"prior-bytes", {target})) == 1


def test_recovery_requires_review_and_preserves_changed_move_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"original-source")
    state = tmp_path / "state"
    crashed = _crash_apply(
        tmp_path, root, state, "changed-source", "published", "move"
    )
    assert crashed.returncode == _CRASH_EXIT

    source.write_bytes(b"external-source-update")
    result = _recover(tmp_path, root, state, "changed-source")

    statuses = {decision["status"] for decision in result["decisions"]}
    assert "review_required" in statuses
    assert source.read_bytes() == b"external-source-update"
    assert (root / "output.bin").read_bytes() == b"original-source"


def test_unsupported_hard_link_fails_closed_without_clobber_or_source_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"source-remains")
    target = root / "target.bin"
    state = tmp_path / "state"
    observed = fingerprint(source, root)
    operation = Operation("publish-target", "move", target, source=observed)
    real_link = os.link

    def unsupported_link(source_path, destination_path, *args, **kwargs):
        if Path(destination_path).name == target.name:
            raise OSError(errno.EOPNOTSUPP, "hard links unsupported", str(destination_path))
        return real_link(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(transactions.os, "link", unsupported_link)
    result = TransactionExecutor(state, (root,)).apply(
        OperationPlan("failure-injection-v1", "no-hardlink", (operation,), (RootPermission(root),)),
        authorization=RunAuthorization(apply=True),
    )

    assert any(decision.status == "unsupported" for decision in result.decisions)
    assert not target.exists()
    assert source.read_bytes() == b"source-remains"


def test_file_fsync_failure_before_publication_leaves_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"source-original")
    observed = fingerprint(source, root)
    target = root / "target.bin"
    operation = Operation("move-source", "move", target, source=observed)
    real_fsync = os.fsync
    failed = False

    def fail_stage_file_fsync(fd: int) -> None:
        nonlocal failed
        info = os.fstat(fd)
        if (
            not failed
            and stat.S_ISREG(info.st_mode)
            and Path(f"/proc/self/fd/{fd}").read_bytes() == b"source-original"
        ):
            failed = True
            raise OSError(errno.EIO, "injected stage flush failure")
        real_fsync(fd)

    monkeypatch.setattr(transactions.os, "fsync", fail_stage_file_fsync)
    result = TransactionExecutor(tmp_path / "state", (root,)).apply(
        OperationPlan(
            "failure-injection-v1",
            "flush-before",
            (operation,),
            (RootPermission(root),),
        ),
        authorization=RunAuthorization(apply=True),
    )

    assert failed
    assert result.committed == 0
    assert any(
        decision.status in {"publication_failed", "review_required"}
        for decision in result.decisions
    )
    assert source.read_bytes() == b"source-original"
    assert not target.exists()


def test_directory_fsync_failure_after_link_preserves_published_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "source.bin"
    content = b"linked-but-not-durably-committed"
    source.write_bytes(content)
    observed = fingerprint(source, root)
    target = root / "target.bin"
    operation = Operation("move-source", "move", target, source=observed)
    real_link = os.link
    real_fsync = os.fsync
    published = False
    failed = False

    def record_link(source_path, destination_path, *args, **kwargs):
        nonlocal published
        result = real_link(source_path, destination_path, *args, **kwargs)
        if target.exists() and target.read_bytes() == content:
            published = True
        return result

    def fail_directory_fsync_after_publish(fd: int) -> None:
        nonlocal failed
        if published and not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError(errno.EIO, "injected parent directory flush failure")
        real_fsync(fd)

    monkeypatch.setattr(transactions.os, "link", record_link)
    monkeypatch.setattr(transactions.os, "fsync", fail_directory_fsync_after_publish)
    result = TransactionExecutor(tmp_path / "state", (root,)).apply(
        OperationPlan(
            "failure-injection-v1",
            "flush-after",
            (operation,),
            (RootPermission(root),),
        ),
        authorization=RunAuthorization(apply=True),
    )

    assert published and failed
    assert result.committed == 0
    assert any(
        decision.status in {"publication_failed", "review_required"}
        for decision in result.decisions
    )
    assert target.read_bytes() == content
    assert source.read_bytes() == content


def test_two_processes_cannot_publish_under_the_same_root_lock(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    state = tmp_path / "state"
    marker = tmp_path / "holder-staged"
    first_target = root / "first.bin"
    second_target = root / "second.bin"
    holder_code = f"""
        import json
        import sys
        from pathlib import Path
        from komga_cover_extractor.domain.decisions import RunAuthorization
        from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
        root, state = Path({str(root)!r}), Path({str(state)!r})
        operation = Operation('holder', 'publish', root / 'first.bin', artifact=ArtifactSpec(content=b'first'))
        plan = OperationPlan('lock-v1', 'lock-holder', (operation,), (RootPermission(root),))
        def checkpoint(phase, member):
            if phase == 'staged' and member == 'holder':
                Path({str(marker)!r}).write_text('ready', encoding='utf-8')
                sys.stdin.readline()
        result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(
            plan, authorization=RunAuthorization(apply=True)
        )
        print(json.dumps(result.as_dict(), sort_keys=True))
        """
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(holder_code)],
        cwd=tmp_path,
        env=_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "first process did not reach its staged checkpoint"

        contender = _run_python(
            tmp_path,
            f"""
            import json
            from pathlib import Path
            from komga_cover_extractor.domain.decisions import RunAuthorization
            from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
            from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
            root, state = Path({str(root)!r}), Path({str(state)!r})
            operation = Operation('contender', 'publish', root / 'second.bin', artifact=ArtifactSpec(content=b'second'))
            plan = OperationPlan('lock-v1', 'lock-contender', (operation,), (RootPermission(root),))
            result = TransactionExecutor(state, (root,)).apply(
                plan, authorization=RunAuthorization(apply=True)
            )
            print(json.dumps(result.as_dict(), sort_keys=True))
            """,
        )
        assert contender.returncode == 0, contender.stderr or contender.stdout
        contender_result = json.loads(contender.stdout.strip().splitlines()[-1])
        statuses = {decision["status"] for decision in contender_result["decisions"]}
        assert statuses & {"permission_denied", "conflict", "review_required"}
        assert not second_target.exists()
        assert not first_target.exists()
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("continue\n")
            holder.stdin.flush()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=10)
            pytest.fail("lock holder did not stop within the bounded cleanup timeout")
    assert holder.returncode == 0, (holder.stderr.read() if holder.stderr else "")
    assert first_target.read_bytes() == b"first"


def test_repeated_recovery_does_not_duplicate_local_commit_outbox_event(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    target = root / "published.bin"
    state = tmp_path / "state"
    operation = Operation(
        "published",
        "publish",
        target,
        artifact=ArtifactSpec(content=b"durably published bytes"),
    )
    plan = OperationPlan(
        "outbox-v1",
        "outbox-idempotence",
        (operation,),
        (RootPermission(root),),
    )
    executor = TransactionExecutor(state, (root,))

    result = executor.apply(plan, authorization=RunAuthorization(apply=True))
    before = executor.state_store.list_outbox(operation_id="outbox-idempotence")

    assert result.committed == 1
    assert target.read_bytes() == b"durably published bytes"
    assert len(before) == 1
    assert before[0].event_type == "local_commit"
    event_id = before[0].event_id
    published_identity = fingerprint(target, root)

    first_recovery = executor.recover(
        "outbox-idempotence", authorization=RunAuthorization(apply=True)
    )
    second_recovery = executor.recover(
        "outbox-idempotence", authorization=RunAuthorization(apply=True)
    )
    after = executor.state_store.list_outbox(operation_id="outbox-idempotence")

    assert first_recovery.failed == 0
    assert second_recovery.failed == 0
    assert first_recovery.committed == second_recovery.committed == 0
    preview = executor.recover("outbox-idempotence", authorization=RunAuthorization())
    assert preview.committed == 0 and preview.skipped == 1
    assert target.read_bytes() == b"durably published bytes"
    assert fingerprint(target, root) == published_identity
    assert len(after) == 1
    assert after[0].event_type == "local_commit"
    assert after[0].event_id == event_id

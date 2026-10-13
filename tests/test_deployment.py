from __future__ import annotations

import importlib.util
import json
import os
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
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
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker-entrypoint.sh"


def _tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("mask", "expected_mode"), [("022", 0o644), ("027", 0o640), ("0077", 0o600)])
def test_entrypoint_applies_runtime_mask_and_preserves_arguments(tmp_path: Path, mask: str, expected_mode: int) -> None:
    output = tmp_path / "created file"
    program = (
        "import json, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text('created'); "
        "print(json.dumps(sys.argv[2:]))"
    )
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), sys.executable, "-c", program, str(output), "a b", "*", "$PATH", ""],
        env={**os.environ, "UMASK": mask}, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["a b", "*", "$PATH", ""]
    assert output.read_text() == "created"
    assert stat.S_IMODE(output.stat().st_mode) == expected_mode


def test_entrypoint_defaults_to_022_without_mask_environment(tmp_path: Path) -> None:
    output = tmp_path / "default-mask"
    environ = dict(os.environ)
    environ.pop("UMASK", None)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('default')", str(output)],
        env=environ, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert output.read_text() == "default"
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


@pytest.mark.parametrize("mask", ["", "22", "008", "888", "1022", "027; touch forbidden", "symbolic"])
def test_invalid_runtime_mask_never_reaches_child(tmp_path: Path, mask: str) -> None:
    output = tmp_path / "forbidden"
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('forbidden')", str(output)],
        env={**os.environ, "UMASK": mask}, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "UMASK must" in result.stderr
    assert not output.exists()


def test_entrypoint_exec_preserves_child_exit_and_process_identity() -> None:
    process = subprocess.Popen(
        ["sh", str(ENTRYPOINT), sys.executable, "-u", "-c", "import os, signal; print(os.getpid(), flush=True); signal.pause()"],
        env={**os.environ, "UMASK": "022"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
            assert int(process.stdout.readline()) == process.pid
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGTERM
        assert stderr == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_requirements_publication_updates_both_consumers_and_check_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exporter = _tool("export_requirements")
    addon = tmp_path / "addons/qbit_torrent_unchecker"
    addon.mkdir(parents=True)
    snapshot = "# Generated locked fixture\nregex==2025.9.18\n"
    assert exporter.publish_snapshot(tmp_path, snapshot) == 0
    assert (tmp_path / "requirements.txt").read_text() == snapshot
    assert (addon / "requirements.txt").read_text() == snapshot
    (addon / "requirements.txt").write_text("stale\n")
    before = (addon / "requirements.txt").stat().st_mtime_ns
    assert exporter.publish_snapshot(tmp_path, snapshot, check=True) == 1
    assert (addon / "requirements.txt").read_text() == "stale\n"
    assert (addon / "requirements.txt").stat().st_mtime_ns == before
    assert "addons/qbit_torrent_unchecker/requirements.txt" in capsys.readouterr().err


def test_full_legacy_report_gate_accepts_known_failures_but_rejects_new_failure() -> None:
    verifier = _tool("verify_distribution")
    report = json.loads((ROOT / "docs/refactor/baseline-results.json").read_text(encoding="utf-8"))
    verifier.verify_legacy_report(report)
    original = next(row for row in report["tests"] if row["name"] == "test_remove_hidden_files")
    original["status"] = "fail"
    with pytest.raises(ValueError, match="known failures"):
        verifier.verify_legacy_report(report)


def test_full_legacy_report_gate_rejects_partial_corpus_and_changed_fixture() -> None:
    verifier = _tool("verify_distribution")
    baseline = json.loads((ROOT / "docs/refactor/baseline-results.json").read_text(encoding="utf-8"))
    baseline["corpus"]["processed_rows"] = 44713
    with pytest.raises(ValueError, match="corpus"):
        verifier.verify_legacy_report(baseline)
    baseline["corpus"]["processed_rows"] = 44714
    baseline["source_hashes"]["settings.py"] = "0" * 64
    with pytest.raises(ValueError, match="different original sources"):
        verifier.verify_legacy_report(baseline)


@pytest.fixture
def installed_commit_proof(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    library = tmp_path / "library"
    library.mkdir()
    source = library / "incoming.cbz"
    source.write_bytes(b"original book bytes")
    plan = OperationPlan("deployment-proof", "terminal-proof", (
        Operation("series", "mkdir", library / "Series"),
        Operation("book", "move", library / "Series/book.cbz",
                  source=fingerprint(source, library), depends_on=("series",)),
        Operation("art", "publish", library / "Series/art.bin",
                  artifact=ArtifactSpec(content=b"published artwork"), depends_on=("series",)),
    ), (RootPermission(library),))
    result = TransactionExecutor(tmp_path / "state", (library,)).apply(
        plan, authorization=RunAuthorization(apply=True),
    )
    assert (result.exit_code, result.committed, result.failed) == (0, 1, 0)
    assert (library / "Series/book.cbz").read_bytes() == b"original book bytes"
    assert (library / "Series/art.bin").read_bytes() == b"published artwork"
    assert not source.exists()
    retained = [path for path in library.rglob("*") if path.is_file()
                and path != library / "Series/book.cbz" and path.read_bytes() == b"original book bytes"]
    assert len(retained) == 1
    return _tool("verify_installed"), result.as_dict()


def test_installed_commit_proof_accepts_settled_publication_and_cleanup(
    tmp_path: Path, installed_commit_proof: tuple[Any, dict[str, Any]],
) -> None:
    verifier, payload = installed_commit_proof
    verifier._assert_commit(payload, tmp_path, 1)


@pytest.mark.parametrize(("member", "status"), [
    ("art", "planned"), ("art", "staging"), ("art", "staged"), ("art", "publishing"),
    ("art", "published"), ("art", "cleanup_pending"), ("art", "complete"),
    ("art", "needs_review"), ("art", "failed"), ("book", "committed"), ("series", "complete"),
])
def test_installed_commit_proof_rejects_inconsistent_terminal_members(
    tmp_path: Path, installed_commit_proof: tuple[Any, dict[str, Any]], member: str, status: str,
) -> None:
    verifier, payload = installed_commit_proof
    with sqlite3.connect(tmp_path / "state/state.sqlite3") as connection:
        connection.execute("UPDATE members SET status=? WHERE member_id=?", (status, member))
    with pytest.raises(ValueError):
        verifier._assert_commit(payload, tmp_path, 1)


def test_installed_commit_proof_requires_one_event_for_every_member(
    tmp_path: Path, installed_commit_proof: tuple[Any, dict[str, Any]],
) -> None:
    verifier, payload = installed_commit_proof
    with sqlite3.connect(tmp_path / "state/state.sqlite3") as connection:
        connection.execute("DELETE FROM outbox WHERE member_id='art'")
    with pytest.raises(ValueError):
        verifier._assert_commit(payload, tmp_path, 1)


def test_installed_repeat_manifest_ignores_only_settled_lock_and_directory_mtimes(
    tmp_path: Path, installed_commit_proof: tuple[Any, dict[str, Any]],
) -> None:
    verifier, _ = installed_commit_proof
    library = tmp_path / "library"
    strict_before = verifier._manifest(library)
    published_before = verifier._file_manifest(library)
    for path in (library, library / ".kce-transaction.lock"):
        observed = path.stat()
        os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000_000))
    assert verifier._manifest(library) != strict_before
    assert verifier._file_manifest(library) == published_before
    artwork = library / "Series/art.bin"
    observed = artwork.stat()
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"published artwork")
    os.utime(replacement, ns=(observed.st_atime_ns, observed.st_mtime_ns))
    os.replace(replacement, artwork)
    assert artwork.read_bytes() == b"published artwork"
    assert artwork.stat().st_mtime_ns == observed.st_mtime_ns
    assert verifier._file_manifest(library) != published_before


def test_installed_repeat_manifest_does_not_ignore_unsettled_lock_ownership(
    tmp_path: Path, installed_commit_proof: tuple[Any, dict[str, Any]],
) -> None:
    verifier, _ = installed_commit_proof
    library = tmp_path / "library"
    lock_path = library / ".kce-transaction.lock"
    lock = json.loads(lock_path.read_bytes())
    lock["owner"] = {"operation_id": "pending", "pid": os.getpid(), "process_start": "observed"}
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError, match="unsettled or invalid root lock"):
        verifier._file_manifest(library)

from __future__ import annotations

import errno
import json
import struct
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from komga_cover_extractor.application.library_workflow import (
    ArtworkWorkspace,
    release_artwork_workspace,
)
from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.config import Configuration, validate_config
from komga_cover_extractor.domain.decisions import RunAuthorization, RunResult
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.domain.releases import ReleaseSnapshot
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor


def _png(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes((red, green, blue)) * 16 for _ in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _book(path: Path, *, publisher: str = "Fixture Press", color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("01.png", _png(*color))
        archive.writestr("ComicInfo.xml", f"<ComicInfo><Publisher>{publisher}</Publisher><Year>2015</Year></ComicInfo>")
    return path.read_bytes()


def _config(tmp_path: Path, **overrides: object) -> Configuration:
    library, downloads = tmp_path / "library", tmp_path / "downloads"
    library.mkdir(exist_ok=True)
    downloads.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "paths": [str(library)], "download_folders": [str(downloads)],
        "state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs"),
        "correct_file_extensions_toggle": False, "convert_to_cbz_toggle": False,
        "delete_unacceptable_files_toggle": False, "delete_chapters_from_downloads_toggle": False,
        "rename_files_in_download_folders_toggle": False,
        "create_folders_for_items_in_download_folder_toggle": False,
        "check_for_duplicate_volumes_toggle": False, "check_for_existing_series_toggle": False,
        "rename_dirs_in_download_folder_toggle": False, "move_series_to_correct_library_toggle": False,
        "generate_release_group_list_toggle": False, "extract_covers_toggle": False,
        "check_for_missing_volumes_toggle": False, "bookwalker_check": False,
        "manual_rename": False, "manual_delete": False, "log_to_file": False,
        "match_through_image_similarity": False, "match_through_identifiers": False,
        "compare_detected_cover_to_blank_images": False, "required_similarity_score": 1.0,
        "discord_webhook_url": [], "bookwalker_webhook_urls": [], "new_volume_webhook": "",
        "send_scan_request_to_komga_libraries_toggle": False, "library_types": [],
    }
    values.update(overrides)
    return validate_config(values, base_dir=tmp_path)


def _capabilities() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("lxml")


def test_public_catalog_distinguishes_no_library_verified_empty_and_invalid_metadata(tmp_path: Path) -> None:
    _capabilities()
    config = _config(tmp_path)
    with open_pipeline(config) as pipeline:
        empty = pipeline.library_snapshot()
        assert empty.available and empty.releases == ()
        assert tuple(root.path for root in empty.roots) == (tmp_path / "library",)
    with open_pipeline(replace(config, paths=())) as pipeline:
        absent = pipeline.library_snapshot()
        assert not absent.available and absent.reason == "no_configured_library_roots"
    path = tmp_path / "library" / "Series" / "Series v01.cbz"
    original = _book(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", b"<ComicInfo><Publisher>malformed")
    corrupted = path.read_bytes()
    assert corrupted != original
    with open_pipeline(config) as pipeline:
        unavailable = pipeline.library_snapshot()
        result = pipeline.run(RunRequest("inspect"))
    assert not unavailable.available
    assert result.exit_code == 2 and result.committed == 0
    assert any(decision.status == "invalid_input" for decision in result.decisions)
    assert path.read_bytes() == corrupted
    assert not (tmp_path / "state").exists()


def test_single_book_inspection_does_not_open_unselected_corrupt_archives(tmp_path: Path) -> None:
    _capabilities()
    config = _config(tmp_path)
    selected = tmp_path / "library" / "Series" / "Series v01.cbz"
    original = _book(selected)
    other = tmp_path / "library" / "Series" / "Series v02.cbz"
    other.write_bytes(b"Not an archive and not evidence about the selected book.")
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest("inspect", books=(selected,)))
    assert result.exit_code == 0
    assert tuple(release.path for release in result.releases) == (selected,)
    assert ("Publisher", "Fixture Press") in result.releases[0].observation.metadata
    assert selected.read_bytes() == original
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(("suffix", "first_member", "page_count", "grayscale", "kind"), [
    (".png", None, 86, True, "chapter"),
    (".PNG", None, 86, True, "volume"),
    (".PNG", "000.txt", 86, True, "volume"),
    (".PNG", "000/", 86, True, "volume"),
    (".PNG", "000.txt", 85, False, "chapter"),
    (".PNG", None, 86, False, "volume"),
])
def test_inspected_archive_page_classification_keeps_raw_lexical_first_member_and_image_count(
    tmp_path: Path, suffix: str, first_member: str | None, page_count: int, grayscale: bool, kind: str,
) -> None:
    _capabilities()
    config = _config(tmp_path)
    path = tmp_path / "library" / "Series" / "Unnumbered Book.cbz"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(page_count, 0, -1):
            image = _png(128, 128, 128) if index == 1 and grayscale else _png(255, 0, 0)
            archive.writestr(f"{index:03}{suffix}", image)
        if first_member is not None:
            archive.writestr(first_member, b"" if first_member.endswith("/") else b"not an image")
    original = path.read_bytes()
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest("inspect", books=(path,)))
    assert (result.exit_code, result.committed, result.failed) == (0, 0, 0)
    assert len(result.releases) == 1
    assert result.releases[0].kind == kind
    assert result.releases[0].number.raw_tokens == ("1",)
    assert "archive:observed" in result.releases[0].provenance
    assert path.read_bytes() == original
    assert tuple(path.parent.iterdir()) == (path,)
    assert not config.state_dir.exists() and not config.log_dir.exists()


def test_nonbook_cleanup_is_planned_without_fabricating_a_release(tmp_path: Path) -> None:
    config = _config(tmp_path, delete_unacceptable_files_toggle=True, unacceptable_keywords=["Unwanted"])
    path = tmp_path / "downloads" / "Unwanted.txt"
    original = b"This is a text file, not a comic or novel container.\n"
    path.write_bytes(original)
    with open_pipeline(config) as pipeline:
        preview = pipeline.run(RunRequest("run"))
    assert preview.exit_code == 0 and preview.planned == 1
    assert preview.releases == () and path.read_bytes() == original
    assert not (tmp_path / "state").exists()
    with open_pipeline(config) as pipeline:
        applied = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))
    assert applied.committed == 1 and applied.releases == ()
    assert not path.exists()
    assert any(path.read_bytes() == original for path in (tmp_path / "downloads").rglob("source"))


def test_retiring_the_only_release_does_not_resurrect_the_original_catalog(tmp_path: Path) -> None:
    config = _config(tmp_path, chapter_support_toggle=True, delete_chapters_from_downloads_toggle=True)
    source = tmp_path / "downloads" / "Series c000.cbz"
    original = _book(source)
    with open_pipeline(config) as pipeline:
        preview = pipeline.run(RunRequest("run"))
    assert preview.exit_code == 0 and preview.releases == ()
    assert preview.planned == 1 and source.read_bytes() == original
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))
    assert result.exit_code == 0 and result.releases == () and result.committed == 1
    assert not source.exists()


def test_committed_delivery_receives_observed_prechange_metadata_and_current_paths(tmp_path: Path) -> None:
    _capabilities()
    config = _config(
        tmp_path, rename_files_in_download_folders_toggle=True,
        check_for_existing_series_toggle=True, restructure_when_renaming=False,
        ranked_keywords=[{"name": "Digital", "score": 3, "file_type": "both"}],
    )
    existing = tmp_path / "library" / "Series" / "Series v01 (Scan).cbz"
    incoming = tmp_path / "downloads" / "Incoming" / "Series Volume 1 (Digital).cbz"
    prior_bytes = _book(existing, publisher="Prior Press")
    incoming_bytes = _book(incoming, publisher="Incoming Press", color=(0, 255, 0))
    observed: list[tuple[ReleaseSnapshot, ...]] = []
    with open_pipeline(config) as pipeline:
        finish = pipeline.after_commit
        assert finish is not None

        def receive(result: RunResult, request: RunRequest, before: tuple[ReleaseSnapshot, ...]) -> RunResult:
            observed.append(before)
            return finish(result, request, before)

        pipeline.after_commit = receive
        result = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))
    target = existing.parent / "Series v01 (Digital).cbz"
    assert result.committed == 1 and result.failed == 0
    assert target.read_bytes() == incoming_bytes
    assert tuple(release.path for release in result.releases) == (target,)
    assert len(observed) == 1
    before = {release.path: release for release in observed[0]}
    assert ("Publisher", "Prior Press") in before[existing].observation.metadata
    assert ("Publisher", "Incoming Press") in before[incoming].observation.metadata
    assert not existing.exists() and not incoming.exists()
    assert any(path.read_bytes() == prior_bytes for path in (tmp_path / "library").rglob("source"))


def test_owned_artwork_stage_survives_failure_then_cleans_after_recovery_and_repeat(tmp_path: Path) -> None:
    _capabilities()
    config = _config(tmp_path)
    root = tmp_path / "library"
    book = root / "Series" / "Series v01.cbz"
    _book(book)
    source = fingerprint(book, root)
    destination = book.with_suffix(".png")
    old, new = _png(255, 0, 0), _png(0, 255, 0)
    destination.write_bytes(old)
    prior = fingerprint(destination, root)
    authorization = RunAuthorization(apply=True)
    operation_id = "artwork-stage-recovery-fixture"

    def fail(event: str, member: str) -> None:
        if event == "staged":
            raise OSError(errno.EIO, "Injected staging failure")

    with ArtworkWorkspace(root=root, operation_id=operation_id, authorization=authorization,
                          max_bytes=1024 * 1024) as workspace:
        artifact = workspace.spill(ArtifactSpec(content=new, media_type="image/png"))
        plan = OperationPlan("fixture-policy", operation_id, (
            Operation("cover", "publish", destination, source=source, destination_before=prior,
                      collision="replace", artifact=artifact, reason="book_artwork"),
        ), (RootPermission(root),), "book_artwork")
        workspace.record(plan)
        workspace.retained = True
        failed = TransactionExecutor(tmp_path / "state", (root,), checkpoint=fail).apply(plan, authorization=authorization)
        stage_directory = workspace.path
    assert failed.exit_code != 0 and failed.committed == 0
    assert destination.read_bytes() == old
    assert stage_directory is not None and stage_directory.exists()
    with open_pipeline(config) as pipeline:
        recovered = pipeline.run(RunRequest("recover", operation_id=operation_id, authorization=authorization))
    assert recovered.committed == 1 and destination.read_bytes() == new
    assert not stage_directory.exists()
    published = fingerprint(destination, root)
    with open_pipeline(config) as pipeline:
        repeated = pipeline.run(RunRequest("recover", operation_id=operation_id, authorization=authorization))
    assert repeated.exit_code == 0
    assert any(decision.reason == "already_complete" for decision in repeated.decisions)
    assert fingerprint(destination, root) == published


@pytest.mark.parametrize("tamper", ["marker", "unrecorded_file"])
def test_workspace_cleanup_refuses_changed_ownership_or_unrecorded_contents(tmp_path: Path, tamper: str) -> None:
    _capabilities()
    root = tmp_path / "library"
    root.mkdir()
    book = root / "Series v01.cbz"
    _book(book)
    authorization = RunAuthorization(apply=True)
    operation_id = "artwork-workspace-ownership-fixture"
    with ArtworkWorkspace(root=root, operation_id=operation_id, authorization=authorization,
                          max_bytes=1024 * 1024) as workspace:
        artifact = workspace.spill(ArtifactSpec(content=_png(0, 255, 0), media_type="image/png"))
        plan = OperationPlan("fixture-policy", operation_id, (
            Operation("cover", "publish", book.with_suffix(".png"), source=fingerprint(book, root), artifact=artifact),
        ), (RootPermission(root),))
        workspace.record(plan)
        result = TransactionExecutor(tmp_path / "state", (root,)).apply(plan, authorization=authorization)
        assert result.committed == 1
        assert workspace.path is not None and artifact.source is not None
        workspace.retained = True
        if tamper == "marker":
            marker = workspace.path / ".owner.json"
            value = json.loads(marker.read_text(encoding="utf-8"))
            value["operation_id"] = "another-operation"
            marker.write_text(json.dumps(value), encoding="utf-8")
        else:
            (workspace.path / "user-note.txt").write_bytes(b"Not owned by the operation.")
        decision = release_artwork_workspace(plan, state_dir=tmp_path / "state", authorization=authorization)
        assert decision is not None and decision.status == "review_required"
        assert workspace.path.exists() and artifact.source.path.exists()
        assert book.with_suffix(".png").read_bytes() == _png(0, 255, 0)


def test_watch_root_inference_is_scoped_and_preserves_explicit_restrictions(tmp_path: Path) -> None:
    _capabilities()
    from komga_cover_extractor.domain.libraries import LibraryRoot
    config = _config(
        tmp_path, chapter_support_toggle=True, auto_classify_watchdog_paths=True,
        check_for_existing_series_toggle=True,
    )
    chapter = tmp_path / "library" / "Series" / "Series c000.cbz"
    _book(chapter)
    with open_pipeline(config) as pipeline:
        ordinary = pipeline.run(RunRequest("run"))
        watched = pipeline.run(RunRequest("run", watch=True))
        restricted = pipeline.run(RunRequest("run", watch=True, roots=(
            LibraryRoot(tmp_path / "library", formats=("chapter",)),
            LibraryRoot(tmp_path / "downloads", roles=("download",)),
        )))
    assert not any(decision.reason == "watch_root_policy_inferred" for decision in ordinary.decisions)
    inferred = next(decision for decision in watched.decisions if decision.reason == "watch_root_policy_inferred")
    assert inferred.path == tmp_path / "library"
    assert "formats=chapter" in inferred.candidates and "sample_size=1" in inferred.candidates
    assert not any(decision.reason == "watch_root_policy_inferred" for decision in restricted.decisions)
    assert chapter.exists() and not (tmp_path / "state").exists()


def test_verified_empty_watch_library_is_not_an_inferred_match(tmp_path: Path) -> None:
    config = _config(tmp_path, auto_classify_watchdog_paths=True, check_for_existing_series_toggle=True)
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest("run", watch=True))
        catalog = pipeline.library_snapshot()
    assert result.exit_code == 0 and catalog.available and catalog.releases == ()
    assert any(decision.status == "no_match" and decision.reason == "empty_input"
               and decision.path == tmp_path / "library" for decision in result.decisions)
    assert not (tmp_path / "state").exists()


def test_observed_cover_absence_cannot_match_at_a_zero_threshold(tmp_path: Path) -> None:
    _capabilities()
    from komga_cover_extractor.application.library_workflow import make_cover_similarity
    from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
    from komga_cover_extractor.domain.releases import FileObservation
    from komga_cover_extractor.matching.series import match_series
    from komga_cover_extractor.parsing.filenames import parse_release
    config = _config(tmp_path, required_image_similarity_score=0.0, required_matching_percentage=0)
    root = tmp_path / "library"
    paths = (root / "Series One" / "Series One v01.cbz", root / "Series Two" / "Series Two v01.cbz")
    for path in paths:
        path.parent.mkdir()
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ComicInfo.xml", "<ComicInfo><Year>2015</Year></ComicInfo>")
    with open_pipeline(config) as pipeline:
        releases = tuple(parse_release(FileObservation(
            path, root_role="library", fingerprint=fingerprint(path, root),
        ), pipeline.parsing) for path in paths)
        policy = replace(pipeline.matching, match_through_image_similarity=True, match_through_identifiers=False)
        compare = make_cover_similarity(plans=(), policy=pipeline.covers, matching=policy, cancel=pipeline.cancel)
        assert compare(releases[0], releases[1]) == -1.0
        result = match_series(releases[0], LibrarySnapshot((releases[1],), (LibraryRoot(root),)), policy,
                              cover_similarity=compare)
    assert result.status == "no_match"
    assert not any(path.suffix in {".jpg", ".png", ".webp"} for path in root.rglob("*"))
    assert not (tmp_path / "state").exists()


def test_applied_unchanged_run_keeps_current_observed_metadata_without_creating_state(tmp_path: Path) -> None:
    _capabilities()
    config = _config(tmp_path)
    book = tmp_path / "library" / "Series" / "Series v01.cbz"
    original = _book(book)
    observed = fingerprint(book, tmp_path / "library")
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))
    assert result.committed == 0 and result.planned == 0
    assert len(result.releases) == 1
    assert ("Publisher", "Fixture Press") in result.releases[0].observation.metadata
    assert result.releases[0].observation.fingerprint == observed
    assert book.read_bytes() == original and not (tmp_path / "state").exists()


@pytest.mark.parametrize("command", ["organize", "run"])
@pytest.mark.parametrize(("fault", "status", "reason", "exit_code"), [
    ("missing", "stale_plan", "observed_input_disappeared", 3),
    ("changed", "stale_plan", "source_changed", 3),
    ("denied", "permission_denied", "filesystem_observation_denied", 1),
    ("error", "failed", "filesystem_observation_failed", 1),
])
def test_committed_organization_reports_survive_refresh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
    fault: str, status: str, reason: str, exit_code: int,
) -> None:
    _capabilities()
    from komga_cover_extractor.application import organize as organizing
    from komga_cover_extractor.application import pipeline as scheduling
    from komga_cover_extractor.infrastructure.filesystem import FilesystemError
    from komga_cover_extractor.infrastructure.state import StateStore
    config = _config(tmp_path, rename_files_in_download_folders_toggle=True)
    source = tmp_path / "downloads" / "Series Volume 1.cbz"
    original = _book(source)
    target = source.with_name("Series v01.cbz")
    settled = False
    applied: list[str] = []
    original_apply = TransactionExecutor.apply
    original_fingerprint = fingerprint

    def apply_then_mark(self, plan, *, authorization):
        nonlocal settled
        result = original_apply(self, plan, authorization=authorization)
        applied.append(plan.operation_id)
        settled |= bool(result.committed)
        return result

    def unavailable_observation(path, root):
        if settled and path == target:
            if fault == "missing":
                raise FileNotFoundError(errno.ENOENT, "Injected post-commit disappearance", str(path))
            if fault == "changed":
                raise FilesystemError("source_changed", path)
            if fault == "denied":
                raise PermissionError(errno.EACCES, "Injected post-commit observation denial", str(path))
            raise OSError(errno.EIO, "Injected post-commit observation failure", str(path))
        return original_fingerprint(path, root)

    monkeypatch.setattr(TransactionExecutor, "apply", apply_then_mark)
    monkeypatch.setattr(organizing, "fingerprint", unavailable_observation)
    monkeypatch.setattr(scheduling, "fingerprint", unavailable_observation)
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest(command, books=(source,), authorization=RunAuthorization(apply=True)))
    assert (result.exit_code, result.committed, result.planned) == (exit_code, 1, 1)
    assert [(decision.status, decision.reason, decision.path) for decision in result.decisions
            if decision.path == target and decision.status == status] == [(status, reason, target)]
    assert len(applied) == len(result.plan_reports) == 1
    assert result.plan_reports[0].operation_id == applied[0]
    assert target.read_bytes() == original and not source.exists()
    store = StateStore(tmp_path / "state")
    record = store.get_operation(applied[0])
    assert record is not None and record.phase in {"committed", "cleanup_pending", "complete"}
    retained = [member.cleanup_path for member in store.list_members(applied[0])
                if member.source is not None and member.source.path == source]
    assert len(retained) == 1 and retained[0] is not None
    assert retained[0].read_bytes() == original


@pytest.mark.parametrize("command", ["organize", "run"])
def test_no_cover_is_a_completed_nonmatch_for_each_organizer_command(tmp_path: Path, command: str) -> None:
    _capabilities()
    config = _config(
        tmp_path, check_for_existing_series_toggle=True, match_through_image_similarity=True,
        required_image_similarity_score=0.0, required_matching_percentage=0, required_similarity_score=1.0,
    )
    incoming = tmp_path / "downloads" / "Incoming" / "Rareword One v01.cbz"
    existing = tmp_path / "library" / "Rareword Two" / "Rareword Two v01.cbz"
    for path in (incoming, existing):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ComicInfo.xml", "<ComicInfo><Year>2015</Year></ComicInfo>")
    originals = {path: path.read_bytes() for path in (incoming, existing)}
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest(command, books=(incoming,), authorization=RunAuthorization(apply=True)))
    assert (result.exit_code, result.planned, result.committed) == (0, 0, 0)
    assert any(decision.status == "no_match" and decision.path == incoming for decision in result.decisions)
    assert {path: path.read_bytes() for path in originals} == originals
    assert not (tmp_path / "state").exists() and not (tmp_path / "logs").exists()
    assert not any(path.suffix in {".jpg", ".png", ".webp"} for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("command", ["organize", "run"])
@pytest.mark.parametrize(("status", "reason", "exit_code"), [
    ("missing_dependency", "comparison_decoder_unavailable", 2),
    ("failed", "comparison_cover_read_failed", 1),
])
def test_unavailable_cover_evidence_is_not_a_completed_nonmatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
    status: str, reason: str, exit_code: int,
) -> None:
    _capabilities()
    from komga_cover_extractor.application import library_workflow as observations
    from komga_cover_extractor.application.extract_covers import CoverResult
    config = _config(
        tmp_path, check_for_existing_series_toggle=True, match_through_image_similarity=True,
        required_image_similarity_score=0.0, required_matching_percentage=0, required_similarity_score=1.0,
    )
    incoming = tmp_path / "downloads" / "Incoming" / "Rareword One v01.cbz"
    existing = tmp_path / "library" / "Rareword Two" / "Rareword Two v01.cbz"
    originals = {incoming: _book(incoming), existing: _book(existing)}

    def unavailable_cover(*args, **kwargs):
        return CoverResult(status, reason)

    monkeypatch.setattr(observations, "extract_cover", unavailable_cover)
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest(command, books=(incoming,), authorization=RunAuthorization(apply=True)))
    assert (result.exit_code, result.planned, result.committed) == (exit_code, 0, 0)
    assert any((decision.status, decision.reason) == (status, reason) for decision in result.decisions)
    assert {path: path.read_bytes() for path in originals} == originals
    assert not (tmp_path / "state").exists() and not (tmp_path / "logs").exists()
    assert not incoming.with_suffix(".png").exists() and not existing.with_suffix(".png").exists()


@pytest.mark.parametrize("changed_source", ["original", "staged"])
def test_cached_cover_absence_revalidates_original_and_staged_book_sources(
    tmp_path: Path, changed_source: str,
) -> None:
    _capabilities()
    import os

    from komga_cover_extractor.application.library_workflow import make_cover_similarity
    from komga_cover_extractor.application.planning import project_release
    from komga_cover_extractor.domain.releases import FileObservation
    from komga_cover_extractor.infrastructure.workers import WorkerError
    from komga_cover_extractor.parsing.filenames import parse_release
    config = _config(tmp_path, required_image_similarity_score=0.0)
    root, scratch = tmp_path / "downloads", tmp_path / "scratch"
    scratch.mkdir()
    originals = tuple(root / name for name in ("Series One v01.cbr", "Series Two v01.cbr"))
    stages = tuple(scratch / name for name in ("first.cbz", "second.cbz"))
    for path in originals:
        path.write_bytes(b"Original archive bytes retained outside the staged CBZ.")
    for path in stages:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ComicInfo.xml", "<ComicInfo><Year>2015</Year></ComicInfo>")
    original_bytes = {path: path.read_bytes() for path in originals}
    with open_pipeline(config) as pipeline:
        releases = tuple(parse_release(FileObservation(
            path, root_role="download", fingerprint=fingerprint(path, root),
        ), pipeline.parsing) for path in originals)
        projected = tuple(project_release(release, release.path.with_suffix(".cbz"),
                                          parsing=pipeline.parsing, roots=pipeline.roots) for release in releases)
        plan = OperationPlan("fixture-policy", "fixture-staged-book-bytes", tuple(
            Operation(f"book-{index}", "publish", release.path, source=release.observation.fingerprint,
                      artifact=ArtifactSpec(source=fingerprint(stage, scratch)))
            for index, (release, stage) in enumerate(zip(projected, stages, strict=True))
        ), (RootPermission(root), RootPermission(scratch, write=False)))
        compare = make_cover_similarity(plans=(plan,), policy=pipeline.covers, matching=pipeline.matching, cancel=pipeline.cancel)
        assert compare(*projected) == -1.0
        changed = originals[0] if changed_source == "original" else stages[0]
        info = changed.stat()
        content = changed.read_bytes()
        changed.write_bytes(content[:-1] + bytes((content[-1] ^ 1,)))
        os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert (changed.stat().st_size, changed.stat().st_mtime_ns) == (info.st_size, info.st_mtime_ns)
        with pytest.raises(WorkerError) as failure:
            compare(*projected)
    assert (failure.value.status, failure.value.reason) == ("stale_plan", "book_byte_source_changed")
    assert originals[1].read_bytes() == original_bytes[originals[1]]
    if changed_source == "staged":
        assert originals[0].read_bytes() == original_bytes[originals[0]]
    assert not any(path.with_suffix(".cbz").exists() for path in originals)
    assert not (tmp_path / "state").exists()


def test_cover_absence_cache_expires_before_unavailable_evidence_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capabilities()
    from komga_cover_extractor.application import library_workflow as observations
    from komga_cover_extractor.application.extract_covers import CoverResult
    from komga_cover_extractor.infrastructure.workers import WorkerError
    config = _config(tmp_path, required_image_similarity_score=0.0)
    root = tmp_path / "library"
    paths = (root / "Series One v01.cbz", root / "Series Two v01.cbz")
    for path in paths:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ComicInfo.xml", "<ComicInfo><Year>2015</Year></ComicInfo>")
    originals = {path: path.read_bytes() for path in paths}
    clock = [1000.0]
    monkeypatch.setattr(observations, "monotonic", lambda: clock[0])
    with open_pipeline(config) as pipeline:
        inspected = pipeline.run(RunRequest("inspect", books=paths))
        assert inspected.exit_code == 0 and len(inspected.releases) == 2
        compare = observations.make_cover_similarity(
            plans=(), policy=pipeline.covers, matching=pipeline.matching, cancel=pipeline.cancel,
        )
        assert compare(*inspected.releases) == -1.0

        def unavailable_cover(*args, **kwargs):
            return CoverResult("missing_dependency", "comparison_decoder_unavailable")

        monkeypatch.setattr(observations, "extract_cover", unavailable_cover)
        clock[0] = 1299.99
        assert compare(*inspected.releases) == -1.0
        clock[0] = 1300.0
        with pytest.raises(WorkerError) as failure:
            compare(*inspected.releases)
    assert (failure.value.status, failure.value.reason) == ("missing_dependency", "comparison_decoder_unavailable")
    assert {path: path.read_bytes() for path in originals} == originals
    assert not any(path.suffix in {".jpg", ".png", ".webp"} for path in root.rglob("*"))
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("threshold", [0.9, 1.0])
def test_full_run_does_not_republish_prior_group_projection(tmp_path: Path, threshold: float) -> None:
    _capabilities()
    config = _config(tmp_path, required_similarity_score=threshold, required_matching_percentage=75,
                     rename_files_in_download_folders_toggle=True, check_for_existing_series_toggle=True)
    existing = tmp_path / "library/Chronicle/Chronicle v01.cbz"
    existing_bytes = _book(existing)
    incoming = tmp_path / "downloads/Incoming/Chroniclex Volume 2 (Digital).cbz"
    original = _book(incoming)
    target_parent = existing.parent if threshold < 1 else incoming.parent
    target = target_parent / "Chroniclex v02 (Digital).cbz"
    with open_pipeline(config) as pipeline:
        preview = pipeline.run(RunRequest("run"))
        assert (preview.exit_code, preview.planned, preview.committed) == (0, 1, 0)
        assert {release.path for release in preview.releases} == {existing, target}
        assert incoming.read_bytes() == original and existing.read_bytes() == existing_bytes
        applied = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))
        assert (applied.exit_code, applied.committed, applied.failed) == (0, 1, 0)
        observed = fingerprint(target, next(root.path for root in pipeline.roots if root.contains(target)))
        repeated = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))
        assert (repeated.exit_code, repeated.planned, repeated.committed) == (0, 0, 0)
        assert fingerprint(target, observed.root) == observed
    assert target.read_bytes() == original and existing.read_bytes() == existing_bytes
    assert not incoming.exists()


def test_selected_full_run_artwork_does_not_depend_on_unrelated_metadata(tmp_path: Path) -> None:
    _capabilities()
    config = _config(tmp_path, extract_covers_toggle=True, output_covers_as_webp=False,
                     compress_image_option=False)
    selected = tmp_path / "library/Atlas/Atlas v01.cbz"
    original = _book(selected)
    unrelated = tmp_path / "library/Boreal/Boreal v01.cbz"
    unrelated.parent.mkdir()
    with zipfile.ZipFile(unrelated, "w") as archive:
        archive.writestr("01.png", _png(0, 255, 0))
        archive.writestr("ComicInfo.xml", b"<ComicInfo><Publisher>malformed")
    invalid = unrelated.read_bytes()
    with open_pipeline(config) as pipeline:
        outcome = pipeline.run(RunRequest("run", books=(selected,),
                                          authorization=RunAuthorization(apply=True)))
    assert (outcome.exit_code, outcome.committed, outcome.failed) == (0, 1, 0)
    from PIL import Image
    for path in (selected.with_suffix(".png"), selected.parent / "cover.png"):
        with Image.open(path) as image:
            assert image.format == "PNG" and image.convert("RGB").getpixel((8, 8)) == (255, 0, 0)
    assert selected.read_bytes() == original and unrelated.read_bytes() == invalid
    assert not unrelated.with_suffix(".png").exists()
    assert not (unrelated.parent / "cover.png").exists()


def test_required_matching_catalog_metadata_is_still_observed(tmp_path: Path) -> None:
    _capabilities()
    config = _config(tmp_path, check_for_existing_series_toggle=True,
                     extract_covers_toggle=True)
    incoming = tmp_path / "downloads/Atlas/Atlas v01.cbz"
    original = _book(incoming)
    unrelated = tmp_path / "library/Boreal/Boreal v01.cbz"
    unrelated.parent.mkdir()
    with zipfile.ZipFile(unrelated, "w") as archive:
        archive.writestr("01.png", _png(0, 255, 0))
        archive.writestr("ComicInfo.xml", b"<ComicInfo><Publisher>malformed")
    invalid = unrelated.read_bytes()
    with open_pipeline(config) as pipeline:
        outcome = pipeline.run(RunRequest("run", books=(incoming,),
                                          authorization=RunAuthorization(apply=True)))
    assert outcome.exit_code != 0 and outcome.committed == 0
    assert any(decision.path == unrelated and decision.status == "invalid_input" for decision in outcome.decisions)
    assert incoming.read_bytes() == original and unrelated.read_bytes() == invalid
    assert not incoming.with_suffix(".png").exists()
    assert not (tmp_path / "state").exists()

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
import tomli_w

from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.cli import main
from komga_cover_extractor.config import validate_config
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.infrastructure.state import StateStore

NATIVE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def _settings(tmp_path: Path, **overrides: object) -> dict[str, object]:
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
        "watchdog_toggle": False, "match_through_image_similarity": False,
        "match_through_identifiers": False, "compare_detected_cover_to_blank_images": False,
        "discord_webhook_url": [], "bookwalker_webhook_urls": [], "new_volume_webhook": "",
        "send_scan_request_to_komga_libraries_toggle": False, "library_types": [],
        "conversion_timeout_seconds": 30,
    }
    values.update(overrides)
    return values


def _zip(path: Path, page: bytes = b"synthetic nested archive asset\n") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pages/01.txt", page)
        archive.writestr("ComicInfo.xml", b"<ComicInfo/>")
        archive.comment = b"Synthetic preprocessing fixture."
    return path.read_bytes()


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _assert_retained(state: Path, operation_id: str, source: Path, original: bytes) -> None:
    store = StateStore(state)
    record = store.get_operation(operation_id)
    assert record is not None and record.phase == "complete"
    retained = tuple(
        member for member in store.list_members(operation_id)
        if member.source is not None and member.source.path == source and member.cleanup_path is not None
    )
    assert len(retained) == 1
    member = retained[0]
    assert member.status == "complete"
    assert member.cleanup_path is not None
    assert member.cleanup_path.read_bytes() == original


def test_full_run_conversion_renames_download_zip_without_header_correction(tmp_path: Path) -> None:
    pytest.importorskip("lxml", reason="full-run archive observations require the covers extra")
    config = validate_config(_settings(
        tmp_path, convert_to_cbz_toggle=True,
        correct_file_extensions_toggle=False, rename_zip_to_cbz=True,
    ), base_dir=tmp_path)
    source = tmp_path / "downloads" / "Downloaded v01.zip"
    original = _zip(source)
    target = source.with_suffix(".cbz")
    before = _tree(tmp_path / "downloads")

    with open_pipeline(config) as pipeline:
        preview = pipeline.run(RunRequest("run"))

    assert (preview.exit_code, preview.planned, preview.committed) == (0, 1, 0), preview.as_dict()
    assert tuple(release.path for release in preview.releases) == (target,)
    assert _tree(tmp_path / "downloads") == before
    assert source.read_bytes() == original and not target.exists()
    assert not config.state_dir.exists() and not config.log_dir.exists()

    with open_pipeline(config) as pipeline:
        applied = pipeline.run(RunRequest("run", authorization=RunAuthorization(apply=True)))

    assert (applied.exit_code, applied.committed, applied.failed) == (0, 1, 0), applied.as_dict()
    assert tuple(release.path for release in applied.releases) == (target,)
    assert target.read_bytes() == original and not source.exists()
    _assert_retained(config.state_dir, applied.as_dict()["plans"][0]["operation_id"], source, original)


@pytest.mark.parametrize(("rename_zip", "collision", "expected_exit", "expected_planned", "expected_committed"), [
    (False, False, 0, 0, 0),
    (True, False, 0, 1, 1),
    (True, True, 3, 0, 0),
])
def test_focused_convert_owns_zip_rename_and_preserves_disabled_or_colliding_inputs(
    tmp_path: Path, rename_zip: bool, collision: bool,
    expected_exit: int, expected_planned: int, expected_committed: int,
) -> None:
    pytest.importorskip("lxml", reason="committed conversion observations require the covers extra")
    config = validate_config(_settings(
        tmp_path, convert_to_cbz_toggle=False,
        correct_file_extensions_toggle=True, rename_zip_to_cbz=rename_zip,
    ), base_dir=tmp_path)
    source = tmp_path / "library" / "Focused v01.zip"
    original = _zip(source)
    target = source.with_suffix(".cbz")
    prior = _zip(target, b"different existing destination\n") if collision else None
    before = _tree(tmp_path / "library")

    with open_pipeline(config) as pipeline:
        preview = pipeline.run(RunRequest("convert", books=(source,)))

    assert (preview.exit_code, preview.planned, preview.committed) == (expected_exit, expected_planned, 0), preview.as_dict()
    assert _tree(tmp_path / "library") == before
    assert source.read_bytes() == original
    assert not config.state_dir.exists() and not config.log_dir.exists()

    with open_pipeline(config) as pipeline:
        applied = pipeline.run(RunRequest("convert", books=(source,), authorization=RunAuthorization(apply=True)))

    assert (applied.exit_code, applied.committed, applied.failed) == (expected_exit, expected_committed, 0), applied.as_dict()
    if expected_committed:
        assert target.read_bytes() == original and not source.exists()
        assert tuple(release.path for release in applied.releases) == (target,)
        _assert_retained(config.state_dir, applied.as_dict()["plans"][0]["operation_id"], source, original)
    else:
        assert _tree(tmp_path / "library") == before
        assert source.read_bytes() == original
        assert not config.state_dir.exists() and not config.log_dir.exists()
        if collision:
            assert target.read_bytes() == prior
            assert any(decision.status == "conflict" and decision.path == target for decision in applied.decisions)
        else:
            assert not target.exists()
            assert any(decision.status == "skipped" for decision in applied.decisions)


@pytest.mark.parametrize("container", ["zip", "rar"])
def test_legacy_full_run_preprocesses_downloads_but_not_library_only_archives(
    tmp_path: Path, container: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("lxml", reason="full-run archive observations require the covers extra")
    if container == "rar":
        pytest.importorskip("rarfile", reason="real RAR preprocessing requires the conversion extra")
        if shutil.which("unrar") is None:
            pytest.skip("Actual native RAR preprocessing requires licensed unrar")
    for name in tuple(os.environ):
        if name.startswith("KCE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = _settings(
        tmp_path, correct_file_extensions_toggle=True,
        convert_to_cbz_toggle=True, rename_zip_to_cbz=True,
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(tomli_w.dumps(settings), encoding="utf-8")
    library, downloads = tmp_path / "library", tmp_path / "downloads"
    library_book = library / ("Library v01.cbr" if container == "zip" else "Library v01.cbz")
    download_book = downloads / "Downloaded v01.cbr"
    if container == "zip":
        library_bytes = _zip(library_book)
        download_bytes = _zip(download_book)
    else:
        fixture = NATIVE_FIXTURES / "rar5-subdirs.rar"
        shutil.copyfile(fixture, library_book)
        shutil.copyfile(fixture, download_book)
        library_bytes, download_bytes = library_book.read_bytes(), download_book.read_bytes()
    target = download_book.with_suffix(".cbz")
    before_library, before_downloads = _tree(library), _tree(downloads)
    arguments = ["legacy", "--config", str(config_path), "-p", str(library), "-df", str(downloads)]

    preview_status = main(arguments)
    preview = json.loads(capsys.readouterr().out)

    assert preview_status == 0, preview
    assert preview["counts"]["planned"] == 1 and preview["counts"]["committed"] == 0
    assert _tree(library) == before_library and _tree(downloads) == before_downloads
    assert library_book.read_bytes() == library_bytes and download_book.read_bytes() == download_bytes
    assert not target.exists()
    assert not (tmp_path / "state").exists() and not (tmp_path / "logs").exists()
    for plan in preview["plans"]:
        for operation in plan["operations"]:
            assert Path(operation["destination"]).is_relative_to(downloads)

    applied_status = main([*arguments, "--apply"])
    applied = json.loads(capsys.readouterr().out)

    assert applied_status == 0, applied
    assert applied["counts"]["committed"] == 1 and applied["counts"]["failed"] == 0
    assert _tree(library) == before_library and library_book.read_bytes() == library_bytes
    assert not library_book.with_suffix(".cbz" if container == "zip" else ".cbr").exists()
    assert not download_book.exists()
    if container == "zip":
        assert target.read_bytes() == download_bytes
    else:
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None
            assert archive.read("sub/dir1/file1.txt") == b"file1\n"
            assert archive.read("sub/dir2/file2.txt") == b"file2\n"
            assert "sub/empty/" in archive.namelist()
    _assert_retained(tmp_path / "state", applied["plans"][0]["operation_id"], download_book, download_bytes)

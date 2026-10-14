from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import tomli_w


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert not tmp_path.resolve().is_relative_to(Path(__file__).resolve().parents[2])
    for name in ("home", "library", "downloads"):
        (tmp_path / name).mkdir()
    return tmp_path


def _configure(workspace: Path, **overrides: Any) -> None:
    values = {
        "paths": [str(workspace / "library")],
        "download_folders": [str(workspace / "downloads")],
        "state_dir": str(workspace / "state"),
        "log_dir": str(workspace / "logs"),
        "correct_file_extensions_toggle": False,
        "convert_to_cbz_toggle": False,
        "delete_unacceptable_files_toggle": False,
        "delete_chapters_from_downloads_toggle": False,
        "rename_files_in_download_folders_toggle": False,
        "create_folders_for_items_in_download_folder_toggle": False,
        "rename_dirs_in_download_folder_toggle": False,
        "check_for_duplicate_volumes_toggle": False,
        "check_for_existing_series_toggle": False,
        "extract_covers_toggle": False,
        "check_for_missing_volumes_toggle": False,
        "generate_release_group_list_toggle": False,
        "move_series_to_correct_library_toggle": False,
        "move_new_series_to_library_toggle": False,
        "copy_existing_volume_covers_toggle": False,
        "move_lone_files_to_similar_folder": False,
        "replace_series_name_in_file_name_with_similar_folder_name": False,
        "manual_rename": False,
        "manual_delete": False,
        "restructure_when_renaming": False,
        "preferred_volume_renaming_format": "v",
        "preferred_chapter_renaming_format": "c",
        "zfill_volume_int_value": 2,
        "zfill_volume_float_value": 4,
        "zfill_chapter_int_value": 3,
        "zfill_chapter_float_value": 5,
        "add_volume_one_number_to_one_shots": False,
        "add_issue_number_to_manga_file_name": False,
        "search_and_add_premium_to_file_name": False,
        "add_publisher_name_to_file_name_when_renaming": False,
        "move_release_group_to_end_of_file_name": False,
        "replace_unicode_when_restructuring": False,
        "chapter_support_toggle": True,
        "rename_chapters_with_preferred_chapter_keyword": True,
        "extract_chapter_covers": False,
        "match_through_identifiers": False,
        "match_through_image_similarity": False,
        "required_similarity_score": 1.0,
        "required_matching_percentage": 90,
        "ranked_keywords": [
            {"name": "Digital", "score": 3, "file_type": "both"},
            {"name": "HD", "score": 5, "file_type": "both"},
        ],
        "ignored_folder_names": [],
        "library_types": [],
        "unacceptable_keywords": [],
        "compare_detected_cover_to_blank_images": False,
        "use_latest_volume_cover_as_series_cover": False,
        "series_cover_file_names": ["cover", "poster"],
        "output_covers_as_webp": False,
        "compress_image_option": False,
        "image_quality": 40,
        "rename_zip_to_cbz": True,
        "log_to_file": False,
        "watchdog_toggle": False,
        "auto_classify_watchdog_paths": False,
        "bookwalker_check": False,
        "send_scan_request_to_komga_libraries_toggle": False,
        "uncheck_non_qbit_upgrades_toggle": False,
        "discord_webhook_url": [],
        "bookwalker_webhook_urls": [],
        "new_volume_webhook": "",
    }
    values.update(overrides)
    (workspace / "config.toml").write_text(tomli_w.dumps(values), encoding="utf-8")


def _cli(
    workspace: Path, command: str, *arguments: str, expected: int = 0,
) -> dict[str, Any]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(workspace / "home"),
        "XDG_CONFIG_HOME": str(workspace / "home" / "config"),
        "XDG_STATE_HOME": str(workspace / "home" / "state"),
        "XDG_CACHE_HOME": str(workspace / "home" / "cache"),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONUNBUFFERED": "1",
    }
    if "LD_LIBRARY_PATH" in os.environ:
        environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    invocation = [
        sys.executable, "-I", "-m", "komga_cover_extractor",
        "--config", "config.toml", command, *arguments, "--json",
    ]
    process = subprocess.run(
        invocation, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
        capture_output=True, check=False, text=True, timeout=90,
    )
    assert process.returncode == expected, (
        f"{invocation!r}\nstdout={process.stdout}\nstderr={process.stderr}"
    )
    payload = json.loads(process.stdout)
    assert payload["exit_code"] == expected
    assert payload["ok"] is (expected == 0)
    return payload


def _png(color: str) -> bytes:
    image = pytest.importorskip("PIL.Image", reason="real organizer CLI fixtures require covers")
    output = io.BytesIO()
    image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


def _jpeg(color: str) -> bytes:
    image = pytest.importorskip("PIL.Image", reason="real organizer CLI fixtures require covers")
    output = io.BytesIO()
    image.new("RGB", (16, 16), color).save(output, format="JPEG", quality=95)
    return output.getvalue()


def _entries(identity: bytes, color: str = "red") -> dict[str, bytes]:
    return {
        "pages/01.png": _png(color),
        "pages/cover.png": _png("blue"),
        "ComicInfo.xml": b"<ComicInfo><Publisher>Fixture Press</Publisher></ComicInfo>",
        "assets/license.txt": b"Generated acceptance fixture, not a real book.\n",
        "assets/identity.txt": identity,
    }


def _zip(path: Path, entries: dict[str, bytes]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            archive.writestr(info, data)
        archive.comment = b"Generated organizer CLI acceptance fixture."
    return path.read_bytes()


def _book(path: Path, identity: bytes, color: str = "red") -> bytes:
    return _zip(path, _entries(identity, color))


def _sevenzip(path: Path, entries: dict[str, bytes]) -> bytes:
    py7zr = pytest.importorskip("py7zr", reason="actual native CLI conversion requires conversion")
    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        for name, data in entries.items():
            archive.writestr(data, name)
    return path.read_bytes()


def _tree(root: Path) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(), path.lstat().st_mode,
            path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in (root, *sorted(root.rglob("*")))
    )


def _assert_pristine(workspace: Path, before: tuple[tuple[Any, ...], ...]) -> None:
    assert _tree(workspace) == before
    assert not (workspace / "state").exists()
    assert not (workspace / "logs").exists()


def _operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [member for plan in payload["plans"] for member in plan["operations"]]


def _source(member: dict[str, Any]) -> Path | None:
    observation = member["source"]
    if observation is None:
        return None
    return Path(observation["root"]) / observation["relative_path"]


def _assert_retained(workspace: Path, *originals: bytes) -> None:
    retained = Counter(
        path.read_bytes()
        for root in (workspace / "library", workspace / "downloads")
        for path in root.rglob("*")
        if path.is_file() and path.name in {"source", "backup"}
        and any(part.startswith(".kce-quarantine-") for part in path.relative_to(root).parts)
    )
    expected = Counter(originals)
    assert retained >= expected


def _assert_retirement_after_publication(
    payload: dict[str, Any], target: Path, retired: tuple[Path, ...],
) -> None:
    plan = next(
        plan for plan in payload["plans"]
        if any(member["destination"] == str(target) and member["kind"] in {"move", "publish"}
               for member in plan["operations"])
    )
    book_member = next(
        member for member in plan["operations"]
        if member["destination"] == str(target) and member["kind"] in {"move", "publish"}
    )
    quarantines = {
        _source(member): member for member in plan["operations"] if member["kind"] == "quarantine"
    }
    for source in retired:
        assert book_member["member_id"] in quarantines[source]["depends_on"]


def _assert_no_republication(workspace: Path, command: str) -> None:
    before = (_tree(workspace / "library"), _tree(workspace / "downloads"))
    repeated = _cli(workspace, command, "--apply")
    assert repeated["counts"]["committed"] == 0
    assert repeated["counts"]["planned"] == 0
    assert repeated["counts"]["failed"] == 0
    assert (_tree(workspace / "library"), _tree(workspace / "downloads")) == before


@pytest.mark.parametrize("group_lone_file", [False, True], ids=["filename", "filename-and-group"])
def test_organize_preview_apply_repeat_keeps_book_sidecars_and_unrelated_art(
    workspace: Path, group_lone_file: bool,
) -> None:
    _configure(
        workspace, rename_files_in_download_folders_toggle=True,
        create_folders_for_items_in_download_folder_toggle=group_lone_file,
    )
    downloads = workspace / "downloads"
    source = downloads / ("Series Volume 1.cbz" if group_lone_file else "Series/Series Volume 1.cbz")
    original = _book(source, b"original book")
    first, second = _jpeg("red"), _png("blue")
    source.with_suffix(".jpg").write_bytes(first)
    source.with_suffix(".png").write_bytes(second)
    custom = (workspace / "library" / "Unrelated" if group_lone_file else source.parent) / "poster.png"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom_bytes = _png("green")
    custom.write_bytes(custom_bytes)
    (custom.parent / ".notes").write_bytes(b"User notes must survive organization.\n")
    target = downloads / "Series" / "Series v01.cbz"
    before = _tree(workspace)

    preview = _cli(workspace, "organize")

    assert preview["counts"] == {"planned": 1, "committed": 0, "skipped": 0, "failed": 0}
    assert {(_source(member), Path(member["destination"])) for member in _operations(preview)
            if member["kind"] == "move"} == {
        (source, target),
        (source.with_suffix(".jpg"), target.with_suffix(".jpg")),
        (source.with_suffix(".png"), target.with_suffix(".png")),
    }
    _assert_pristine(workspace, before)

    applied = _cli(workspace, "organize", "--apply")

    assert applied["counts"]["committed"] == 1
    assert applied["counts"]["failed"] == 0
    assert target.read_bytes() == original
    assert target.with_suffix(".jpg").read_bytes() == first
    assert target.with_suffix(".png").read_bytes() == second
    assert custom.read_bytes() == custom_bytes
    assert (custom.parent / ".notes").read_bytes() == b"User notes must survive organization.\n"
    assert not any(path.exists() for path in (source, source.with_suffix(".jpg"), source.with_suffix(".png")))
    _assert_retained(workspace, original, first, second)
    _assert_no_republication(workspace, "organize")


@pytest.mark.parametrize("collision_path", ["filename", "group", "directory-merge"])
def test_organizer_collisions_preserve_both_real_books_and_artwork(
    workspace: Path, collision_path: str,
) -> None:
    if collision_path == "filename":
        _configure(workspace, rename_files_in_download_folders_toggle=True)
        source = workspace / "downloads" / "Series Volume 1.cbz"
        destination = workspace / "downloads" / "Series v01.cbz"
    elif collision_path == "group":
        _configure(workspace, create_folders_for_items_in_download_folder_toggle=True)
        source = workspace / "downloads" / "Series v01.cbz"
        destination = workspace / "downloads" / "Series" / "Series v01.cbz"
    else:
        _configure(workspace, rename_dirs_in_download_folder_toggle=True)
        source = workspace / "downloads" / "Series [Batch]" / "Series v01.cbz"
        destination = workspace / "downloads" / "Series" / "Series v01.cbz"
    _book(source, b"different incoming bytes", "red")
    _book(destination, b"existing bytes", "green")
    source.with_suffix(".png").write_bytes(_png("red"))
    destination.with_suffix(".png").write_bytes(_png("green"))
    before = _tree(workspace)

    for flags in ((), ("--apply",)):
        result = _cli(workspace, "organize", *flags, expected=3)
        assert result["counts"]["committed"] == 0
        assert "multiple_planned_outputs" in {item["reason"] for item in result["decisions"]}
        _assert_pristine(workspace, before)


@pytest.mark.parametrize("identical", [False, True], ids=["higher-ranked", "identical-content"])
def test_duplicates_preview_and_authorized_cleanup_keep_a_verified_keeper(
    workspace: Path, identical: bool,
) -> None:
    _configure(workspace)
    folder = workspace / "library" / "Series"
    low = folder / ("Series v01 (B).cbz" if identical else "Series v01 (Scan).cbz")
    high = folder / ("Series v01 (A).cbz" if identical else "Series v01 (Digital).cbz")
    low_bytes = _book(low, b"same bytes" if identical else b"lower-ranked scan")
    high_bytes = _book(high, b"same bytes" if identical else b"higher-ranked digital", "red" if identical else "green")
    old_sidecar = _png("red")
    low.with_suffix(".png").write_bytes(old_sidecar)
    before = _tree(workspace)

    preview = _cli(workspace, "duplicates")

    assert preview["counts"]["planned"] == 1
    assert preview["counts"]["committed"] == 0
    assert {_source(member) for member in _operations(preview) if member["kind"] == "quarantine"} == {
        low, low.with_suffix(".png"),
    }
    _assert_pristine(workspace, before)

    applied = _cli(workspace, "duplicates", "--apply")

    assert applied["counts"]["committed"] == 1
    assert applied["counts"]["failed"] == 0
    assert high.read_bytes() == high_bytes
    assert not low.exists() and not low.with_suffix(".png").exists()
    _assert_retained(workspace, low_bytes, old_sidecar)
    _assert_no_republication(workspace, "duplicates")


@pytest.mark.parametrize("command", ["duplicates", "organize"])
def test_equal_rank_different_bytes_need_review_without_losing_either_candidate(
    workspace: Path, command: str,
) -> None:
    _configure(workspace, check_for_existing_series_toggle=command == "organize")
    existing = workspace / "library" / "Series" / "Series v01 (A).cbz"
    incoming = ((workspace / "downloads" / "Incoming") if command == "organize" else existing.parent) / "Series v01 (B).cbz"
    _book(existing, b"first equal-ranked book", "red")
    _book(incoming, b"different equal-ranked book", "green")
    existing.with_suffix(".png").write_bytes(_png("red"))
    incoming.with_suffix(".png").write_bytes(_png("green"))
    before = _tree(workspace)

    for flags in ((), ("--apply",)):
        result = _cli(workspace, command, *flags, expected=3)
        assert result["counts"]["committed"] == 0
        assert any(item["status"] == "review_required" and item["reason"] == "duplicate_choice_required"
                   for item in result["decisions"])
        _assert_pristine(workspace, before)


def test_strict_upgrade_composes_rename_group_match_and_associated_retirement(workspace: Path) -> None:
    _configure(
        workspace, rename_files_in_download_folders_toggle=True,
        create_folders_for_items_in_download_folder_toggle=True,
        check_for_existing_series_toggle=True,
    )
    existing = workspace / "library" / "Series" / "Series v01 (Scan).cbz"
    incoming = workspace / "downloads" / "Series Volume 1 (Digital).cbz"
    old_bytes = _book(existing, b"old scan", "red")
    incoming_bytes = _book(incoming, b"new digital", "blue")
    old_art, new_art = _png("red"), _png("blue")
    existing.with_suffix(".png").write_bytes(old_art)
    incoming.with_suffix(".png").write_bytes(new_art)
    target = existing.parent / "Series v01 (Digital).cbz"
    before = _tree(workspace)

    preview = _cli(workspace, "organize")

    assert preview["counts"]["planned"] == 1
    assert preview["counts"]["committed"] == 0
    assert (incoming, target) in {
        (_source(member), Path(member["destination"])) for member in _operations(preview)
        if member["kind"] == "move"
    }
    assert not any(member["destination"] == str(workspace / "downloads" / "Series" / target.name)
                   for member in _operations(preview))
    _assert_retirement_after_publication(preview, target, (existing, existing.with_suffix(".png")))
    _assert_pristine(workspace, before)

    applied = _cli(workspace, "organize", "--apply")

    assert applied["counts"]["committed"] == 1
    assert applied["counts"]["failed"] == 0
    assert target.read_bytes() == incoming_bytes
    assert target.with_suffix(".png").read_bytes() == new_art
    assert not any(path.exists() for path in (existing, incoming, existing.with_suffix(".png"), incoming.with_suffix(".png")))
    _assert_retained(workspace, old_bytes, incoming_bytes, old_art, new_art)
    _assert_no_republication(workspace, "organize")


@pytest.mark.parametrize("custom_poster", [False, True], ids=["owned-art-same-mtime", "custom-art-same-mtime"])
def test_upgrade_updates_only_proven_owned_series_art_and_preserves_custom_art(
    workspace: Path, custom_poster: bool,
) -> None:
    _configure(workspace, check_for_existing_series_toggle=True)
    existing = workspace / "library" / "Series" / "Series v01 (Scan).cbz"
    old_bytes = _book(existing, b"original scan", "red")
    seeded = _cli(workspace, "extract", "--apply")
    assert seeded["counts"]["committed"] == 1
    cover, poster = existing.parent / "cover.png", existing.parent / "poster.png"
    old_art = _png("red")
    assert cover.read_bytes() == old_art
    previous_mtime = cover.stat().st_mtime_ns
    incoming = workspace / "downloads" / "Incoming" / "Series v01 (Digital).cbz"
    incoming_bytes = _book(incoming, b"replacement digital", "blue")
    new_art = _png("blue")
    incoming.with_suffix(".png").write_bytes(new_art)
    os.utime(incoming.with_suffix(".png"), ns=(previous_mtime, previous_mtime))
    if custom_poster:
        poster.write_bytes(_png("green"))
        os.utime(poster, ns=(previous_mtime, previous_mtime))
    before = _tree(workspace)
    target = existing.parent / incoming.name

    preview = _cli(workspace, "organize", expected=3 if custom_poster else 0)
    assert preview["counts"]["committed"] == 0
    assert _tree(workspace) == before
    if custom_poster:
        assert "custom_series_artwork" in {item["reason"] for item in preview["decisions"]}
    else:
        assert preview["counts"]["planned"] == 1

    result = _cli(workspace, "organize", "--apply", expected=3 if custom_poster else 0)

    if custom_poster:
        assert result["counts"]["committed"] == 0
        assert "custom_series_artwork" in {item["reason"] for item in result["decisions"]}
        assert _tree(workspace) == before
        assert existing.read_bytes() == old_bytes
        assert incoming.read_bytes() == incoming_bytes
        assert poster.read_bytes() == _png("green")
        assert cover.read_bytes() == old_art
    else:
        assert result["counts"]["committed"] == 1
        assert result["counts"]["failed"] == 0
        assert target.read_bytes() == incoming_bytes
        assert target.with_suffix(".png").read_bytes() == new_art
        assert cover.read_bytes() == new_art
        _assert_retained(workspace, old_bytes, incoming_bytes, old_art, old_art, new_art)
        _assert_no_republication(workspace, "organize")


@pytest.mark.parametrize(
    "collection_name, retired_numbers, preserved_numbers",
    [
        ("Series v01-03 (Digital).cbz", (1, 2, 3), ()),
        ("Series v01-03-05 (Digital).cbz", (1, 3, 5), (2, 4)),
    ],
    ids=["contiguous-range", "explicit-set-keeps-holes"],
)
def test_complete_collection_upgrade_retains_components_after_verified_publication(
    workspace: Path, collection_name: str, retired_numbers: tuple[int, ...],
    preserved_numbers: tuple[int, ...],
) -> None:
    _configure(workspace, check_for_existing_series_toggle=True)
    folder = workspace / "library" / "Series"
    components = {
        number: folder / f"Series v{number:02} (Scan).cbz"
        for number in sorted((*retired_numbers, *preserved_numbers))
    }
    originals = {path: _book(path, f"component {number}".encode()) for number, path in components.items()}
    for number, path in components.items():
        path.with_suffix(".png").write_bytes(_png("red" if number % 2 else "green"))
    collection = workspace / "downloads" / "Incoming" / collection_name
    collection_bytes = _book(collection, b"verified complete collection", "blue")
    collection_art = _png("blue")
    collection.with_suffix(".png").write_bytes(collection_art)
    target = folder / collection_name
    retired = tuple(components[number] for number in retired_numbers)
    before = _tree(workspace)

    preview = _cli(workspace, "organize")

    assert preview["counts"]["planned"] == 1
    assert preview["counts"]["committed"] == 0
    _assert_retirement_after_publication(preview, target, retired)
    _assert_pristine(workspace, before)

    applied = _cli(workspace, "organize", "--apply")

    assert applied["counts"]["committed"] == 1
    assert applied["counts"]["failed"] == 0
    assert target.read_bytes() == collection_bytes
    assert target.with_suffix(".png").read_bytes() == collection_art
    assert not collection.exists()
    for number in retired_numbers:
        assert not components[number].exists()
        assert not components[number].with_suffix(".png").exists()
    for number in preserved_numbers:
        assert components[number].read_bytes() == originals[components[number]]
        assert components[number].with_suffix(".png").read_bytes() == _png("green")
    _assert_retained(workspace, collection_bytes, collection_art, *(originals[path] for path in retired))
    _assert_no_republication(workspace, "organize")


@pytest.mark.parametrize(
    "incoming_name, existing_names, reason",
    [
        ("Series v02 (Digital).cbz", ("Series v01-03 (Scan).cbz",), "partial_collection_overlap"),
        (
            "Series v01-03 (Digital).cbz",
            ("Series v01 (Scan).cbz", "Series v02 (HD).cbz", "Series v03 (Scan).cbz"),
            "duplicate_choice_required",
        ),
    ],
    ids=["partial-overlap", "one-component-has-higher-rank"],
)
def test_uncertain_collection_upgrade_preserves_every_component_and_sidecar(
    workspace: Path, incoming_name: str, existing_names: tuple[str, ...], reason: str,
) -> None:
    _configure(workspace, check_for_existing_series_toggle=True)
    for name in existing_names:
        path = workspace / "library" / "Series" / name
        _book(path, name.encode(), "red")
        path.with_suffix(".png").write_bytes(_png("red"))
    incoming = workspace / "downloads" / "Incoming" / incoming_name
    _book(incoming, b"collection candidate", "blue")
    incoming.with_suffix(".png").write_bytes(_png("blue"))
    before = _tree(workspace)

    for flags in ((), ("--apply",)):
        result = _cli(workspace, "organize", *flags, expected=3)
        assert result["counts"]["committed"] == 0
        assert reason in {item["reason"] for item in result["decisions"]}
        _assert_pristine(workspace, before)


def test_two_equally_eligible_series_folders_are_not_a_first_directory_winner(workspace: Path) -> None:
    _configure(workspace, check_for_existing_series_toggle=True)
    first = workspace / "library" / "First" / "Series"
    second = workspace / "library" / "Second" / "Series"
    _book(first / "Series v01.cbz", b"first library candidate")
    _book(second / "Series v01.cbz", b"second library candidate", "green")
    incoming = workspace / "downloads" / "Incoming" / "Series v02.cbz"
    _book(incoming, b"incoming second volume", "blue")
    incoming.with_suffix(".png").write_bytes(_png("blue"))
    before = _tree(workspace)

    for flags in ((), ("--apply",)):
        result = _cli(workspace, "organize", *flags, expected=3)
        assert result["counts"]["committed"] == 0
        ambiguous = next(item for item in result["decisions"] if item["status"] == "ambiguous")
        assert ambiguous["candidates"] == [str(first), str(second)]
        _assert_pristine(workspace, before)


def test_unreadable_library_archive_is_not_empty_success_or_upgrade_permission(workspace: Path) -> None:
    _configure(workspace, check_for_existing_series_toggle=True)
    existing = workspace / "library" / "Series" / "Series v01 (Scan).cbz"
    existing.parent.mkdir()
    existing.write_bytes(b"Corrupt archive. Content identity and library evidence are unavailable.")
    existing.with_suffix(".png").write_bytes(_png("red"))
    incoming = workspace / "downloads" / "Incoming" / "Series v01 (Digital).cbz"
    _book(incoming, b"otherwise valid higher-ranked candidate", "blue")
    before = _tree(workspace)

    result = _cli(workspace, "organize", "--apply", expected=2)

    assert result["counts"]["committed"] == 0
    assert any(item["status"] in {"invalid_input", "missing_dependency"} for item in result["decisions"])
    _assert_pristine(workspace, before)


def test_unknown_sidecar_owner_with_two_same_stem_books_preserves_all_candidates(workspace: Path) -> None:
    _configure(workspace, rename_files_in_download_folders_toggle=True)
    comic = workspace / "downloads" / "Series Volume 1.cbz"
    novel = comic.with_suffix(".epub")
    _book(comic, b"comic book")
    _zip(novel, {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": (
            b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<rootfiles><rootfile full-path="OPS/package.opf" '
            b'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ),
        "OPS/package.opf": (
            b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            b'<manifest><item id="cover" href="images/cover.png" '
            b'media-type="image/png" properties="cover-image"/></manifest></package>'
        ),
        "OPS/images/cover.png": _png("green"),
    })
    comic.with_suffix(".png").write_bytes(_png("blue"))
    before = _tree(workspace)

    result = _cli(workspace, "organize", "--apply", expected=3)

    assert result["counts"]["committed"] == 0
    assert "shared_book_sidecar_stem" in {item["reason"] for item in result["decisions"]}
    _assert_pristine(workspace, before)


@pytest.mark.parametrize("interactive", [False, True], ids=["noninteractive", "interactive-without-terminal"])
@pytest.mark.parametrize("command", ["organize", "duplicates"])
def test_manual_policy_requires_review_without_waiting_for_nonterminal_input(
    workspace: Path, command: str, interactive: bool,
) -> None:
    if command == "organize":
        _configure(workspace, manual_rename=True, rename_files_in_download_folders_toggle=True)
        _book(workspace / "downloads" / "Series Volume 1.cbz", b"manual rename candidate")
        reason = "manual_rename_required"
    else:
        _configure(workspace, manual_delete=True)
        _book(workspace / "library" / "Series" / "Series v01 (Scan).cbz", b"scan")
        _book(workspace / "library" / "Series" / "Series v01 (Digital).cbz", b"digital", "green")
        reason = "manual_retire_required"
    before = _tree(workspace)

    result = _cli(workspace, command, "--apply", *(("--interactive",) if interactive else ()), expected=3)

    assert result["counts"]["committed"] == 0
    assert reason in {item["reason"] for item in result["decisions"]}
    _assert_pristine(workspace, before)


@pytest.mark.parametrize(
    "container, covers_before_matching",
    [("zip", False), ("7z", True)],
    ids=["verified-zip-header-and-late-art", "native-7z-header-and-early-art"],
)
def test_complete_run_uses_original_bytes_through_header_conversion_rename_match_and_art(
    workspace: Path, container: str, covers_before_matching: bool,
) -> None:
    roots = [str(workspace / "library")]
    if covers_before_matching:
        roots.append(str(workspace / "downloads"))
    _configure(
        workspace, paths=roots, correct_file_extensions_toggle=True,
        convert_to_cbz_toggle=True, rename_files_in_download_folders_toggle=True,
        create_folders_for_items_in_download_folder_toggle=True,
        check_for_duplicate_volumes_toggle=True, check_for_existing_series_toggle=True,
        extract_covers_toggle=True, output_covers_as_webp=True, compress_image_option=False,
    )
    existing = workspace / "library" / "Series" / "Series v02.cbz"
    existing_bytes = _book(existing, b"existing volume two", "green")
    source = workspace / "downloads" / "Series Volume 1.cbr"
    entries = _entries(b"observed volume one before any filename change", "red")
    source_bytes = _sevenzip(source, entries) if container == "7z" else _zip(source, entries)
    target = existing.parent / "Series v01.cbz"
    before = _tree(workspace)

    preview = _cli(workspace, "run")

    assert preview["counts"]["committed"] == 0
    assert preview["counts"]["failed"] == 0
    assert any(member["destination"] == str(target) and member["kind"] in {"move", "publish"}
               for member in _operations(preview))
    assert {item["path"] for item in preview["releases"]} == {str(target), str(existing)}
    _assert_pristine(workspace, before)

    applied = _cli(workspace, "run", "--apply")

    assert applied["counts"]["committed"] >= 1
    assert applied["counts"]["failed"] == 0
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert {name: archive.read(name) for name in archive.namelist()} == entries
        if container == "zip":
            assert archive.comment == b"Generated organizer CLI acceptance fixture."
    assert existing.read_bytes() == existing_bytes
    image = pytest.importorskip("PIL.Image", reason="truthful published artwork requires covers")
    sidecar = target.with_suffix(".webp")
    for artwork in (sidecar, existing.parent / "cover.webp"):
        with image.open(artwork) as decoded:
            assert decoded.format == "WEBP"
            assert decoded.size == (16, 16)
            red, green, blue = decoded.convert("RGB").getpixel((8, 8))
            assert abs(red - 255) <= 5 and green <= 5 and blue <= 5
    assert sidecar.read_bytes() == (existing.parent / "cover.webp").read_bytes()
    assert not source.exists()
    assert not (workspace / "downloads" / "Series Volume 1.7z").exists()
    assert not (workspace / "downloads" / "Series Volume 1.cbz").exists()
    assert not (workspace / "downloads" / "Series" / "Series v01.cbz").exists()
    assert not any(path.is_file() and not any(part.startswith(".kce-") for part in path.relative_to(workspace / "downloads").parts)
                   for path in (workspace / "downloads").rglob("*"))
    _assert_retained(workspace, source_bytes)
    _assert_no_republication(workspace, "run")


@pytest.mark.parametrize("restructure", [False, True])
@pytest.mark.parametrize(
    ("name", "target_name", "part"),
    [
        ("Series c53x0.cbz", "Series c053x0.cbz", 0),
        ("Series c53x1.5.cbz", "Series c53x1.5.cbz", "1.5"),
    ],
)
def test_cli_preserves_legacy_zero_and_fractional_chapter_parts_during_rename(
    workspace: Path, restructure: bool, name: str, target_name: str, part: int | str,
) -> None:
    _configure(
        workspace, rename_files_in_download_folders_toggle=True,
        restructure_when_renaming=restructure,
    )
    source = workspace / "downloads" / "Incoming" / name
    original = _book(source, b"zero or fractional chapter part")
    target = source.with_name(target_name)
    expected_commits = int(target != source)
    before = _tree(workspace)

    inspected = _cli(workspace, "inspect", str(source))
    number = inspected["releases"][0]["number"]
    assert number["values"] == ["53"] and number["part"] == part
    preview = _cli(workspace, "organize")
    assert preview["counts"]["planned"] == expected_commits and preview["counts"]["committed"] == 0
    _assert_pristine(workspace, before)

    applied = _cli(workspace, "organize", "--apply")
    assert applied["counts"]["committed"] == expected_commits and applied["counts"]["failed"] == 0
    assert target.read_bytes() == original
    if target != source:
        assert not source.exists()
    observed = next(release for release in applied["releases"] if release["path"] == str(target))
    assert observed["number"]["values"] == ["53"] and observed["number"]["part"] == part
    _assert_no_republication(workspace, "organize")

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Literal

import pytest
from PIL import Image

from komga_cover_extractor.application import artwork_workflow
from komga_cover_extractor.application.artwork_workflow import extract_artwork
from komga_cover_extractor.covers.selection import CoverPolicy
from komga_cover_extractor.covers.series_policy import SeriesCoverPolicy
from komga_cover_extractor.domain.decisions import RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.infrastructure.filesystem import BOOK_SUFFIXES, fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
from komga_cover_extractor.matching.classification import (
    ClassificationPolicy,
    LibraryType,
)
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release


def _raster(color: str, image_format: str = "PNG") -> bytes:
    output = BytesIO()
    with Image.new("RGB", (16, 16), color) as image:
        image.save(output, format=image_format)
    return output.getvalue()


def _book(path: Path, color: str = "red", *, invalid_image: bool = False, comment: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("cover.png", _raster("blue"))
        archive.writestr("01.png", b"not a raster" if invalid_image else _raster(color))
        archive.comment = comment
    return path


def _snapshot(root: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[key] = ("link", str(path.readlink()))
        elif path.is_file():
            result[key] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
        elif path.is_dir():
            result[key] = ("directory",)
    return result


def _catalog(roots: tuple[LibraryRoot, ...], parsing: ParsingPolicy) -> LibrarySnapshot:
    releases = []
    for root in roots:
        for path in sorted(root.path.rglob("*")):
            if not path.is_symlink() and path.is_file() and path.suffix.lower() in BOOK_SUFFIXES:
                siblings = tuple(sibling for sibling in sorted(path.parent.iterdir()) if stat.S_ISREG(sibling.lstat().st_mode))
                role = "both" if {"library", "download"} <= set(root.roles) else root.roles[0]
                releases.append(parse_release(FileObservation(path, siblings=siblings,
                               fingerprint=fingerprint(path, root.path), root_role=role,
                               at_configured_root=path.parent == root.path), parsing))
    return LibrarySnapshot(tuple(releases), roots)


def _run(tmp_path: Path, selected: tuple[Path, ...], *, apply: bool = False,
         mode: Literal["first", "latest"] = "first", webp: bool = False,
         series_policy: SeriesCoverPolicy | None = None, cover_policy: CoverPolicy | None = None,
         roots: tuple[LibraryRoot, ...] | None = None, cancel: Event | None = None,
         catalog: LibrarySnapshot | None = None,
         classification: ClassificationPolicy = ClassificationPolicy()) -> RunResult:
    roots = (LibraryRoot(tmp_path / "library"),) if roots is None else roots
    parsing = ParsingPolicy(chapter_support_toggle=True)
    catalog = _catalog(roots, parsing) if catalog is None else catalog
    selected_releases = tuple(release for release in catalog.releases if release.path in selected)
    assert len(selected_releases) == len(selected)
    covers = cover_policy or CoverPolicy(ArchivePolicy(), output_covers_as_webp=webp)
    series = series_policy or SeriesCoverPolicy(mode=mode)
    version = sha256(json.dumps({"cover": covers.as_dict(), "mode": series.mode,
                                "roles": series.roles, "copy": series.copy_existing_volume_covers_toggle,
                                "chapters": series.extract_chapter_covers}, sort_keys=True).encode()).hexdigest()
    return extract_artwork(selected_releases, catalog, roots=roots, parsing=parsing,
                           cover_policy=covers, series_policy=series, state_dir=tmp_path / "state",
                           policy_version=version, authorization=RunAuthorization(apply=apply),
                           cancel=cancel or Event(), classification=classification)


def _assert_color(path: Path, color: str, image_format: str) -> None:
    with Image.open(path) as image:
        assert image.format == image_format
        red, green, blue = image.convert("RGB").getpixel((8, 8))
    if color == "red":
        assert red > 240 and green < 12 and blue < 12
    elif color == "green":
        assert green > 115 and red < 12 and blue < 12
    else:
        assert blue > 240 and red < 12 and green < 12


def _owned_image(tmp_path: Path, book: Path, target: Path, data: bytes, operation_id: str = "recorded-art") -> None:
    root = tmp_path / "library"
    plan = OperationPlan("previous-image-policy", operation_id,
                         (Operation("image", "publish", target, source=fingerprint(book, root),
                                    artifact=ArtifactSpec(content=data, media_type="image/png")),),
                         (RootPermission(root),))
    result = TransactionExecutor(tmp_path / "state", (root,)).apply(plan, authorization=RunAuthorization(apply=True))
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)


@pytest.mark.parametrize(("mode", "series_color"), [("first", "red"), ("latest", "green")])
def test_real_preview_and_apply_publish_correct_webp_groups(tmp_path: Path, mode: Literal["first", "latest"], series_color: str) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    latest = _book(first.parent / "Series v02.cbz", "green")
    before = _snapshot(tmp_path)
    preview = _run(tmp_path, (first, latest), mode=mode, webp=True)
    assert (preview.planned, preview.committed, preview.failed, preview.exit_code) == (2, 0, 0, 0)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()
    assert preview.plans == ()
    plans = preview.as_dict()["plans"]
    assert isinstance(plans, list)
    assert len(plans) == 2
    chosen = first if mode == "first" else latest
    series_members = [member for plan in plans for member in plan["operations"] if member["member_id"] == "series-cover"]
    assert len(series_members) == 1
    assert series_members[0]["source"] == fingerprint(chosen, tmp_path / "library").as_dict()
    assert series_members[0]["artifact"]["media_type"] == "image/webp"
    assert "content_base64" not in series_members[0]["artifact"]
    applied = _run(tmp_path, (first, latest), apply=True, mode=mode, webp=True)
    assert (applied.planned, applied.committed, applied.skipped, applied.failed, applied.exit_code) == (2, 2, 0, 0, 0)
    _assert_color(first.with_suffix(".webp"), "red", "WEBP")
    _assert_color(latest.with_suffix(".webp"), "green", "WEBP")
    _assert_color(first.parent / "cover.webp", series_color, "WEBP")
    assert not (first.parent / "poster.webp").exists()
    assert first.read_bytes() == before["library/Series/Series v01.cbz"][1]
    assert latest.read_bytes() == before["library/Series/Series v02.cbz"][1]
    provenance = StateStore(tmp_path / "state").committed_artifact(first.parent / "cover.webp")
    assert provenance is not None
    assert provenance.source == fingerprint(chosen, tmp_path / "library")
    assert provenance.published.sha256 == sha256((first.parent / "cover.webp").read_bytes()).hexdigest()


def test_repeated_extraction_preserves_image_inodes_and_mtimes(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True, webp=True).committed == 1
    images = (book.with_suffix(".webp"), book.parent / "cover.webp")
    before = {path: fingerprint(path, tmp_path / "library") for path in images}
    repeat = _run(tmp_path, (book,), apply=True, webp=True)
    assert (repeat.planned, repeat.committed, repeat.skipped, repeat.failed, repeat.exit_code) == (0, 0, 1, 0, 0)
    assert {path: fingerprint(path, tmp_path / "library") for path in images} == before


def test_existing_journal_preview_is_read_only(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    before = _snapshot(tmp_path)
    preview = _run(tmp_path, (book,), webp=True)
    assert (preview.planned, preview.committed, preview.failed, preview.exit_code) == (1, 0, 0, 0)
    assert _snapshot(tmp_path) == before


def test_unselected_volume_one_supplies_series_art_only(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    latest = _book(first.parent / "Series v02.cbz", "green")
    first_bytes = first.read_bytes()
    result = _run(tmp_path, (latest,), apply=True)
    assert (result.planned, result.committed, result.failed, result.exit_code) == (1, 1, 0, 0)
    assert not first.with_suffix(".png").exists()
    _assert_color(latest.with_suffix(".png"), "green", "PNG")
    _assert_color(first.parent / "cover.png", "red", "PNG")
    assert first.read_bytes() == first_bytes
    plan = result.plan_reports[0].as_dict()
    assert [member["destination"] for member in plan["operations"]] == [str(latest.with_suffix(".png")), str(first.parent / "cover.png")]
    assert plan["operations"][1]["source"] == fingerprint(first, tmp_path / "library").as_dict()


def test_explicit_book_selection_does_not_hide_second_volume_one(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01 [A].cbz")
    other = _book(first.parent / "Series v01 [B].cbz", "green")
    result = _run(tmp_path, (first,), apply=True)
    assert result.exit_code == 3
    assert any(decision.reason == "multiple_qualifying_releases" and set(decision.candidates) == {str(first), str(other)} for decision in result.decisions)
    assert not (first.parent / "cover.png").exists()
    assert not other.with_suffix(".png").exists()
    assert first.exists() and other.exists()


def test_same_stem_archive_sibling_conflicts_before_any_sidecar_apply(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    _book(first.with_suffix(".epub"), "green")
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (first,), apply=True)
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 3)
    assert [(decision.status, decision.reason) for decision in result.decisions] == [("conflict", "shared_book_sidecar_stem")]
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_untracked_custom_poster_blocks_the_chosen_group_without_clobber(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    poster = book.parent / "poster.png"
    poster.write_bytes(_raster("blue"))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 3)
    assert any(decision.reason == "custom_series_artwork" and decision.candidates == (str(poster),) for decision in result.decisions)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_identical_untracked_art_is_preserved_without_rewriting(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    sidecar = book.with_suffix(".png")
    sidecar.write_bytes(_raster("red"))
    poster = book.parent / "poster.png"
    poster.write_bytes(_raster("red"))
    before = {path: fingerprint(path, tmp_path / "library") for path in (sidecar, poster)}
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.exit_code) == (1, 0)
    assert {path: fingerprint(path, tmp_path / "library") for path in before} == before
    _assert_color(book.parent / "cover.png", "red", "PNG")


def test_generated_poster_updates_and_retains_prior_bytes(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    poster = book.parent / "poster.png"
    old = _raster("blue")
    _owned_image(tmp_path, book, poster, old)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.planned, result.committed, result.failed, result.exit_code) == (1, 1, 0, 0)
    _assert_color(poster, "red", "PNG")
    retained = [path for path in (tmp_path / "library").rglob("*") if path.is_file() and path != poster and path.read_bytes() == old]
    assert len(retained) == 1
    assert any(part.startswith(".kce-") for part in retained[0].parts)
    assert StateStore(tmp_path / "state").committed_artifact(poster).source == fingerprint(book, tmp_path / "library")


def test_generated_art_cross_extension_cutover_keeps_old_bytes_in_quarantine(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    old_art = book.with_suffix(".png").read_bytes()
    book_bytes = book.read_bytes()
    result = _run(tmp_path, (book,), apply=True, webp=True)
    assert (result.planned, result.committed, result.failed, result.exit_code) == (1, 1, 0, 0)
    assert not book.with_suffix(".png").exists()
    assert not (book.parent / "cover.png").exists()
    _assert_color(book.with_suffix(".webp"), "red", "WEBP")
    _assert_color(book.parent / "cover.webp", "red", "WEBP")
    assert book.read_bytes() == book_bytes
    retained = [path for path in (tmp_path / "library").rglob("*") if path.is_file() and path.read_bytes() == old_art]
    assert len(retained) == 2
    operations = result.plan_reports[0].as_dict()["operations"]
    quarantines = [member for member in operations if member["kind"] == "quarantine"]
    assert len(quarantines) == 2
    assert all(member["destination"] == str(Path(member["source"]["root"]) / member["source"]["relative_path"]) for member in quarantines)
    assert all(member["depends_on"] == ["book-artwork", "series-cover"] for member in quarantines)


def test_equal_mtime_changed_book_is_reextracted_and_old_art_retained(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    old = _raster("red")
    source_info = book.stat()
    _book(book, "green")
    os.utime(book, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    _assert_color(book.with_suffix(".png"), "green", "PNG")
    _assert_color(book.parent / "cover.png", "green", "PNG")
    assert len([path for path in (tmp_path / "library").rglob("*") if path.is_file() and path.read_bytes() == old]) == 2


def test_equal_mtime_replaced_series_art_loses_generated_ownership(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    cover = book.parent / "cover.png"
    info = cover.stat()
    cover.write_bytes(_raster("blue"))
    os.utime(cover, ns=(info.st_atime_ns, info.st_mtime_ns))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.exit_code) == (0, 3)
    assert any(decision.reason == "custom_series_artwork" for decision in result.decisions)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("name", ["Series v01.png", "cover.png", "poster.png"])
def test_unsafe_artwork_links_are_not_followed_or_replaced(tmp_path: Path, name: str) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    outside = tmp_path / "outside.png"
    outside.write_bytes(_raster("blue"))
    (book.parent / name).symlink_to(outside)
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.exit_code) == (0, 3)
    assert any(decision.status == "conflict" and decision.reason == "symlink_traversal" for decision in result.decisions)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_bad_image_is_isolated_and_other_books_still_publish(tmp_path: Path) -> None:
    bad = _book(tmp_path / "library" / "Bad" / "Bad v01.cbz", invalid_image=True)
    good = _book(tmp_path / "library" / "Good" / "Good v01.cbz", "green")
    original = bad.read_bytes()
    result = _run(tmp_path, (bad, good), apply=True)
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (1, 1, 1, 2)
    assert any(decision.path == bad and decision.status == "invalid_input" and decision.reason == "invalid_image" for decision in result.decisions)
    assert bad.read_bytes() == original
    assert not bad.with_suffix(".png").exists()
    _assert_color(good.with_suffix(".png"), "green", "PNG")
    _assert_color(good.parent / "cover.png", "green", "PNG")


def test_cancelled_preview_preserves_every_source_and_creates_no_state(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    latest = _book(first.parent / "Series v02.cbz", "green")
    cancel = Event()
    cancel.set()
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (first, latest), cancel=cancel)
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 2, 3)
    assert [(decision.status, decision.reason) for decision in result.decisions] == [("cancelled", "shutdown_requested"), ("cancelled", "shutdown_requested")]
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("restriction", ["subfolder", "mixed", "download-root"])
def test_series_folder_restrictions_do_not_disable_book_sidecars(tmp_path: Path, restriction: str) -> None:
    directory = tmp_path / "library" if restriction == "download-root" else tmp_path / "library" / "Series"
    book = _book(directory / "Series v01.cbz")
    roots = (LibraryRoot(tmp_path / "library", roles=("download",)),) if restriction == "download-root" else None
    if restriction == "subfolder":
        (directory / "Extras").mkdir()
    elif restriction == "mixed":
        _book(directory / "Other v02.cbz", "green")
    result = _run(tmp_path, (book,), apply=True, roots=roots)
    assert result.committed == 1
    _assert_color(book.with_suffix(".png"), "red", "PNG")
    assert not (directory / "cover.png").exists()
    reasons = {decision.reason for decision in result.decisions}
    assert {"subfolder": "series_contains_subfolders", "mixed": "mixed_series_directory", "download-root": "configured_root_artwork_disabled"}[restriction] in reasons


def test_empty_roles_disable_series_art_without_disabling_book_sidecars(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    result = _run(tmp_path, (book,), apply=True, series_policy=SeriesCoverPolicy(roles=(), create_roles=()))
    assert (result.committed, result.exit_code) == (1, 0)
    _assert_color(book.with_suffix(".png"), "red", "PNG")
    assert not (book.parent / "cover.png").exists()


def test_safe_configured_role_updates_only_recorded_art_and_is_not_created_by_default(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    policy = SeriesCoverPolicy(roles=("landscape art",), create_roles=())
    result = _run(tmp_path, (book,), apply=True, series_policy=policy)
    assert (result.committed, result.exit_code) == (1, 0)
    assert not (book.parent / "landscape art.png").exists()
    landscape = book.parent / "landscape art.png"
    _owned_image(tmp_path, book, landscape, _raster("blue"))
    updated = _run(tmp_path, (book,), apply=True, series_policy=policy)
    assert (updated.committed, updated.failed, updated.exit_code) == (1, 0, 0)
    _assert_color(landscape, "red", "PNG")
    assert not (book.parent / "cover.png").exists()


def test_role_overlapping_book_stem_conflicts_before_any_sidecar_publication(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True,
                  series_policy=SeriesCoverPolicy(roles=("Series v01",), create_roles=()))
    assert (result.planned, result.committed, result.exit_code) == (0, 0, 3)
    assert [(decision.status, decision.reason) for decision in result.decisions] == [("conflict", "series_role_book_sidecar_overlap")]
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_chapter_copy_uses_unique_name_matched_volume_one_without_other_sidecars(tmp_path: Path) -> None:
    volume = _book(tmp_path / "library" / "Volumes" / "Series" / "Series v01.cbz")
    chapter = _book(tmp_path / "library" / "Chapters" / "Series" / "Series c001.cbz", "green")
    _book(tmp_path / "library" / "Volumes" / "Unrelated" / "Unrelated v01.cbz", "blue")
    policy = SeriesCoverPolicy(copy_existing_volume_covers_toggle=True)
    result = _run(tmp_path, (chapter,), apply=True, series_policy=policy)
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    _assert_color(chapter.parent / "cover.png", "red", "PNG")
    assert not chapter.with_suffix(".png").exists()
    assert not volume.with_suffix(".png").exists()
    provenance = StateStore(tmp_path / "state").committed_artifact(chapter.parent / "cover.png")
    assert provenance is not None and provenance.source == fingerprint(volume, tmp_path / "library")


def test_chapter_copy_never_chooses_first_of_two_name_matches(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "One" / "Series" / "Series v01.cbz")
    other = _book(tmp_path / "library" / "Two" / "Series" / "Series v01.cbz", "green")
    chapter = _book(tmp_path / "library" / "Chapters" / "Series" / "Series c001.cbz")
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (chapter,), apply=True, series_policy=SeriesCoverPolicy(copy_existing_volume_covers_toggle=True))
    assert (result.committed, result.exit_code) == (0, 3)
    assert any(decision.reason == "multiple_volume_series_matches" and set(decision.candidates) == {str(first.parent), str(other.parent)} for decision in result.decisions)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_copy_existing_flag_off_does_not_copy_volume_art(tmp_path: Path) -> None:
    volume = _book(tmp_path / "library" / "Volumes" / "Series" / "Series v01.cbz")
    chapter = _book(tmp_path / "library" / "Chapters" / "Series" / "Series c001.cbz", "green")
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (chapter,), apply=True)
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 0)
    assert any(decision.reason == "chapter_cover_disabled" for decision in result.decisions)
    assert _snapshot(tmp_path) == before
    assert not volume.with_suffix(".png").exists()


def test_non_writable_directory_fails_before_state_and_publication(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    before = _snapshot(tmp_path)
    book.parent.chmod(0o555)
    try:
        if os.geteuid() == 0:
            import pwd
            import tempfile
            nobody = pwd.getpwnam("nobody")
            with tempfile.TemporaryDirectory(prefix="kce-read-only-artwork-") as temporary:
                root = Path(temporary)
                root.chmod(0o755)
                fixture = _book(root / "library" / "Series" / "Series v01.cbz")
                (root / "library").chmod(0o777)
                fixture.parent.chmod(0o555)
                code = """import json,sys
from pathlib import Path
from threading import Event
from komga_cover_extractor.application.artwork_workflow import extract_artwork
from komga_cover_extractor.covers.selection import CoverPolicy
from komga_cover_extractor.covers.series_policy import SeriesCoverPolicy
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.libraries import LibraryRoot,LibrarySnapshot
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
from komga_cover_extractor.parsing.filenames import ParsingPolicy,parse_release
root=Path(sys.argv[1]); book=root/'library/Series/Series v01.cbz'; library=LibraryRoot(root/'library'); parsing=ParsingPolicy()
release=parse_release(FileObservation(book,siblings=(book,),root_role='library',fingerprint=fingerprint(book,library.path)),parsing)
result=extract_artwork((release,),LibrarySnapshot((release,),(library,)),roots=(library,),parsing=parsing,cover_policy=CoverPolicy(ArchivePolicy()),series_policy=SeriesCoverPolicy(),state_dir=root/'state',policy_version='permissions',authorization=RunAuthorization(apply=True),cancel=Event())
print(json.dumps(result.as_dict()))
"""
                process = subprocess.run([sys.executable, "-I", "-c", code, str(root)],
                                         user=nobody.pw_uid, group=nobody.pw_gid, extra_groups=(),
                                         capture_output=True, text=True, timeout=30, check=False)
                assert process.returncode == 0, process.stderr
                output = json.loads(process.stdout)
                assert output["counts"]["committed"] == 0 and output["counts"]["failed"] == 1
                assert any(decision["status"] == "permission_denied" for decision in output["decisions"])
                assert not fixture.with_suffix(".png").exists()
                assert not (root / "state").exists()
                fixture.parent.chmod(0o755)
        else:
            result = _run(tmp_path, (book,), apply=True)
            assert (result.committed, result.failed, result.exit_code) == (0, 1, 1)
            assert any(decision.status == "permission_denied" for decision in result.decisions)
            assert not (tmp_path / "state").exists()
    finally:
        book.parent.chmod(0o755)
    assert _snapshot(tmp_path) == before


def test_source_change_before_publication_is_revalidated_not_hidden_by_equal_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz", comment=b"a")
    info = book.stat()
    original = book.read_bytes()
    changed = original[:-1] + b"b"
    real_executor = TransactionExecutor

    def checkpoint(phase: str, member_id: str) -> None:
        if phase == "before_publish" and member_id == "book-artwork":
            book.write_bytes(changed)
            os.utime(book, ns=(info.st_atime_ns, info.st_mtime_ns))

    def executor(*args: object, **kwargs: object) -> TransactionExecutor:
        return real_executor(*args, checkpoint=checkpoint, **kwargs)

    monkeypatch.setattr(artwork_workflow, "TransactionExecutor", executor)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.exit_code) == (0, 3)
    assert any(decision.reason == "publication_source_fingerprint_changed" for decision in result.decisions)
    assert book.read_bytes() == changed
    assert not book.with_suffix(".png").exists()
    assert not (book.parent / "cover.png").exists()


def test_interrupted_series_publication_retains_old_bytes_and_recovers_real_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    old = book.with_suffix(".png").read_bytes()
    real_executor = TransactionExecutor

    def checkpoint(phase: str, member_id: str) -> None:
        if phase == "before_publish" and member_id == "series-cover":
            raise OSError("injected series publication failure")

    def executor(*args: object, **kwargs: object) -> TransactionExecutor:
        return real_executor(*args, checkpoint=checkpoint, **kwargs)

    monkeypatch.setattr(artwork_workflow, "TransactionExecutor", executor)
    result = _run(tmp_path, (book,), apply=True, webp=True)
    assert (result.planned, result.committed, result.failed, result.exit_code) == (1, 0, 1, 1)
    assert book.exists() and book.with_suffix(".png").read_bytes() == old
    assert (book.parent / "cover.png").read_bytes() == old
    operation_id = result.plan_reports[0].operation_id
    store = StateStore(tmp_path / "state")
    assert store.committed_artifact(book.with_suffix(".webp")) is None
    recovered = real_executor(tmp_path / "state", (tmp_path / "library",)).recover(operation_id, authorization=RunAuthorization(apply=True))
    assert (recovered.committed, recovered.failed, recovered.exit_code) == (1, 0, 0)
    _assert_color(book.with_suffix(".webp"), "red", "WEBP")
    _assert_color(book.parent / "cover.webp", "red", "WEBP")
    assert not book.with_suffix(".png").exists()
    assert not (book.parent / "cover.png").exists()
    assert len([path for path in (tmp_path / "library").rglob("*") if path.is_file() and path.read_bytes() == old]) == 2


def test_recorded_book_and_image_moves_preserve_generated_association(tmp_path: Path) -> None:
    old_book = _book(tmp_path / "library" / "Original" / "Series v01.cbz")
    assert _run(tmp_path, (old_book,), apply=True).committed == 1
    new_directory = tmp_path / "library" / "Moved"
    new_directory.mkdir()
    new_book = new_directory / old_book.name
    root = tmp_path / "library"
    sources = (old_book, old_book.with_suffix(".png"), old_book.parent / "cover.png")
    destinations = (new_book, new_book.with_suffix(".png"), new_directory / "cover.png")
    plan = OperationPlan("move-policy", "move-book-and-art",
                         tuple(Operation(f"move-{index}", "move", destination, source=fingerprint(source, root))
                               for index, (source, destination) in enumerate(zip(sources, destinations, strict=True))),
                         (RootPermission(root),))
    moved = TransactionExecutor(tmp_path / "state", (root,)).apply(plan, authorization=RunAuthorization(apply=True))
    assert (moved.committed, moved.failed, moved.exit_code) == (1, 0, 0)
    result = _run(tmp_path, (new_book,), apply=True, webp=True)
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    _assert_color(new_book.with_suffix(".webp"), "red", "WEBP")
    _assert_color(new_directory / "cover.webp", "red", "WEBP")
    assert new_book.exists() and not old_book.exists()
    assert not new_book.with_suffix(".png").exists()


def test_malformed_state_provenance_fails_closed_without_replacement(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    with sqlite3.connect(tmp_path / "state" / "state.sqlite3") as connection:
        connection.execute("UPDATE operations SET plan_sha256=?", ("0" * 64,))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True, webp=True)
    assert (result.committed, result.exit_code) == (0, 3)
    assert any(decision.reason == "artwork_provenance_unavailable" for decision in result.decisions)
    assert _snapshot(tmp_path) == before


def test_first_to_latest_update_borrows_verified_sidecar_and_does_not_rewrite_books(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    latest = _book(first.parent / "Series v02.cbz", "green")
    assert _run(tmp_path, (first, latest), apply=True).committed == 2
    sidecars = {path: fingerprint(path, tmp_path / "library") for path in (first.with_suffix(".png"), latest.with_suffix(".png"))}
    result = _run(tmp_path, (first, latest), apply=True, mode="latest")
    assert (result.planned, result.committed, result.skipped, result.failed, result.exit_code) == (1, 1, 1, 0, 0)
    assert {path: fingerprint(path, tmp_path / "library") for path in sidecars} == sidecars
    _assert_color(first.parent / "cover.png", "green", "PNG")
    members = result.plan_reports[0].as_dict()["operations"]
    assert len(members) == 1
    assert members[0]["member_id"] == "series-cover"
    assert members[0]["source"] == fingerprint(latest, tmp_path / "library").as_dict()
    assert members[0]["artifact"]["source"] == sidecars[latest.with_suffix(".png")].as_dict()
    assert len([path for path in (tmp_path / "library").rglob("*") if path.is_file() and path != first.with_suffix(".png") and path.read_bytes() == _raster("red")]) == 1


@pytest.mark.parametrize("mode", ["first", "latest"])
def test_actual_part_selection_keeps_part_twelve_distinct(tmp_path: Path, mode: Literal["first", "latest"]) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01 part1.cbz")
    twelfth = _book(first.parent / "Series v01 part12.cbz", "green")
    result = _run(tmp_path, (first, twelfth), apply=True, mode=mode)
    assert (result.committed, result.failed, result.exit_code) == (2, 0, 0)
    _assert_color(first.parent / "cover.png", "red" if mode == "first" else "green", "PNG")
    selected = next(member for report in result.plan_reports for member in report.as_dict()["operations"] if member["member_id"] == "series-cover")
    assert selected["source"] == fingerprint(first if mode == "first" else twelfth, tmp_path / "library").as_dict()


@pytest.mark.parametrize("mode", ["first", "latest"])
def test_actual_contiguous_range_qualifies_for_volume_one_and_latest_endpoint(tmp_path: Path, mode: Literal["first", "latest"]) -> None:
    collection = _book(tmp_path / "library" / "Series" / "Series v01-03.cbz")
    second = _book(collection.parent / "Series v02.cbz", "green")
    result = _run(tmp_path, (second,), apply=True, mode=mode)
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    _assert_color(collection.parent / "cover.png", "red", "PNG")
    assert not collection.with_suffix(".png").exists()
    record = StateStore(tmp_path / "state").committed_artifact(collection.parent / "cover.png")
    assert record is not None and record.source == fingerprint(collection, tmp_path / "library")


def test_explicit_set_without_one_does_not_invent_volume_one(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v00-02-03.cbz")
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    assert any(decision.reason == "volume_one_required" for decision in result.decisions)
    assert not (book.parent / "cover.png").exists()
    _assert_color(book.with_suffix(".png"), "red", "PNG")


def test_chapter_copy_with_only_volume_two_does_not_invent_a_source(tmp_path: Path) -> None:
    volume = _book(tmp_path / "library" / "Volumes" / "Series" / "Series v02.cbz")
    chapter = _book(tmp_path / "library" / "Chapters" / "Series" / "Series c001.cbz", "green")
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (chapter,), apply=True, series_policy=SeriesCoverPolicy(copy_existing_volume_covers_toggle=True))
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 0)
    assert _snapshot(tmp_path) == before
    assert not volume.with_suffix(".png").exists()


def test_enabled_chapter_cover_gates_publish_chapter_art_without_volume_copy(tmp_path: Path) -> None:
    _book(tmp_path / "library" / "Volumes" / "Series" / "Series v01.cbz", "red")
    chapter = _book(tmp_path / "library" / "Chapters" / "Series" / "Series c001.cbz", "green")
    result = _run(tmp_path, (chapter,), apply=True,
                  cover_policy=CoverPolicy(ArchivePolicy(), chapter_support_toggle=True, extract_chapter_covers=True),
                  series_policy=SeriesCoverPolicy(chapter_support_toggle=True, extract_chapter_covers=True))
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    _assert_color(chapter.with_suffix(".png"), "green", "PNG")
    _assert_color(chapter.parent / "cover.png", "green", "PNG")


@pytest.mark.parametrize("restriction", ["format", "extension"])
def test_root_restrictions_prevent_sidecar_and_series_publication(tmp_path: Path, restriction: str) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    root = LibraryRoot(tmp_path / "library", formats=("chapter",)) if restriction == "format" else LibraryRoot(tmp_path / "library", extensions=(".epub",))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True, roots=(root,))
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 0)
    assert any(decision.reason == restriction + "_restriction" for decision in result.decisions)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_recorded_art_from_unrelated_book_is_not_owned_by_current_series(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    unrelated = _book(tmp_path / "library" / "Unrelated" / "Unrelated v01.cbz", "green")
    poster = book.parent / "poster.png"
    _owned_image(tmp_path, unrelated, poster, _raster("blue"))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.exit_code) == (0, 3)
    assert any(decision.reason == "custom_series_artwork" for decision in result.decisions)
    assert _snapshot(tmp_path) == before


def test_snapshot_staleness_is_typed_even_when_mtime_and_size_are_unchanged(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz", comment=b"a")
    roots = (LibraryRoot(tmp_path / "library"),)
    catalog = _catalog(roots, ParsingPolicy(chapter_support_toggle=True))
    info = book.stat()
    changed = book.read_bytes()[:-1] + b"b"
    book.write_bytes(changed)
    os.utime(book, ns=(info.st_atime_ns, info.st_mtime_ns))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True, roots=roots, catalog=catalog)
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 3)
    assert [(decision.status, decision.reason) for decision in result.decisions] == [("stale_plan", "source_fingerprint_changed")]
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_artwork_hard_link_to_book_is_refused_without_decoding_or_replacement(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    os.link(book, book.with_suffix(".png"))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (book,), apply=True)
    assert (result.committed, result.exit_code) == (0, 3)
    assert [(decision.status, decision.reason) for decision in result.decisions] == [("conflict", "artwork_aliases_book")]
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_unavailable_library_evidence_does_not_become_successful_chapter_copy(tmp_path: Path) -> None:
    chapter = _book(tmp_path / "library" / "Chapters" / "Series" / "Series c001.cbz")
    roots = (LibraryRoot(tmp_path / "library"),)
    catalog = replace(_catalog(roots, ParsingPolicy(chapter_support_toggle=True)),
                      available=False, reason="library_unavailable")
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (chapter,), apply=True, roots=roots, catalog=catalog,
                  series_policy=SeriesCoverPolicy(copy_existing_volume_covers_toggle=True))
    assert (result.planned, result.committed, result.skipped, result.exit_code) == (0, 0, 1, 2)
    assert any(decision.status == "missing_dependency" and decision.reason == "library_unavailable" for decision in result.decisions)
    assert _snapshot(tmp_path) == before


def test_sibling_book_ledger_cannot_authorize_replacing_the_wrong_book_sidecar(tmp_path: Path) -> None:
    first = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    sibling = _book(first.parent / "Series v02.cbz", "green")
    sidecar = first.with_suffix(".png")
    _owned_image(tmp_path, sibling, sidecar, _raster("blue"))
    before = _snapshot(tmp_path)
    result = _run(tmp_path, (first,), apply=True)
    assert (result.planned, result.committed, result.exit_code) == (0, 0, 3)
    assert any(decision.reason == "custom_book_artwork" and decision.candidates == (str(sidecar),) for decision in result.decisions)
    assert _snapshot(tmp_path) == before


def test_generation_assessment_converges_without_rewriting_identical_art(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01.cbz")
    assert _run(tmp_path, (book,), apply=True).committed == 1
    before = fingerprint(book.with_suffix(".png"), tmp_path / "library")
    real_extract = artwork_workflow.extract_cover
    decoded: list[Path] = []

    def observe_decode(path: Path, **kwargs: object):
        decoded.append(path)
        return real_extract(path, **kwargs)

    monkeypatch.setattr(artwork_workflow, "extract_cover", observe_decode)
    assert _run(tmp_path, (book,), apply=True, mode="latest").skipped == 1
    assert decoded == []
    changed = CoverPolicy(ArchivePolicy(), cover_patterns=("a-new-configured-pattern",))
    assessed = _run(tmp_path, (book,), apply=True, mode="latest", cover_policy=changed)
    assert (assessed.committed, assessed.skipped, assessed.exit_code) == (0, 1, 0)
    assert decoded == [book]
    decoded.clear()
    repeated = _run(tmp_path, (book,), apply=True, mode="latest", cover_policy=changed)
    assert (repeated.committed, repeated.skipped, repeated.exit_code) == (0, 1, 0)
    assert decoded == []
    assert fingerprint(book.with_suffix(".png"), tmp_path / "library") == before


def test_artwork_uses_supplied_classification_for_restricted_root(tmp_path: Path) -> None:
    book = _book(tmp_path / "library" / "Series" / "Series v01 (Special).cbz")
    root = LibraryRoot(tmp_path / "library", library_types=("custom",))
    classification = ClassificationPolicy(
        library_types=(LibraryType("custom", (".cbz",), ("Special",), (), 100),),
    )
    result = _run(tmp_path, (book,), apply=True, roots=(root,), classification=classification)
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0)
    _assert_color(book.with_suffix(".png"), "red", "PNG")
    _assert_color(book.parent / "cover.png", "red", "PNG")

from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import zipfile
import zlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event, Thread

import pytest

from komga_cover_extractor.application import conversion_workflow as workflow
from komga_cover_extractor.application.artwork_workflow import extract_artwork
from komga_cover_extractor.application.conversion_workflow import (
    ConversionPlanReport,
    build_conversion_plan,
    convert_releases,
    prepare_conversion,
    release_conversion_artifacts,
)
from komga_cover_extractor.application.convert import observe_header
from komga_cover_extractor.application.header_correction import correct_headers
from komga_cover_extractor.covers.selection import CoverPolicy
from komga_cover_extractor.covers.series_policy import SeriesCoverPolicy
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
)
from komga_cover_extractor.domain.releases import FileObservation, ReleaseSnapshot
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
from komga_cover_extractor.matching.classification import ClassificationPolicy
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release

NATIVE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


@pytest.fixture
def library(tmp_path: Path) -> Path:
    result = tmp_path / "library"
    result.mkdir()
    return result


def _sevenzip(path: Path, entries: dict[str, bytes], *, raw_names: tuple[str, ...] | None = None) -> None:
    py7zr = pytest.importorskip("py7zr", reason="actual 7z fixtures require the conversion extra")
    with py7zr.SevenZipFile(path, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        for name, data in entries.items():
            archive.writestr(data, name)
        if raw_names is not None:
            for entry, raw_name in zip(archive.header.files_info.files, raw_names, strict=True):
                entry["filename"] = raw_name


def _rar(root: Path, name: str, *, destination: str | None = None) -> Path:
    path = root / (destination or name)
    shutil.copyfile(NATIVE_FIXTURES / name, path)
    return path


def _snapshot(path: Path, root: Path) -> ReleaseSnapshot:
    return ReleaseSnapshot(FileObservation(path, fingerprint=fingerprint(path, root)))


def _run(
    root: Path, books: tuple[Path, ...], *, apply: bool = False, interactive: bool = False,
    cancel: Event | None = None, policy: ArchivePolicy | None = None,
):
    return convert_releases(
        tuple(_snapshot(book, root) for book in books), roots=(LibraryRoot(root),),
        archive_policy=policy or ArchivePolicy(), state_dir=root.parent / "state",
        policy_version="conversion-workflow-test", authorization=RunAuthorization(apply=apply),
        cancel=cancel if cancel is not None else Event(), interactive=interactive,
    )


def _tree(root: Path) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.lstat().st_mode, path.lstat().st_mtime_ns,
         hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        for path in (root, *sorted(root.rglob("*")))
    )


def _manifest(entries: dict[str, bytes]) -> list[list[str]]:
    return [[name, hashlib.sha256(data).hexdigest()] for name, data in sorted(entries.items())]

def _png() -> bytes:
    image = pytest.importorskip("PIL.Image", reason="this generated image fixture requires the covers extra")
    output = io.BytesIO()
    image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()



def _assert_originals_retained(store: StateStore, operation_id: str, originals: dict[Path, bytes]) -> None:
    members = store.list_members(operation_id)
    cleanups = tuple(member for member in members if member.kind == "quarantine")
    assert len(cleanups) == len(originals)
    for member in cleanups:
        assert member.status == "complete"
        assert member.source is not None
        assert member.cleanup_path is not None and member.cleanup_fingerprint is not None
        expected = originals[member.source.path]
        assert member.cleanup_path.read_bytes() == expected
        assert member.cleanup_fingerprint.sha256 == hashlib.sha256(expected).hexdigest()
        assert fingerprint(member.cleanup_path, member.cleanup_fingerprint.root) == member.cleanup_fingerprint
        assert not member.source.path.exists()


def test_preview_has_real_manifest_without_changing_library_or_initializing_state(library: Path) -> None:
    book = library / "Series v01.7z"
    entries = {
        "pages/01.txt": b"first generated page\n",
        "pages/01.png": _png(),
        "other/01.txt": b"different nested path with the same basename\n",
        "ComicInfo.xml": b"<ComicInfo><Publisher>Fixture Press</Publisher></ComicInfo>",
        "OPS/package.opf": b'<package xmlns="http://www.idpf.org/2007/opf"/>',
        "assets/LICENSE": b"Synthetic fixture assets.\n",
    }
    _sevenzip(book, entries)
    observed = fingerprint(book, library)
    before = _tree(library)

    result = _run(library, (book,))

    assert result.as_dict()["counts"] == {"planned": 1, "committed": 0, "skipped": 0, "failed": 0}
    assert result.exit_code == 0
    assert result.decisions[0].reason == "verified_conversion_preview_requires_regeneration"
    assert result.plans == ()
    report = result.as_dict()["plans"][0]
    conversion = report["conversion"]
    assert conversion["artifact_purpose"] == "informational"
    assert conversion["container"] == "7z"
    assert conversion["manifest"] == _manifest(entries)
    assert conversion["sources"] == [observed.as_dict()]
    artifact = conversion["artifact"]
    scratch = Path(artifact["root"])
    assert not scratch.is_relative_to(library)
    assert {"path": str(scratch), "write": False} in report["roots"]
    assert report["operations"][0]["artifact"]["sha256"] == artifact["sha256"]
    assert report["operations"][0]["source"] == observed.as_dict()
    assert report["operations"][1]["kind"] == "quarantine"
    assert report["operations"][1]["destination"] == str(book)
    assert report["operations"][1]["depends_on"] == ["book"]
    assert not scratch.exists()
    assert _tree(library) == before
    assert not (library.parent / "state").exists()
    assert not (library.parent / "logs").exists()


def test_preparation_exposes_verified_file_until_context_closes_and_report_is_immutable(library: Path) -> None:
    book = library / "Prepared v01.7z"
    entries = {"pages/01.txt": b"prepared page", "ComicInfo.xml": b"<ComicInfo/>"}
    _sevenzip(book, entries)
    original = fingerprint(book, library)
    header = observe_header(book, root=library, policy=ArchivePolicy())
    roots = (LibraryRoot(library),)

    with prepare_conversion(header, roots=roots, archive_policy=ArchivePolicy(),
                            authorization=RunAuthorization(), cancel=Event()) as prepared:
        assert prepared.artifact_purpose == "informational"
        assert prepared.result.status == "valid"
        artifact = prepared.result.artifact
        assert artifact is not None
        assert fingerprint(artifact.path, artifact.root) == artifact
        assert prepared.result.sources == (original,)
        with zipfile.ZipFile(artifact.path) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == entries
            assert archive.testzip() is None
        report = ConversionPlanReport(build_conversion_plan(prepared, policy_version="prepared-test"), prepared)
        with pytest.raises(FrozenInstanceError):
            report._conversion_json = "{}"
        detached = report.as_dict()
        detached["conversion"]["manifest"].clear()
        assert report.as_dict()["conversion"]["manifest"] == _manifest(entries)
        with pytest.raises(ValueError, match="publication artifact"):
            prepared.retain_for_recovery()

    assert not artifact.path.exists()
    assert fingerprint(book, library) == original
    assert not (library.parent / "state").exists()


def test_apply_publishes_actual_cbz_then_quarantines_source_and_repeat_does_not_rewrite(library: Path) -> None:
    book = library / "Series v01.7z"
    entries = {"pages/01.txt": b"new book page", "pages/01.png": _png(),
               "ComicInfo.xml": b"<ComicInfo/>", "assets/LICENSE": b"synthetic"}
    _sevenzip(book, entries)
    original_bytes = book.read_bytes()
    original_fp = fingerprint(book, library)

    result = _run(library, (book,), apply=True)

    assert result.as_dict()["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert result.exit_code == 0
    target = book.with_suffix(".cbz")
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert {name: archive.read(name) for name in archive.namelist()} == entries
    report = result.plan_reports[0].as_dict()
    assert report["conversion"]["artifact_purpose"] == "publication"
    assert report["conversion"]["manifest"] == _manifest(entries)
    assert report["conversion"]["artifact"]["root"] == str(library)
    assert report["operations"][0]["source"] == original_fp.as_dict()
    assert report["operations"][0]["collision"] == "absent"
    assert not list(library.glob(".kce-convert-*"))
    assert not Path(report["operations"][0]["artifact"]["source"]["root"]).joinpath(
        report["operations"][0]["artifact"]["source"]["relative_path"]
    ).exists()
    store = StateStore(library.parent / "state")
    record = store.get_operation(report["operation_id"])
    assert record is not None and record.phase == "complete"
    _assert_originals_retained(store, record.operation_id, {book: original_bytes})
    events = store.list_outbox(operation_id=record.operation_id)
    assert len(events) == 2
    published = fingerprint(target, library)
    assert published.sha256 == report["operations"][0]["artifact"]["sha256"]

    repeated = _run(library, (target,), apply=True)

    assert repeated.as_dict()["counts"] == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
    assert repeated.decisions[0].reason == "already_zip_container"
    assert fingerprint(target, library) == published
    assert store.list_outbox(operation_id=record.operation_id) == events
    _assert_originals_retained(store, record.operation_id, {book: original_bytes})


def test_apply_real_rar_preserves_nested_unicode_paths_and_empty_directory(library: Path) -> None:
    book = _rar(library, "rar5-subdirs.rar")
    original = book.read_bytes()

    result = _run(library, (book,), apply=True)

    assert result.committed == 1, result.as_dict()
    assert result.failed == 0 and result.exit_code == 0
    with zipfile.ZipFile(book.with_suffix(".cbz")) as archive:
        assert archive.testzip() is None
        assert archive.read("sub/dir1/file1.txt") == b"file1\n"
        assert archive.read("sub/dir2/file2.txt") == b"file2\n"
        assert zlib.crc32(archive.read("sub/with space/long fn.txt")) == 0x71b7247d
        assert zlib.crc32(archive.read("sub/üȵĩöḋè/file.txt")) == 0x2fec89c1
        assert "sub/empty/" in archive.namelist()
    report = result.plan_reports[0].as_dict()
    assert report["conversion"]["container"] == "rar"
    _assert_originals_retained(StateStore(library.parent / "state"), report["operation_id"], {book: original})
    assert not list(library.glob(".kce-convert-*"))


def test_real_rar_archive_and_member_comments_reach_published_cbz_and_report(library: Path) -> None:
    book = _rar(library, "rar3-comment-plain.rar")
    original = book.read_bytes()

    result = _run(library, (book,), apply=True)

    assert result.committed == 1, result.as_dict()
    report = result.plan_reports[0].as_dict()
    assert base64.b64decode(report["conversion"]["comment_base64"], validate=True) == b"RARcomment\n"
    with zipfile.ZipFile(book.with_suffix(".cbz")) as archive:
        assert archive.comment == b"RARcomment\n"
        assert archive.read("file1.txt") == b""
        assert archive.read("file2.txt") == b""
        assert archive.getinfo("file1.txt").comment == b"Comment1v2\n"
        assert archive.getinfo("file2.txt").comment == b"Comment2v2\n"
    _assert_originals_retained(StateStore(library.parent / "state"), report["operation_id"], {book: original})


def test_multipart_rar_is_one_group_and_every_original_part_is_retained(library: Path) -> None:
    parts = tuple(_rar(library, f"rar5-vols.part{index}.rar") for index in (1, 2, 3))
    originals = {path: path.read_bytes() for path in parts}
    original_fps = [fingerprint(path, library).as_dict() for path in parts]
    before = _tree(library)

    preview = _run(library, parts)

    assert preview.as_dict()["counts"] == {"planned": 1, "committed": 0, "skipped": 0, "failed": 0}
    assert preview.plan_reports[0].as_dict()["conversion"]["sources"] == original_fps
    assert _tree(library) == before
    assert not (library.parent / "state").exists()

    result = _run(library, parts, apply=True)

    assert result.as_dict()["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert result.exit_code == 0
    target = parts[0].with_suffix(".cbz")
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == ["vols/bigfile.txt", "vols/smallfile.txt"]
        assert len(archive.read("vols/bigfile.txt")) == 205000
        assert zlib.crc32(archive.read("vols/bigfile.txt")) == 0x509ad74c
        assert len(archive.read("vols/smallfile.txt")) == 2050
        assert zlib.crc32(archive.read("vols/smallfile.txt")) == 0xd08a1f86
    report = result.plan_reports[0].as_dict()
    assert report["conversion"]["sources"] == original_fps
    assert [operation["destination"] for operation in report["operations"] if operation["kind"] == "quarantine"] == [str(part) for part in parts]
    assert len(report["operations"]) == 4
    _assert_originals_retained(StateStore(library.parent / "state"), report["operation_id"], originals)
    assert not list(library.glob(".kce-convert-*"))
    assert not parts[1].with_suffix(".cbz").exists()
    assert not parts[2].with_suffix(".cbz").exists()


@pytest.mark.parametrize("valid_target", [False, True])
@pytest.mark.parametrize("apply", [False, True])
def test_existing_output_is_a_collision_even_if_invalid_and_never_deleted(library: Path, valid_target: bool, apply: bool) -> None:
    book = library / "Collision v01.7z"
    _sevenzip(book, {"pages/01.txt": b"new converted book"})
    target = book.with_suffix(".cbz")
    if valid_target:
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("pages/01.txt", b"different old book")
    else:
        target.write_bytes(b"invalid CBZ bytes are still user-owned data")
    original = fingerprint(book, library)
    prior = fingerprint(target, library)
    before = _tree(library)

    result = _run(library, (book,), apply=apply)

    assert result.as_dict()["counts"] == {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
    assert result.exit_code == 3
    assert any(decision.reason == "conversion_destination_exists" for decision in result.decisions)
    assert fingerprint(book, library) == original
    assert fingerprint(target, library) == prior
    assert _tree(library) == before
    assert not (library.parent / "state").exists()


class _NoInput(io.StringIO):
    def readline(self, *args, **kwargs):
        raise AssertionError("noninteractive conversion must not read stdin")


def test_interactive_flag_without_terminal_does_not_read_stdin(library: Path, monkeypatch) -> None:
    book = library / "Collision v01.7z"
    _sevenzip(book, {"page.txt": b"new"})
    target = book.with_suffix(".cbz")
    target.write_bytes(b"retain custom target")
    before = _tree(library)
    monkeypatch.setattr(sys, "stdin", _NoInput())

    result = _run(library, (book,), apply=True, interactive=True)

    assert result.exit_code == 3
    assert result.committed == 0
    assert _tree(library) == before
    assert not (library.parent / "state").exists()


def test_explicit_terminal_replace_retains_prior_target_and_original_source(library: Path, monkeypatch) -> None:
    book = library / "Replace v01.7z"
    entries = {"pages/01.txt": b"new selected book", "ComicInfo.xml": b"<ComicInfo/>"}
    _sevenzip(book, entries)
    original = book.read_bytes()
    target = book.with_suffix(".cbz")
    old_target = b"an invalid old CBZ remains recoverable after explicit replacement"
    target.write_bytes(old_target)
    old_fp = fingerprint(target, library)
    master, slave = os.openpty()
    try:
        with os.fdopen(os.dup(slave), "r") as terminal, os.fdopen(os.dup(slave), "w") as diagnostics:
            monkeypatch.setattr(sys, "stdin", terminal)
            monkeypatch.setattr(sys, "stderr", diagnostics)
            os.write(master, b"replace\n")
            result = _run(library, (book,), apply=True, interactive=True)
    finally:
        os.close(master)
        os.close(slave)

    assert result.as_dict()["counts"] == {"planned": 1, "committed": 1, "skipped": 0, "failed": 0}
    assert result.exit_code == 0
    report = result.plan_reports[0].as_dict()
    assert report["operations"][0]["collision"] == "replace"
    assert report["operations"][0]["destination_before"] == old_fp.as_dict()
    with zipfile.ZipFile(target) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == entries
    store = StateStore(library.parent / "state")
    book_member = next(member for member in store.list_members(report["operation_id"]) if member.member_id == "book")
    assert book_member.backup_path is not None and book_member.backup_fingerprint is not None
    assert book_member.backup_path.read_bytes() == old_target
    assert book_member.backup_fingerprint.sha256 == old_fp.sha256
    _assert_originals_retained(store, report["operation_id"], {book: original})
    assert not list(library.glob(".kce-convert-*"))


@pytest.mark.parametrize("apply", [False, True])
def test_verified_identical_cbz_is_not_rewritten_and_does_not_retire_any_source(library: Path, apply: bool) -> None:
    book = library / "Identical v01.7z"
    _sevenzip(book, {"pages/01.txt": b"same book", "ComicInfo.xml": b"<ComicInfo/>"})
    source = fingerprint(book, library)
    header = observe_header(book, root=library, policy=ArchivePolicy())
    target = book.with_suffix(".cbz")
    with prepare_conversion(header, roots=(LibraryRoot(library),), archive_policy=ArchivePolicy(),
                            authorization=RunAuthorization(), cancel=Event()) as prepared:
        assert prepared.result.artifact is not None
        shutil.copyfile(prepared.result.artifact.path, target)
    before = _tree(library)
    target_fp = fingerprint(target, library)

    result = _run(library, (book,), apply=apply)

    assert result.as_dict()["counts"] == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
    assert result.exit_code == 0
    assert result.decisions[0].reason == "verified_identical_cbz_sources_retained"
    assert fingerprint(book, library) == source
    assert fingerprint(target, library) == target_fp
    assert _tree(library) == before
    assert not (library.parent / "state").exists()


@pytest.mark.parametrize("epub", [False, True])
def test_zip_and_epub_headers_skip_without_conversion_or_extension_correction(library: Path, epub: bool) -> None:
    book = library / "Wrong extension.cbr"
    with zipfile.ZipFile(book, "w") as archive:
        if epub:
            archive.writestr("mimetype", b"application/epub+zip")
            archive.writestr("META-INF/container.xml", b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr("OPS/package.opf", b'<package xmlns="http://www.idpf.org/2007/opf"><manifest/></package>')
        else:
            archive.writestr("page.txt", b"ordinary ZIP comic")
    before = _tree(library)

    result = _run(library, (book,), apply=True)

    assert result.as_dict()["counts"] == {"planned": 0, "committed": 0, "skipped": 1, "failed": 0}
    assert result.decisions[0].reason == ("epub_not_converted" if epub else "already_zip_container")
    assert _tree(library) == before
    assert not book.with_suffix(".cbz").exists()
    assert not book.with_suffix(".epub").exists()
    assert not (library.parent / "state").exists()


def test_native_archive_named_cbz_requires_separate_header_correction(library: Path) -> None:
    book = _rar(library, "rar5-subdirs.rar", destination="native-content.cbz")
    before = _tree(library)

    result = _run(library, (book,), apply=True)

    assert result.exit_code == 3
    assert result.planned == 0 and result.committed == 0
    assert result.decisions[0].reason == "header_correction_required"
    assert _tree(library) == before
    assert not (library.parent / "state").exists()


def test_multiple_selected_native_archives_with_one_destination_conflict_before_any_apply(library: Path) -> None:
    first, second = library / "Same v01.7z", library / "Same v01.cbr"
    _sevenzip(first, {"page.txt": b"first native archive"})
    _sevenzip(second, {"page.txt": b"different native archive"})
    before = _tree(library)

    result = _run(library, (first, second), apply=True)

    assert result.exit_code == 3
    assert result.planned == 0 and result.committed == 0
    assert [decision.reason for decision in result.decisions] == [
        "multiple_archives_share_conversion_destination", "multiple_archives_share_conversion_destination",
    ]
    assert _tree(library) == before
    assert not (library.parent / "state").exists()


def test_same_size_same_mtime_source_change_fails_before_private_directory_creation(library: Path) -> None:
    book = library / "Stale v01.7z"
    _sevenzip(book, {"page.txt": b"original page"})
    snapshot = _snapshot(book, library)
    observed = snapshot.observation.fingerprint
    assert observed is not None
    changed = bytearray(book.read_bytes())
    changed[-1] ^= 1
    book.write_bytes(changed)
    os.utime(book, ns=(observed.mtime_ns, observed.mtime_ns))
    before = _tree(library)

    result = convert_releases((snapshot,), roots=(LibraryRoot(library),), archive_policy=ArchivePolicy(),
                              state_dir=library.parent / "state", policy_version="stale-test",
                              authorization=RunAuthorization(apply=True), cancel=Event())

    assert result.exit_code == 3
    assert result.decisions[0].status == "stale_plan"
    assert result.planned == 0 and result.committed == 0
    assert _tree(library) == before
    assert not list(library.glob(".kce-convert-*"))
    assert not (library.parent / "state").exists()


def test_source_outside_component_root_is_rejected_without_preparation(library: Path) -> None:
    outside = library.with_name("library-old")
    outside.mkdir()
    book = outside / "Outside v01.7z"
    _sevenzip(book, {"page.txt": b"outside content"})
    snapshot = _snapshot(book, outside)
    before = _tree(outside)

    result = convert_releases((snapshot,), roots=(LibraryRoot(library),), archive_policy=ArchivePolicy(),
                              state_dir=library.parent / "state", policy_version="root-test",
                              authorization=RunAuthorization(apply=True), cancel=Event())

    assert result.committed == 0 and result.planned == 0
    assert result.decisions[0].reason == "outside_selected_roots"
    assert _tree(outside) == before
    assert list(library.iterdir()) == []
    assert not (library.parent / "state").exists()


def test_symlink_parent_is_rejected_before_native_preparation(library: Path) -> None:
    outside = library.parent / "outside"
    outside.mkdir()
    book = outside / "Symlink v01.7z"
    _sevenzip(book, {"page.txt": b"outside source bytes"})
    source = fingerprint(book, outside)
    alias = library / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    path = alias / book.name
    observation = replace(source, root=library, relative_path=f"alias/{book.name}")
    snapshot = ReleaseSnapshot(FileObservation(path, fingerprint=observation))

    result = convert_releases((snapshot,), roots=(LibraryRoot(library),), archive_policy=ArchivePolicy(),
                              state_dir=library.parent / "state", policy_version="root-test",
                              authorization=RunAuthorization(apply=True), cancel=Event())

    assert result.committed == 0 and result.planned == 0
    assert result.decisions[0].reason == "symlink_traversal"
    assert fingerprint(book, outside) == source
    assert not list(library.glob(".kce-convert-*"))
    assert not (library.parent / "state").exists()


@pytest.mark.parametrize("case", ["incomplete_rar", "malformed_7z", "traversal_7z", "quota_7z"])
def test_invalid_input_retains_original_without_cbz_journal_or_false_success(library: Path, case: str) -> None:
    policy = ArchivePolicy()
    if case == "incomplete_rar":
        book = _rar(library, "rar5-vols.part1.rar")
        expected = "incomplete_multipart_archive"
    else:
        book = library / "Invalid v01.7z"
        _sevenzip(book, {"page.txt": b"abcdef"}, raw_names=("../outside.txt",) if case == "traversal_7z" else None)
        if case == "malformed_7z":
            book.write_bytes(book.read_bytes()[:20])
            expected = None
        elif case == "quota_7z":
            policy = replace(policy, conversion_member_max_bytes=5)
            expected = "archive_member_limit"
        else:
            expected = "unsafe_member_name"
    original = fingerprint(book, library)

    result = _run(library, (book,), apply=True, policy=policy)

    assert result.committed == 0 and result.planned == 0
    assert result.exit_code == 2, result.as_dict()
    assert any(decision.status == "invalid_input" for decision in result.decisions)
    if expected is not None:
        assert any(decision.reason == expected for decision in result.decisions), result.as_dict()
    assert fingerprint(book, library) == original
    assert not book.with_suffix(".cbz").exists()
    assert not (library.parent / "outside.txt").exists()
    assert not (library.parent / "state").exists()
    assert not list(library.glob(".kce-convert-*"))


def test_missing_real_unrar_is_a_capability_failure_with_original_retained(library: Path, monkeypatch) -> None:
    book = _rar(library, "rar5-subdirs.rar")
    original = fingerprint(book, library)
    monkeypatch.setenv("PATH", str(library.parent / "no-tools"))

    result = _run(library, (book,), apply=True)

    assert result.committed == 0 and result.planned == 0
    assert result.exit_code == 2
    assert any(decision.status == "missing_dependency" and decision.reason == "unrar_native_tool_required"
               for decision in result.decisions)
    assert fingerprint(book, library) == original
    assert not book.with_suffix(".cbz").exists()
    assert not (library.parent / "state").exists()


def test_changed_multipart_part_is_revalidated_by_actual_executor_before_publication(library: Path, monkeypatch) -> None:
    parts = tuple(_rar(library, f"rar5-vols.part{index}.rar") for index in (1, 2, 3))
    originals = {part: part.read_bytes() for part in parts}
    original_fp = fingerprint(parts[1], library)
    changed = bytearray(originals[parts[1]])
    changed[-1] ^= 1

    class ChangedPartExecutor(TransactionExecutor):
        def apply(self, plan, *, authorization):
            parts[1].write_bytes(changed)
            os.utime(parts[1], ns=(original_fp.mtime_ns, original_fp.mtime_ns))
            return super().apply(plan, authorization=authorization)

    monkeypatch.setattr(workflow, "TransactionExecutor", ChangedPartExecutor)

    result = _run(library, parts, apply=True)

    assert result.committed == 0
    assert any(decision.status == "stale_plan" for decision in result.decisions), result.as_dict()
    assert parts[0].read_bytes() == originals[parts[0]]
    assert parts[1].read_bytes() == bytes(changed)
    assert parts[2].read_bytes() == originals[parts[2]]
    assert not parts[0].with_suffix(".cbz").exists()
    assert not (library.parent / "state").exists()
    assert not list(library.glob(".kce-convert-*"))


def test_failed_actual_publication_retains_verified_stage_for_recorded_recovery(library: Path, monkeypatch) -> None:
    book = library / "Failure v01.7z"
    entries = {"pages/01.txt": b"recover this book", "ComicInfo.xml": b"<ComicInfo/>"}
    _sevenzip(book, entries)
    original = book.read_bytes()

    def fail_before_publish(phase: str, member: str) -> None:
        if phase == "before_publish" and member == "book":
            raise OSError(errno.EIO, "injected publication failure")

    class FailedExecutor(TransactionExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, checkpoint=fail_before_publish, **kwargs)

    monkeypatch.setattr(workflow, "TransactionExecutor", FailedExecutor)
    result = _run(library, (book,), apply=True)

    assert result.committed == 0 and result.failed == 1
    assert result.exit_code == 1
    assert book.read_bytes() == original
    assert not book.with_suffix(".cbz").exists()
    report = result.plan_reports[0].as_dict()
    store = StateStore(library.parent / "state")
    record = store.get_operation(report["operation_id"])
    assert record is not None
    artifact = record.plan.operations[0].artifact
    assert artifact is not None and artifact.source is not None
    assert fingerprint(artifact.source.path, library) == artifact.source
    assert artifact.source.path.parent.name.startswith(".kce-convert-")
    assert (artifact.source.path.parent / ".owner.json").is_file()
    assert store.list_outbox(operation_id=record.operation_id) == ()

    recovered = TransactionExecutor(library.parent / "state", (library,)).recover(
        record.operation_id, authorization=RunAuthorization(apply=True),
    )
    assert recovered.committed == 1 and recovered.failed == 0
    assert release_conversion_artifacts(record.plan, roots=(LibraryRoot(library),),
                                        state_dir=library.parent / "state",
                                        authorization=RunAuthorization(apply=True)) is None
    with zipfile.ZipFile(book.with_suffix(".cbz")) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == entries
    _assert_originals_retained(store, record.operation_id, {book: original})
    assert not artifact.source.path.parent.exists()


def test_postcommit_cleanup_failure_counts_commit_once_and_keeps_recovery_artifact(library: Path, monkeypatch) -> None:
    book = library / "Cleanup v01.7z"
    _sevenzip(book, {"page.txt": b"locally committed book"})
    original = book.read_bytes()

    def fail_cleanup(phase: str, member: str) -> None:
        if phase == "before_cleanup":
            raise OSError(errno.EIO, "injected source cleanup failure")

    class CleanupFailureExecutor(TransactionExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, checkpoint=fail_cleanup, **kwargs)

    monkeypatch.setattr(workflow, "TransactionExecutor", CleanupFailureExecutor)
    result = _run(library, (book,), apply=True)

    assert result.committed == 1 and result.failed == 0
    assert result.exit_code == 3
    assert any(decision.reason == "cleanup_pending" for decision in result.decisions)
    target_fp = fingerprint(book.with_suffix(".cbz"), library)
    assert book.read_bytes() == original
    report = result.plan_reports[0].as_dict()
    store = StateStore(library.parent / "state")
    record = store.get_operation(report["operation_id"])
    assert record is not None and record.phase == "cleanup_pending"
    artifact = record.plan.operations[0].artifact
    assert artifact is not None and artifact.source is not None
    assert artifact.source.path.exists()
    events = store.list_outbox(operation_id=record.operation_id)
    assert len(events) == 2
    unauthorized_cleanup = release_conversion_artifacts(record.plan, roots=(LibraryRoot(library),),
                                                        state_dir=library.parent / "state", authorization=RunAuthorization())
    assert unauthorized_cleanup is not None and unauthorized_cleanup.status == "planned"
    assert artifact.source.path.exists()
    premature = release_conversion_artifacts(record.plan, roots=(LibraryRoot(library),),
                                             state_dir=library.parent / "state", authorization=RunAuthorization(apply=True))
    assert premature is not None and premature.reason == "conversion_group_not_settled"
    assert artifact.source.path.exists()

    recovery = TransactionExecutor(library.parent / "state", (library,)).recover(
        record.operation_id, authorization=RunAuthorization(apply=True),
    )
    assert recovery.committed == 0 and recovery.failed == 0
    assert release_conversion_artifacts(record.plan, roots=(LibraryRoot(library),),
                                        state_dir=library.parent / "state", authorization=RunAuthorization(apply=True)) is None
    assert fingerprint(book.with_suffix(".cbz"), library) == target_fp
    assert store.list_outbox(operation_id=record.operation_id) == events
    _assert_originals_retained(store, record.operation_id, {book: original})
    assert not artifact.source.path.parent.exists()


@pytest.mark.parametrize("phase", ["published", "group_committed", "before_cleanup"])
def test_real_process_conversion_crash_recovers_in_fresh_process_without_republish(library: Path, phase: str) -> None:
    book = library / "Crash v01.7z"
    entries = {"pages/01.txt": b"recover a real native conversion", "ComicInfo.xml": b"<ComicInfo/>"}
    _sevenzip(book, entries)
    original = book.read_bytes()
    state = library.parent / "state"
    crash_script = textwrap.dedent(f"""
        import os
        from pathlib import Path
        from threading import Event
        from komga_cover_extractor.application import conversion_workflow as workflow
        from komga_cover_extractor.domain.decisions import RunAuthorization
        from komga_cover_extractor.domain.libraries import LibraryRoot
        from komga_cover_extractor.domain.releases import FileObservation, ReleaseSnapshot
        from komga_cover_extractor.infrastructure.filesystem import fingerprint
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
        from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
        root, book, state = Path({str(library)!r}), Path({str(book)!r}), Path({str(state)!r})
        def checkpoint(phase, member):
            if phase == {phase!r}:
                os._exit(73)
        class CrashExecutor(TransactionExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, checkpoint=checkpoint, **kwargs)
        workflow.TransactionExecutor = CrashExecutor
        observed = ReleaseSnapshot(FileObservation(book, fingerprint=fingerprint(book, root)))
        workflow.convert_releases((observed,), roots=(LibraryRoot(root),), archive_policy=ArchivePolicy(),
            state_dir=state, policy_version='crash-conversion-test',
            authorization=RunAuthorization(apply=True), cancel=Event())
        raise SystemExit(1)
    """)

    crashed = subprocess.run([sys.executable, "-I", "-c", crash_script], cwd=library.parent,
                             capture_output=True, text=True, timeout=30, check=False)

    assert crashed.returncode == 73, crashed.stderr or crashed.stdout
    assert book.read_bytes() == original
    target = book.with_suffix(".cbz")
    published = fingerprint(target, library)
    store = StateStore(state)
    records = store.list_operations()
    assert len(records) == 1
    record = records[0]
    artifact = record.plan.operations[0].artifact
    assert artifact is not None and artifact.source is not None
    assert artifact.source.path.exists()
    recovery_script = textwrap.dedent(f"""
        import json
        from pathlib import Path
        from komga_cover_extractor.application.conversion_workflow import release_conversion_artifacts
        from komga_cover_extractor.domain.decisions import RunAuthorization
        from komga_cover_extractor.domain.libraries import LibraryRoot
        from komga_cover_extractor.infrastructure.state import StateStore
        from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
        root, state = Path({str(library)!r}), Path({str(state)!r})
        authorization = RunAuthorization(apply=True)
        result = TransactionExecutor(state, (root,)).recover({record.operation_id!r}, authorization=authorization)
        saved = StateStore(state).get_operation({record.operation_id!r})
        assert saved is not None
        settlement = release_conversion_artifacts(saved.plan, roots=(LibraryRoot(root),), state_dir=state,
                                                  authorization=authorization)
        print(json.dumps({{'result': result.as_dict(), 'settlement': settlement.as_dict() if settlement else None}}))
    """)
    first = subprocess.run([sys.executable, "-I", "-c", recovery_script], cwd=library.parent,
                           capture_output=True, text=True, timeout=30, check=False)
    assert first.returncode == 0, first.stderr or first.stdout
    observed = json.loads(first.stdout)
    assert observed["result"]["counts"]["committed"] == (1 if phase == "published" else 0)
    assert observed["result"]["exit_code"] == 0
    assert observed["settlement"] is None
    assert fingerprint(target, library) == published
    with zipfile.ZipFile(target) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == entries
    _assert_originals_retained(store, record.operation_id, {book: original})
    assert not artifact.source.path.parent.exists()
    events = store.list_outbox(operation_id=record.operation_id)

    second = subprocess.run([sys.executable, "-I", "-c", recovery_script], cwd=library.parent,
                            capture_output=True, text=True, timeout=30, check=False)
    assert second.returncode == 0, second.stderr or second.stdout
    assert json.loads(second.stdout)["result"]["counts"]["committed"] == 0
    assert json.loads(second.stdout)["settlement"] is None
    assert fingerprint(target, library) == published
    assert store.list_outbox(operation_id=record.operation_id) == events


def test_cancel_before_work_is_readonly_and_does_not_start_a_native_worker(library: Path) -> None:
    book = library / "Cancelled v01.7z"
    _sevenzip(book, {"page.txt": b"cancelled original"})
    before = _tree(library)
    cancel = Event()
    cancel.set()

    result = _run(library, (book,), apply=True, cancel=cancel)

    assert result.as_dict()["counts"] == {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
    assert result.exit_code == 3
    assert result.decisions[0].status == "cancelled"
    assert _tree(library) == before
    assert not (library.parent / "state").exists()


def test_cancellation_during_native_decode_reaps_native_descendants_and_preserves_book(library: Path, monkeypatch) -> None:
    book = _rar(library, "rar5-solid.rar")
    original = fingerprint(book, library)
    real_unrar = shutil.which("unrar")
    assert real_unrar is not None, "licensed real unrar is required for this native cancellation proof"
    tools = library.parent / "slow-native"
    tools.mkdir()
    marker = tools / "native-pids.json"
    executable = tools / "unrar"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        f"real = {real_unrar!r}\n"
        "if '-?' in sys.argv[1:]: os.execv(real, [real, *sys.argv[1:]])\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"with open({str(marker)!r}, 'w') as stream: json.dump([os.getpid(), child.pid], stream)\n"
        "time.sleep(30)\n", encoding="utf-8",
    )
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tools))
    cancel = Event()

    def request_cancel_after_native_start() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                pids = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(.01)
                continue
            if isinstance(pids, list) and len(pids) == 2:
                break
        cancel.set()

    trigger = Thread(target=request_cancel_after_native_start)
    trigger.start()
    started = time.monotonic()
    try:
        result = _run(library, (book,), apply=True, cancel=cancel)
    finally:
        cancel.set()
        trigger.join(timeout=12)

    assert not trigger.is_alive()
    assert time.monotonic() - started < 15
    assert marker.is_file(), "the injected native decoder must actually start before cancellation"
    for pid in json.loads(marker.read_text(encoding="utf-8")):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert any(decision.status == "cancelled" for decision in result.decisions), result.as_dict()
    assert result.committed == 0 and result.planned == 0
    assert fingerprint(book, library) == original
    assert not book.with_suffix(".cbz").exists()
    assert not (library.parent / "state").exists()


@pytest.mark.parametrize("boundary", ["staged", "before_publish", "published"])
def test_nonprimary_rar_part_changed_during_staging_or_publication_preserves_every_part(
    library: Path, monkeypatch, boundary: str,
) -> None:
    parts = tuple(_rar(library, f"rar5-vols.part{index}.rar") for index in (1, 2, 3))
    originals = {part: part.read_bytes() for part in parts}
    observed = fingerprint(parts[1], library)
    changed = bytearray(originals[parts[1]])
    changed[-1] ^= 1

    def change_part_at_publication(phase: str, member: str) -> None:
        if phase == boundary and member == "book":
            parts[1].write_bytes(changed)
            os.utime(parts[1], ns=(observed.mtime_ns, observed.mtime_ns))

    class BoundaryChangeExecutor(TransactionExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, checkpoint=change_part_at_publication, **kwargs)

    monkeypatch.setattr(workflow, "TransactionExecutor", BoundaryChangeExecutor)

    result = _run(library, parts, apply=True)

    assert result.committed == 0, result.as_dict()
    assert result.exit_code == 3
    assert any(decision.status in {"stale_plan", "review_required"} for decision in result.decisions)
    assert parts[0].read_bytes() == originals[parts[0]]
    assert parts[1].read_bytes() == bytes(changed)
    assert parts[2].read_bytes() == originals[parts[2]]
    assert not parts[0].with_suffix(".cbz").exists()
    store = StateStore(library.parent / "state")
    record = store.get_operation(result.plan_reports[0].operation_id)
    assert record is not None and record.phase == "needs_review"
    assert store.list_outbox(operation_id=record.operation_id) == ()
    assert not tuple(member.cleanup_path for member in store.list_members(record.operation_id) if member.cleanup_path)
    artifact = record.plan.operations[0].artifact
    assert artifact is not None and artifact.source is not None
    assert artifact.source.path.parent.name.startswith(".kce-convert-")
    assert fingerprint(artifact.source.path, library) == artifact.source
    with zipfile.ZipFile(artifact.source.path) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == ["vols/bigfile.txt", "vols/smallfile.txt"]


def test_corrupt_existing_state_fails_closed_before_owned_stage_preparation(library: Path) -> None:
    book = library / "State v01.7z"
    _sevenzip(book, {"page.txt": b"preserve this book"})
    state = library.parent / "state"
    state.mkdir()
    database = state / "state.sqlite3"
    invalid_database = b"this is not a valid journal, and must not be replaced"
    database.write_bytes(invalid_database)
    before = _tree(library)

    result = _run(library, (book,), apply=True)

    assert result.exit_code == 3
    assert result.as_dict()["counts"] == {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
    assert result.decisions[0].reason == "conversion_state_invalid"
    assert _tree(library) == before
    assert database.read_bytes() == invalid_database
    assert not (state / "apply.lock").exists()


def test_changed_owned_marker_after_commit_preserves_stage_for_review(library: Path, monkeypatch) -> None:
    book = library / "Marker v01.7z"
    _sevenzip(book, {"page.txt": b"published book"})
    original = book.read_bytes()

    def alter_marker(phase: str, member: str) -> None:
        if phase == "group_committed":
            markers = tuple(library.glob(".kce-convert-*/.owner.json"))
            assert len(markers) == 1
            markers[0].write_text('{"format":"not-owned-by-this-operation"}', encoding="utf-8")

    class ChangedMarkerExecutor(TransactionExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, checkpoint=alter_marker, **kwargs)

    monkeypatch.setattr(workflow, "TransactionExecutor", ChangedMarkerExecutor)

    result = _run(library, (book,), apply=True)

    assert result.committed == 1 and result.failed == 0
    assert result.exit_code == 3
    assert any(decision.reason == "conversion_marker_identity_mismatch" for decision in result.decisions)
    store = StateStore(library.parent / "state")
    record = store.get_operation(result.plan_reports[0].operation_id)
    assert record is not None and record.phase == "complete"
    artifact = record.plan.operations[0].artifact
    assert artifact is not None and artifact.source is not None
    assert fingerprint(artifact.source.path, library) == artifact.source
    assert (artifact.source.path.parent / ".owner.json").read_text(encoding="utf-8") == '{"format":"not-owned-by-this-operation"}'
    _assert_originals_retained(store, record.operation_id, {book: original})


@pytest.mark.parametrize(("suffix", "epub", "expected"), [(".cbr", False, ".cbz"), (".zip", True, ".epub")])
def test_header_correction_projects_preview_then_preserves_bytes_and_same_stem_artwork(library: Path, suffix: str, epub: bool, expected: str) -> None:
    book = library / ("Series v01" + suffix)
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("mimetype" if epub else "page.txt", b"application/epub+zip" if epub else b"archive asset")
    sidecar = book.with_suffix(".jpg")
    sidecar.write_bytes(b"custom artwork")
    original = book.read_bytes()
    state = library.parent / "state"
    arguments = dict(
        roots=(LibraryRoot(library, roles=("download",)),), parsing=ParsingPolicy(),
        archive_policy=ArchivePolicy(), state_dir=state, policy_version="header-regression",
        cancel=Event(), manual_rename=False,
    )
    preview = correct_headers((_snapshot(book, library),), authorization=RunAuthorization(), **arguments)
    destination = book.with_suffix(expected)
    assert preview.releases[0].path == destination and preview.releases[0].observation.fingerprint.path == book
    assert not destination.exists() and not state.exists()
    assert book.read_bytes() == original and sidecar.read_bytes() == b"custom artwork"
    applied = correct_headers((_snapshot(book, library),), authorization=RunAuthorization(apply=True), **arguments)
    assert applied.committed == 1 and not book.exists()
    assert destination.read_bytes() == original and sidecar.read_bytes() == b"custom artwork"
    assert applied.releases[0].observation.fingerprint.path == destination


@pytest.mark.parametrize("manual", [False, True])
def test_header_collision_or_unanswered_manual_choice_never_changes_archives(library: Path, manual: bool) -> None:
    book = library / "Series v01.cbr"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("page.txt", b"source")
    destination = book.with_suffix(".cbz")
    if not manual:
        destination.write_bytes(b"unrelated existing target")
    before = {path: path.read_bytes() for path in library.iterdir()}
    state = library.parent / "state"
    result = correct_headers(
        (_snapshot(book, library),), roots=(LibraryRoot(library),), parsing=ParsingPolicy(),
        archive_policy=ArchivePolicy(), state_dir=state, policy_version="header-regression",
        authorization=RunAuthorization(apply=True), cancel=Event(), manual_rename=manual,
    )
    assert result.committed == 0 and result.exit_code == 3
    assert {path: path.read_bytes() for path in library.iterdir()} == before
    assert not state.exists()


def test_followup_extraction_recognizes_generated_art_owned_by_converted_book(library: Path) -> None:
    folder = library / "Series"
    folder.mkdir()
    source = folder / "Series v01.7z"
    image = _png()
    _sevenzip(source, {"001.png": image})
    roots = (LibraryRoot(library),)
    state = library.parent / "state"
    authorization = RunAuthorization(apply=True)
    cancel = Event()
    archive = ArchivePolicy()
    header = observe_header(source, root=library, policy=archive)
    with prepare_conversion(header, roots=roots, archive_policy=archive, authorization=authorization, cancel=cancel) as prepared:
        base = build_conversion_plan(prepared, policy_version="converted-art-regression")
        plan = OperationPlan(base.policy_version, base.operation_id, (
            *base.operations,
            Operation("series", "publish", folder / "cover.png", source=header.source,
                      artifact=ArtifactSpec(content=image, media_type="image/png"),
                      depends_on=("book",), reason="generated_series_artwork"),
        ), base.roots)
        prepared.retain_for_recovery()
        assert TransactionExecutor(state, (library,)).apply(plan, authorization=authorization).committed == 1
        assert release_conversion_artifacts(plan, roots=roots, state_dir=state, authorization=authorization) is None
    book = source.with_suffix(".cbz")
    parsing = ParsingPolicy()
    release = parse_release(FileObservation(book, siblings=(book,), root_role="library",
                                           fingerprint=fingerprint(book, library)), parsing)
    original = book.read_bytes()
    result = extract_artwork(
        (release,), LibrarySnapshot((release,), roots), roots=roots, parsing=parsing,
        cover_policy=CoverPolicy(archive_policy=archive, output_covers_as_webp=True,
                                 compare_detected_cover_to_blank_images=False),
        series_policy=SeriesCoverPolicy(), state_dir=state, policy_version="converted-art-followup",
        authorization=authorization, cancel=cancel, classification=ClassificationPolicy(),
    )
    assert result.committed == 1
    codec = pytest.importorskip("PIL.Image")
    with codec.open(folder / "cover.webp") as generated:
        assert generated.format == "WEBP" and generated.size == (8, 8)
        red, green, blue = generated.convert("RGB").getpixel((0, 0))
        assert red > 240 and green < 10 and blue < 10
    assert not (folder / "cover.png").exists()
    assert book.read_bytes() == original

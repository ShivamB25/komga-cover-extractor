from __future__ import annotations

import os
import shutil
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from komga_cover_extractor.application.convert import convert_book
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy

NATIVE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def test_actual_7z_timestamps_survive_verified_cbz_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    py7zr = pytest.importorskip("py7zr", reason="actual timestamp conversion needs the conversion extra")
    monkeypatch.setenv("TZ", "UTC")
    library, scratch, sources = (tmp_path / name for name in ("library", "scratch", "sources"))
    for directory in (library, scratch, sources):
        directory.mkdir()
    page = sources / "page.txt"
    page.write_bytes(b"original page bytes\n")
    metadata = sources / "ComicInfo.xml"
    metadata.write_bytes(b"<ComicInfo/>\n")
    empty = sources / "empty"
    empty.mkdir()
    for path, timestamp in (
        (page, datetime(2020, 7, 20, 19, 1, 33, tzinfo=UTC)),
        (metadata, datetime(2010, 11, 2, 10, 3, 25, tzinfo=UTC)),
        (empty, datetime(2015, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ):
        os.utime(path, (timestamp.timestamp(), timestamp.timestamp()))
    book = library / "Series v01.7z"
    with py7zr.SevenZipFile(book, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.write(page, "pages/01.txt")
        archive.write(metadata, "ComicInfo.xml")
        archive.write(empty, "empty")
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == "valid", result.reason
    assert result.artifact is not None
    target = library / "Series v01.cbz"
    plan = OperationPlan("timestamp-test", "publish-timestamp-test", (
        Operation("book", "publish", target, source=result.sources[0],
                  artifact=ArtifactSpec(source=result.artifact, media_type="application/zip")),
    ), (RootPermission(library), RootPermission(scratch, write=False)))
    applied = TransactionExecutor(tmp_path / "state", (library, scratch)).apply(
        plan, authorization=RunAuthorization(apply=True),
    )
    assert (applied.committed, applied.failed, applied.exit_code) == (1, 0, 0)
    with zipfile.ZipFile(target) as archive:
        assert archive.getinfo("pages/01.txt").date_time == (2020, 7, 20, 19, 1, 32)
        assert archive.getinfo("ComicInfo.xml").date_time == (2010, 11, 2, 10, 3, 24)
        assert archive.getinfo("empty/").date_time == (2015, 1, 2, 3, 4, 4)
        assert archive.read("pages/01.txt") == b"original page bytes\n"
        assert archive.read("ComicInfo.xml") == b"<ComicInfo/>\n"
        assert archive.testzip() is None
    assert book.read_bytes() == original


@pytest.mark.parametrize(("timestamp", "expected"), [
    (datetime(1979, 12, 31, 23, 59, 59, tzinfo=UTC), (1980, 1, 1, 0, 0, 0)),
    (datetime(2108, 1, 1, tzinfo=UTC), (2107, 12, 31, 23, 59, 58)),
    (None, (1980, 1, 1, 0, 0, 0)),
    ((1 << 64) - 1, (2107, 12, 31, 23, 59, 58)),
])
def test_actual_7z_missing_and_out_of_zip_range_dates_remain_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    timestamp: datetime | int | None, expected: tuple[int, int, int, int, int, int],
) -> None:
    py7zr = pytest.importorskip("py7zr", reason="actual timestamp conversion needs the conversion extra")
    from py7zr.helpers import ArchiveTimestamp

    monkeypatch.setenv("TZ", "UTC")
    library, scratch = tmp_path / "library", tmp_path / "scratch"
    library.mkdir()
    scratch.mkdir()
    book = library / "Series v01.7z"
    stored = ArchiveTimestamp.from_datetime(timestamp.timestamp()) if isinstance(timestamp, datetime) else timestamp
    with py7zr.SevenZipFile(book, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.writestr(b"dated page bytes", "pages/01.txt")
        archive.header.files_info.files[-1]["lastwritetime"] = stored
        archive.writestr(b"<ComicInfo/>", "ComicInfo.xml")
        archive.header.files_info.files[-1]["lastwritetime"] = ArchiveTimestamp.from_datetime(
            datetime(2020, 7, 20, 19, 1, 32, tzinfo=UTC).timestamp(),
        )
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == "valid", result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.getinfo("pages/01.txt").date_time == expected
        assert archive.getinfo("ComicInfo.xml").date_time == (2020, 7, 20, 19, 1, 32)
        assert archive.read("pages/01.txt") == b"dated page bytes"
        assert archive.read("ComicInfo.xml") == b"<ComicInfo/>"
        assert archive.testzip() is None
    assert book.read_bytes() == original
    assert not book.with_suffix(".cbz").exists()


def test_actual_7z_instant_uses_legacy_local_zip_civil_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    py7zr = pytest.importorskip("py7zr", reason="actual timestamp conversion needs the conversion extra")
    from py7zr.helpers import ArchiveTimestamp

    monkeypatch.setenv("TZ", "Etc/GMT+4")
    library, scratch = tmp_path / "library", tmp_path / "scratch"
    library.mkdir()
    scratch.mkdir()
    book = library / "Series v01.7z"
    with py7zr.SevenZipFile(book, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.writestr(b"dated page bytes", "pages/01.txt")
        archive.header.files_info.files[-1]["lastwritetime"] = ArchiveTimestamp.from_datetime(
            datetime(2020, 7, 20, 19, 1, 33, tzinfo=UTC).timestamp(),
        )
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == "valid", result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.getinfo("pages/01.txt").date_time == (2020, 7, 20, 15, 1, 32)
        assert archive.read("pages/01.txt") == b"dated page bytes"
    assert book.read_bytes() == original


@pytest.mark.parametrize("timezone", ["UTC", "Etc/GMT+4"])
def test_licensed_rar5_keeps_original_member_dates_and_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timezone: str,
) -> None:
    rarfile = pytest.importorskip("rarfile", reason="actual timestamp conversion needs the conversion extra")
    if shutil.which("unrar") is None:
        pytest.skip("actual RAR timestamp proof requires the licensed unrar prerequisite")
    monkeypatch.setenv("TZ", timezone)
    library, scratch = tmp_path / "library", tmp_path / "scratch"
    library.mkdir()
    scratch.mkdir()
    book = library / "Series v01.rar"
    shutil.copyfile(NATIVE_FIXTURES / "rar5-subdirs.rar", book)
    original = book.read_bytes()
    with rarfile.RarFile(book) as source_archive:
        source_times = {info.filename: info.mtime for info in source_archive.infolist() if info.is_file()}
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == "valid", result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        for name, source_time in source_times.items():
            assert source_time is not None
            published = archive.getinfo(name).date_time
            local_time = datetime(*published, tzinfo=ZoneInfo(timezone))
            assert published[5] % 2 == 0
            assert 0 <= source_time.timestamp() - local_time.timestamp() < 2
        ordered = ("sub/dir1/file1.txt", "sub/dir2/file2.txt", "sub/with space/long fn.txt")
        assert archive.getinfo(ordered[0]).date_time < archive.getinfo(ordered[1]).date_time < archive.getinfo(ordered[2]).date_time
        assert archive.read("sub/dir1/file1.txt") == b"file1\n"
        assert archive.read("sub/dir2/file2.txt") == b"file2\n"
        assert sha256(archive.read("sub/with space/long fn.txt")).hexdigest() == "e708f3c52269d19a8ac86b5934ff9cf2a80c696cb04a66a435e96f9ca44f7c18"
        assert archive.testzip() is None
    assert book.read_bytes() == original
    assert not book.with_suffix(".cbz").exists()


def test_licensed_rar3_preserves_dates_archive_and_file_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("rarfile", reason="actual timestamp conversion needs the conversion extra")
    if shutil.which("unrar") is None:
        pytest.skip("actual RAR timestamp proof requires the licensed unrar prerequisite")
    monkeypatch.setenv("TZ", "UTC")
    library, scratch = tmp_path / "library", tmp_path / "scratch"
    library.mkdir()
    scratch.mkdir()
    book = library / "Series v01.rar"
    shutil.copyfile(NATIVE_FIXTURES / "rar3-comment-plain.rar", book)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == "valid", result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.getinfo("file1.txt").date_time == (2010, 11, 2, 10, 3, 24)
        assert archive.getinfo("file2.txt").date_time == (2010, 11, 2, 10, 3, 24)
        assert archive.comment == b"RARcomment\n"
        assert archive.getinfo("file1.txt").comment == b"Comment1v2\n"
        assert archive.getinfo("file2.txt").comment == b"Comment2v2\n"
        assert archive.read("file1.txt") == b"" and archive.read("file2.txt") == b""
        assert archive.testzip() is None
    assert book.read_bytes() == original
    assert not book.with_suffix(".cbz").exists()

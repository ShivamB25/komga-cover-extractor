"""Generated archives and rasters. Worker tests use the installed interpreter."""
from __future__ import annotations

import stat
import zipfile
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from PIL import Image

from komga_cover_extractor.application import extract_covers as service
from komga_cover_extractor.application.extract_covers import extract_cover
from komga_cover_extractor.covers.selection import CoverPolicy
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.images import (
    ByteOutputPolicy,
    FileOutputPolicy,
)
from komga_cover_extractor.infrastructure.workers import WorkerError, run_worker
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
from komga_cover_extractor.matching import cover_similarity
from komga_cover_extractor.matching.cover_similarity import (
    compare_cover_bytes,
)


def _raster(color: str, *, image_format: str = "PNG") -> bytes:
    buffer = BytesIO()
    with Image.new("RGB", (16, 16), color) as image:
        image.save(buffer, format=image_format)
    return buffer.getvalue()


def _archive(path: Path, entries: tuple[tuple[str, bytes], ...]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def _manifest(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def _request(book: Path, root: Path, policy: CoverPolicy, *, output: str = "file", kind: str = "volume") -> dict[str, object]:
    return {"book": str(book), "root": str(root), "cover_policy": policy.as_dict(), "output": output, "release_kind": kind}


def _epub(path: Path, *, cover_href: str = "images/Front%20Cover.jpg", extra_manifest: str = "") -> Path:
    container = b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    opf = f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Generated fixture</dc:title><dc:publisher>First publisher</dc:publisher><dc:publisher>Second publisher</dc:publisher></metadata><manifest><item id="front" href="{cover_href}" media-type="image/jpeg" properties="nav cover-image"/>{extra_manifest}</manifest></package>'.encode()
    return _archive(path, (("mimetype", b"application/epub+zip"), ("META-INF/container.xml", container), ("OPS/package.opf", opf), ("AAA/Front Cover.jpg", _raster("blue", image_format="JPEG")), ("OPS/images/Front Cover.jpg", _raster("red", image_format="JPEG"))))


@pytest.mark.parametrize("compress", [False, True])
def test_real_worker_selects_lexical_red_page_and_returns_actual_webp(tmp_path: Path, compress: bool) -> None:
    book = _archive(tmp_path / "Series v01.cbz", (("cover.png", _raster("blue")), ("01.png", _raster("red"))))
    before = _manifest(tmp_path)
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy(), output_covers_as_webp=True, compress_image_option=compress), output=FileOutputPolicy())
    assert (result.status, result.selected_member, result.format, result.media_type, result.suffix) == ("valid", "01.png", "WEBP", "image/webp", ".webp")
    assert result.data is None and result.artifact is not None and result.artifact.content is not None
    with Image.open(BytesIO(result.artifact.content)) as image:
        assert image.format == "WEBP"
        red, green, blue = image.convert("RGB").getpixel((8, 8))
        assert red > 240 and green < 12 and blue < 12
    assert _manifest(tmp_path) == before
    assert tuple(page.name for page in result.archive_pages) == ("01.png", "cover.png")


def test_byte_mode_returns_quality60_jpeg_without_sidecar(tmp_path: Path) -> None:
    source = _raster("red")
    book = _archive(tmp_path / "Series v01.cbz", (("01.png", source),))
    before = _manifest(tmp_path)
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy(), image_quality=2), output=ByteOutputPolicy())
    expected = BytesIO()
    with Image.open(BytesIO(source)) as image:
        image.save(expected, format="JPEG", quality=60)
    assert (result.status, result.format, result.suffix, result.data, result.artifact) == ("valid", "JPEG", ".jpg", expected.getvalue(), None)
    assert _manifest(tmp_path) == before


def test_file_mode_preserves_png_and_normalizes_jpeg_suffix(tmp_path: Path) -> None:
    for name, source, expected_format, expected_suffix in (("PNG v01.cbz", _raster("red"), "PNG", ".png"), ("JPEG v01.cbz", _raster("red", image_format="JPEG"), "JPEG", ".jpg")):
        member = "01.png" if expected_format == "PNG" else "01.jpeg"
        book = _archive(tmp_path / name, ((member, source),))
        result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
        assert (result.status, result.format, result.suffix) == ("valid", expected_format, expected_suffix)
        assert result.artifact is not None and result.artifact.content == source
    assert sorted(path.suffix for path in tmp_path.iterdir()) == [".cbz", ".cbz"]


def test_epub_worker_resolves_exact_percent_decoded_member(tmp_path: Path) -> None:
    book = _epub(tmp_path / "Novel v01.epub")
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=ByteOutputPolicy())
    assert (result.status, result.container, result.selected_member) == ("valid", "epub", "OPS/images/Front Cover.jpg")
    assert result.data is not None
    with Image.open(BytesIO(result.data)) as image:
        red, green, blue = image.getpixel((8, 8))
        assert red > 240 and green < 12 and blue < 12
    assert tuple(value for key, value in result.metadata.values if key == "dc:publisher") == ("First publisher", "Second publisher")


@pytest.mark.parametrize("href", ["https://invalid.example/cover.jpg", "../../outside.jpg", "missing.jpg"])
def test_authoritative_bad_epub_target_does_not_use_blue_decoy(tmp_path: Path, href: str) -> None:
    book = _epub(tmp_path / "Novel.epub", cover_href=href)
    before = _manifest(tmp_path)
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
    assert result.status == "invalid_input"
    assert result.artifact is None and result.data is None
    assert _manifest(tmp_path) == before


def test_authoritative_multiple_epub_covers_require_review(tmp_path: Path) -> None:
    book = _epub(tmp_path / "Novel.epub", extra_manifest='<item id="other" href="../AAA/Front%20Cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
    assert result.status in {"review_required", "ambiguous"}
    assert result.artifact is None


def test_missing_metadata_and_no_usable_images_remain_absent(tmp_path: Path) -> None:
    book = _archive(tmp_path / "Series.cbz", ((".hidden.png", _raster("red")), ("__cover.jpg", _raster("blue", image_format="JPEG")), ("readme.txt", b"generated fixture")))
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
    assert (result.status, result.reason, result.metadata.status) == ("absent", "no_cover_images", "absent")
    assert result.artifact is None


def test_chapter_gates_are_enforced_by_real_service(tmp_path: Path) -> None:
    book = _archive(tmp_path / "Series c001.cbz", (("01.png", _raster("red")),))
    policy = CoverPolicy(ArchivePolicy())
    disabled = extract_cover(book, root=tmp_path, policy=policy, output=FileOutputPolicy(), release_kind="chapter")
    enabled = extract_cover(book, root=tmp_path, policy=replace(policy, chapter_support_toggle=True, extract_chapter_covers=True), output=FileOutputPolicy(), release_kind="chapter")
    assert (disabled.status, disabled.reason, disabled.artifact) == ("absent", "chapter_cover_disabled", None)
    assert (enabled.status, enabled.selected_member) == ("valid", "01.png")


def test_all_member_pages_include_truthful_directory_observations(tmp_path: Path) -> None:
    book = _archive(tmp_path / "Series.cbz", (("000.png/", b""), ("001.xml", b"<generated/>"), ("nested/01.png", _raster("red"))))
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
    assert result.status == "valid"
    assert tuple((page.name, page.is_directory) for page in result.archive_pages) == (("000.png", True), ("001.xml", False), ("nested/01.png", False))


def test_encoded_output_quota_preserves_original_book_and_custom_cover(tmp_path: Path) -> None:
    raw = _raster("red")
    book = _archive(tmp_path / "Series v01.cbz", (("01.png", raw),))
    existing = tmp_path / "Series v01.jpg"
    existing.write_bytes(_raster("blue", image_format="JPEG"))
    before = _manifest(tmp_path)
    policy = CoverPolicy(replace(ArchivePolicy(), cover_member_max_bytes=len(raw)))
    result = extract_cover(book, root=tmp_path, policy=policy, output=ByteOutputPolicy())
    assert result.status == "invalid_input" and result.data is None
    assert _manifest(tmp_path) == before


def test_blank_filter_reads_real_members_and_advances_without_sidecars(tmp_path: Path) -> None:
    buffer = BytesIO()
    with Image.new("RGB", (16, 16), "red") as art:
        for y in range(8):
            for x in range(8):
                art.putpixel((x, y), (0, 0, 0))
        art.save(buffer, format="PNG")
    white = _raster("white")
    book = _archive(tmp_path / "Series.cbz", (("00.png", white), ("01.png", buffer.getvalue()), ("cover.png", _raster("blue"))))
    before = _manifest(tmp_path)
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy(), compare_detected_cover_to_blank_images=True), output=FileOutputPolicy())
    assert result.status == "valid" and result.selected_member == "01.png"
    assert "blank_cover_skipped" in result.diagnostics
    assert _manifest(tmp_path) == before


def test_bad_first_page_is_invalid_not_blank_or_original_byte_fallback(tmp_path: Path) -> None:
    book = _archive(tmp_path / "Series.cbz", (("01.png", b"bad image"), ("cover.png", _raster("blue"))))
    before = _manifest(tmp_path)
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
    assert (result.status, result.reason, result.artifact) == ("invalid_input", "invalid_image", None)
    assert _manifest(tmp_path) == before


def test_cancelled_request_starts_no_decode_and_writes_nothing(tmp_path: Path) -> None:
    book = _archive(tmp_path / "Series.cbz", (("01.png", _raster("red")),))
    before = _manifest(tmp_path)
    cancelled = Event()
    cancelled.set()
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy(), cancel=cancelled)
    assert (result.status, result.reason, result.artifact) == ("cancelled", "shutdown_requested", None)
    assert _manifest(tmp_path) == before


def test_supervisor_result_validation_rejects_false_image_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = _archive(tmp_path / "Series.cbz", (("01.png", _raster("red")),))
    policy = CoverPolicy(ArchivePolicy())
    response = run_worker("cover", _request(book, tmp_path, policy))
    response["format"], response["media_type"], response["suffix"] = "WEBP", "image/webp", ".webp"
    monkeypatch.setattr(service, "run_worker", lambda *args, **kwargs: response)
    result = extract_cover(book, root=tmp_path, policy=policy, output=FileOutputPolicy())
    assert (result.status, result.reason, result.artifact) == ("failed", "invalid_cover_worker_image", None)


def test_authorized_root_escape_is_refused_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = _archive(tmp_path / "Series.cbz", (("01.png", _raster("red")),))
    before = _manifest(tmp_path)
    result = extract_cover(outside, root=root, policy=CoverPolicy(ArchivePolicy()), output=FileOutputPolicy())
    assert (result.status, result.reason) == ("invalid_input", "outside_authorized_root")
    assert _manifest(tmp_path) == before


def test_epub_blank_match_advances_pattern_before_lexical_fallback(tmp_path: Path) -> None:
    container = b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>'
    package = b'<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest/></package>'
    white = _raster("white")
    book = _archive(tmp_path / "Novel.epub", (
        ("mimetype", b"application/epub+zip"), ("META-INF/container.xml", container),
        ("OPS/package.opf", package), ("OPS/cover_00.png", white),
        ("OPS/cover_01.png", _raster("green")), ("OPS/p000.png", _raster("red")),
    ))
    result = extract_cover(book, root=tmp_path, policy=CoverPolicy(ArchivePolicy(), cover_patterns=(r"cover", r"p000"), compare_detected_cover_to_blank_images=True), output=FileOutputPolicy())
    assert result.status == "valid" and result.selected_member == "OPS/p000.png"


def test_supervised_similarity_returns_score_without_parent_decoder(tmp_path: Path) -> None:
    before = _manifest(tmp_path)
    same = compare_cover_bytes(_raster("red"), _raster("red"), required_score=1.0)
    different = compare_cover_bytes(_raster("white"), _raster("black"))
    assert (same.status, same.score) == ("matched", 1.0)
    assert (different.status, different.reason) == ("no_match", "ssim_threshold")
    assert _manifest(tmp_path) == before


def test_large_valid_covers_are_not_limited_by_worker_json_transport(tmp_path: Path) -> None:
    buffer = BytesIO()
    with Image.new("RGB", (1700, 1700), "red") as image:
        image.save(buffer, format="PNG", compress_level=0)
    encoded = buffer.getvalue()
    assert len(encoded) > 8 * 1024 * 1024
    before = _manifest(tmp_path)
    result = compare_cover_bytes(encoded, encoded, required_score=1.0)
    assert (result.status, result.score) == ("matched", 1.0)
    assert _manifest(tmp_path) == before


def test_comparison_member_limit_is_per_input_and_inclusive() -> None:
    encoded = _raster("red")
    accepted = compare_cover_bytes(encoded, encoded, max_bytes=len(encoded))
    rejected = compare_cover_bytes(encoded, encoded, max_bytes=len(encoded) - 1)
    assert (accepted.status, accepted.score) == ("matched", 1.0)
    assert (rejected.status, rejected.reason, rejected.score) == ("invalid_input", "image_byte_limit", None)


def test_supervised_similarity_preserves_negative_score_at_zero_threshold() -> None:
    buffers = (BytesIO(), BytesIO())
    for inverse, buffer in enumerate(buffers):
        with Image.new("L", (16, 16)) as image:
            for y in range(16):
                for x in range(16):
                    image.putpixel((x, y), 240 if (x // 4 + y // 4) % 2 == inverse else 20)
            image.save(buffer, format="PNG")
    result = compare_cover_bytes(buffers[0].getvalue(), buffers[1].getvalue(), required_score=0.0)
    assert (result.status, result.reason) == ("no_match", "ssim_threshold")
    assert result.score is not None and -1 <= result.score < 0


def test_comparison_worker_bounds_both_decoded_inputs() -> None:
    buffer = BytesIO()
    with Image.new("RGB", (17, 16), "red") as image:
        image.save(buffer, format="PNG")
    result = compare_cover_bytes(_raster("red"), buffer.getvalue(), max_pixels=256)
    assert (result.status, result.reason, result.score) == ("invalid_input", "image_pixel_limit", None)


def test_comparison_scratch_is_private_and_cleaned_after_worker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = _raster("red"), _raster("blue")
    visited: list[Path] = []
    before = _manifest(tmp_path)

    def fail_worker(kind: str, request: dict[str, object], **options: object) -> dict[str, object]:
        assert kind == "cover" and request["comparison"] is True
        root = Path(str(request["root"]))
        visited.append(root)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "left.image").stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "right.image").stat().st_mode) == 0o600
        assert (root / "left.image").read_bytes() == left
        assert (root / "right.image").read_bytes() == right
        raise WorkerError("worker_deadline", "failed")

    monkeypatch.setattr(cover_similarity, "run_worker", fail_worker)
    result = compare_cover_bytes(left, right)
    assert (result.status, result.reason, result.score) == ("failed", "worker_deadline", None)
    assert len(visited) == 1 and not visited[0].exists()
    assert _manifest(tmp_path) == before


def test_comparison_source_change_is_stale_without_decoding_or_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "kce-cover-comparison-generated"
    root.mkdir(mode=0o700)
    left, right = root / "left.image", root / "right.image"
    left.write_bytes(_raster("red"))
    right.write_bytes(_raster("blue"))
    left.chmod(0o600)
    right.chmod(0o600)
    request = {
        "comparison": True, "root": str(root), "left": fingerprint(left, root).as_dict(),
        "right": fingerprint(right, root).as_dict(), "required_score": 0.9,
        "max_pixels": 256, "max_bytes": 1024,
    }
    changed = _raster("green")
    left.write_bytes(changed)
    with pytest.raises(WorkerError) as caught:
        run_worker("cover", request)
    assert (caught.value.status, caught.value.reason) == ("stale_plan", "comparison_source_changed")
    assert left.read_bytes() == changed and right.read_bytes() == _raster("blue")

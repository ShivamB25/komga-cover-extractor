"""Small generated images exercise the retained OpenCV/SSIM pipeline."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from komga_cover_extractor.infrastructure.images import ImageError
from komga_cover_extractor.matching import cover_similarity
from komga_cover_extractor.matching.cover_similarity import (
    BlankReferenceImages,
    compare_covers,
    compare_to_blank,
    load_blank_references,
)


def _solid(color: str, mode: str = "RGB") -> bytes:
    buffer = BytesIO()
    with Image.new(mode, (16, 16), color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _pattern(*, inverse: bool = False, grayscale: bool = False) -> bytes:
    buffer = BytesIO()
    with Image.new("L" if grayscale else "RGB", (16, 16)) as image:
        for y in range(16):
            for x in range(16):
                value = 240 if ((x // 4 + y // 4) % 2 == int(inverse)) else 20
                image.putpixel((x, y), value if grayscale else (value, value, value))
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _blanks() -> BlankReferenceImages:
    return BlankReferenceImages(_solid("white"), _solid("black"))


@pytest.mark.parametrize("color", ["white", "black"])
def test_characterized_white_and_black_are_blank(color: str) -> None:
    result = compare_to_blank(_solid(color), references=_blanks())
    assert (result.status, result.reason, result.score) == ("matched", "blank_reference_match", 1.0)


def test_near_blank_fixture_uses_ssim_not_variance() -> None:
    buffer = BytesIO()
    with Image.new("RGB", (16, 16), "white") as image:
        image.putpixel((0, 0), (0, 0, 0))
        image.save(buffer, format="PNG")
    result = compare_to_blank(buffer.getvalue(), references=_blanks())
    assert (result.status, result.reason) == ("matched", "blank_reference_match")
    assert result.score is not None and result.score >= 0.9


def test_dark_and_grayscale_artwork_are_not_assumed_blank() -> None:
    dark = compare_to_blank(_pattern(), references=_blanks())
    gray = compare_to_blank(_pattern(grayscale=True), references=_blanks())
    assert (dark.status, dark.reason) == ("no_match", "artwork_not_blank")
    assert (gray.status, gray.reason) == ("no_match", "artwork_not_blank")


def test_channel_reconciliation_matches_gray_and_rgb_observations() -> None:
    result = compare_covers(_pattern(grayscale=True), _pattern())
    assert (result.status, result.score) == ("matched", 1.0)
    other = compare_covers(_pattern(), _pattern(inverse=True))
    assert (other.status, other.reason) == ("no_match", "ssim_threshold")
    assert other.score is not None and other.score < 0.9


def test_similarity_threshold_is_inclusive_and_policy_scoped() -> None:
    same = compare_covers(_pattern(), _pattern(), required_score=1.0)
    assert (same.status, same.score) == ("matched", 1.0)
    different = compare_covers(_solid("white"), _solid("black"), required_score=0.9)
    relaxed = compare_covers(_solid("white"), _solid("black"), required_score=0.0)
    assert different.status == "no_match"
    assert relaxed.status == "matched"


def test_invalid_image_is_neither_blank_nor_known_nonmatch() -> None:
    result = compare_to_blank(b"bad image", references=_blanks())
    assert (result.status, result.reason, result.score) == ("invalid_input", "invalid_image", None)
    valid = compare_covers(_pattern(), _pattern())
    assert (valid.status, valid.score) == ("matched", 1.0)


def test_pixel_budget_fails_before_optional_decoder_work() -> None:
    accepted = compare_covers(_pattern(), _pattern(), max_pixels=256)
    rejected = compare_covers(_pattern(), _pattern(), max_pixels=255)
    assert accepted.score == 1.0
    assert (rejected.status, rejected.reason, rejected.score) == ("invalid_input", "image_pixel_limit", None)


def test_missing_similarity_extra_is_a_capability_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_import = cover_similarity.import_module

    def missing(name: str) -> object:
        if name == "skimage.metrics":
            raise ModuleNotFoundError("generated secret-free missing dependency")
        return actual_import(name)

    monkeypatch.setattr(cover_similarity, "import_module", missing)
    result = compare_to_blank(_pattern(), references=_blanks())
    assert (result.status, result.reason, result.score) == ("missing_dependency", "image_similarity_extra_required", None)


def test_missing_packaged_resources_are_not_silently_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cover_similarity.resources, "files", lambda package: tmp_path)
    with pytest.raises(ImageError) as caught:
        load_blank_references()
    assert (caught.value.status, caught.value.reason) == ("missing_dependency", "packaged_blank_resources_required")
    result = compare_to_blank(_pattern())
    assert (result.status, result.reason, result.score) == ("missing_dependency", "packaged_blank_resources_required", None)

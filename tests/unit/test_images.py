"""Generated raster fixtures exercise bytes, not legacy source strings."""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageCms

from komga_cover_extractor.infrastructure.images import (
    ByteOutputPolicy,
    FileOutputPolicy,
    ImageError,
    encode_image,
    observe_image,
)


def _image(mode: str = "RGB", color: object = "red", *, image_format: str = "PNG", size: tuple[int, int] = (16, 16), **options: object) -> bytes:
    buffer = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def _decoded(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


@pytest.mark.parametrize("compress", [False, True])
def test_explicit_webp_transcodes_even_without_compression(compress: bool) -> None:
    source = _image()
    result = encode_image(source, source_suffix=".png", output=FileOutputPolicy(), output_covers_as_webp=True, compress_image_option=compress)
    assert (result.format, result.media_type, result.suffix) == ("WEBP", "image/webp", ".webp")
    assert result.data != source
    with _decoded(result.data) as image:
        assert image.format == "WEBP"
        red, green, blue = image.convert("RGB").getpixel((8, 8))
        assert red > 240 and green < 12 and blue < 12


@pytest.mark.parametrize(("image_format", "source_suffix", "suffix"), [("PNG", ".png", ".png"), ("JPEG", ".jpeg", ".jpg")])
def test_untransformed_truthful_images_preserve_encoded_bytes(image_format: str, source_suffix: str, suffix: str) -> None:
    source = _image(image_format=image_format)
    result = encode_image(source, source_suffix=source_suffix, output=FileOutputPolicy())
    assert result.data == source
    assert (result.format, result.suffix) == (image_format, suffix)


def test_mislabeled_member_returns_truthful_content_format() -> None:
    source = _image(image_format="JPEG")
    result = encode_image(source, source_suffix=".png", output=FileOutputPolicy())
    assert (result.format, result.media_type, result.suffix) == ("JPEG", "image/jpeg", ".jpg")
    with _decoded(result.data) as image:
        assert image.format == "JPEG"


@pytest.mark.parametrize(("source_format", "requested_webp", "target"), [("PNG", False, "JPEG"), ("WEBP", False, "WEBP"), ("PNG", True, "WEBP")])
def test_byte_output_has_separate_quality_and_codec(source_format: str, requested_webp: bool, target: str) -> None:
    source = _image(image_format=source_format)
    result = encode_image(source, source_suffix="." + source_format.lower(), output=ByteOutputPolicy(), output_covers_as_webp=requested_webp, image_quality=5)
    expected = BytesIO()
    with _decoded(source) as image:
        image.save(expected, format=target, quality=60)
    assert result.data == expected.getvalue()
    assert result.format == target


def test_file_compression_uses_quality_40_and_optimize() -> None:
    source = _image()
    result = encode_image(source, source_suffix=".png", output=FileOutputPolicy(), compress_image_option=True)
    expected = BytesIO()
    with _decoded(source) as image:
        image.save(expected, format="JPEG", quality=40, optimize=True)
    assert (result.data, result.format, result.suffix) == (expected.getvalue(), "JPEG", ".jpg")


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_jpeg_transparency_composites_onto_white(mode: str) -> None:
    buffer = BytesIO()
    color = (255, 0, 0, 0) if mode == "RGBA" else (0, 0) if mode == "LA" else 0
    with Image.new(mode, (16, 16), color) as image:
        if mode == "P":
            image.putpalette([255, 0, 0] * 256)
            image.info["transparency"] = 0
        image.save(buffer, format="PNG")
    result = encode_image(buffer.getvalue(), source_suffix=".png", output=ByteOutputPolicy())
    with _decoded(result.data) as image:
        assert image.mode == "RGB"
        assert image.getpixel((8, 8)) == (255, 255, 255)


@pytest.mark.parametrize(("mode", "color", "source_format"), [("L", 100, "PNG"), ("CMYK", (0, 255, 255, 0), "JPEG")])
def test_jpeg_accepts_grayscale_and_cmyk(mode: str, color: object, source_format: str) -> None:
    result = encode_image(_image(mode, color, image_format=source_format), source_suffix=".png", output=ByteOutputPolicy())
    with _decoded(result.data) as image:
        assert image.format == "JPEG"
        assert image.mode == ("L" if mode == "L" else "RGB")


def test_orientation_applies_once_and_exif_is_stripped() -> None:
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "generated fixture"
    source = _image(size=(24, 16), exif=exif)
    first = encode_image(source, source_suffix=".png", output=ByteOutputPolicy())
    with _decoded(first.data) as image:
        assert image.size == (16, 24)
        assert list(image.getexif().items()) == []
    second = encode_image(first.data, source_suffix=".jpg", output=ByteOutputPolicy())
    with _decoded(second.data) as image:
        assert image.size == (16, 24)
        assert list(image.getexif().items()) == []


def test_valid_icc_survives_only_unchanged_color_space() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = _image(icc_profile=profile)
    result = encode_image(source, source_suffix=".png", output=ByteOutputPolicy())
    with _decoded(result.data) as image:
        assert image.info["icc_profile"] == profile
    palette = _image("P", 0, icc_profile=profile)
    changed = encode_image(palette, source_suffix=".png", output=ByteOutputPolicy())
    with _decoded(changed.data) as image:
        assert "icc_profile" not in image.info
    invalid = encode_image(_image(icc_profile=b"not an ICC profile"), source_suffix=".png", output=ByteOutputPolicy())
    with _decoded(invalid.data) as image:
        assert "icc_profile" not in image.info


def test_animated_input_is_explicitly_unsupported() -> None:
    buffer = BytesIO()
    with Image.new("RGB", (16, 16), "red") as first, Image.new("RGB", (16, 16), "blue") as second:
        first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(ImageError) as caught:
        encode_image(buffer.getvalue(), source_suffix=".png", output=ByteOutputPolicy())
    assert (caught.value.status, caught.value.reason) == ("unsupported", "unsupported_animation")


def test_pixel_and_encoded_byte_limits_include_exact_boundary() -> None:
    source = _image()
    observation = observe_image(source, max_pixels=256)
    assert (observation.width, observation.height, observation.is_grayscale) == (16, 16, False)
    with pytest.raises(ImageError) as pixels:
        observe_image(source, max_pixels=255)
    assert pixels.value.reason == "image_pixel_limit"
    assert encode_image(source, source_suffix=".png", output=FileOutputPolicy(), max_bytes=len(source)).data == source
    with pytest.raises(ImageError) as size:
        encode_image(source, source_suffix=".png", output=FileOutputPolicy(), max_bytes=len(source) - 1)
    assert size.value.reason == "image_byte_limit"


def test_transformed_output_is_bounded_during_encoding() -> None:
    source = _image()
    with pytest.raises(ImageError) as caught:
        encode_image(source, source_suffix=".png", output=ByteOutputPolicy(), max_bytes=len(source))
    assert caught.value.reason == "image_byte_limit"


@pytest.mark.parametrize("source", [b"not an image", _image()[:30]])
def test_bad_images_are_invalid_not_blank(source: bytes) -> None:
    with pytest.raises(ImageError) as caught:
        observe_image(source, max_pixels=256)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "invalid_image")


def test_encoder_failure_never_falls_back_to_original_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _image()

    def fail_save(self: Image.Image, *args: object, **kwargs: object) -> None:
        raise OSError("generated encoder failure")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    with pytest.raises(ImageError) as caught:
        encode_image(source, source_suffix=".png", output=ByteOutputPolicy())
    assert (caught.value.status, caught.value.reason) == ("failed", "image_encode_failed")
    with _decoded(source) as image:
        assert image.format == "PNG"
        assert image.getpixel((8, 8)) == (255, 0, 0)


def test_grayscale_observation_preserves_legacy_tolerance() -> None:
    assert observe_image(_image("RGB", (100, 110, 115)), max_pixels=256).is_grayscale is True
    assert observe_image(_image("RGB", (100, 116, 100)), max_pixels=256).is_grayscale is False

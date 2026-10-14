"""Raster decoding and encoding owned by the supervised archive worker."""
from __future__ import annotations

from collections.abc import Buffer
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Literal, cast

from .workers import WorkerError

if TYPE_CHECKING:
    from PIL import Image

RasterFormat = Literal["JPEG", "PNG", "WEBP"]
_FORMAT_DETAILS: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
GRAYSCALE_CHANNEL_TOLERANCE = 15
GRAYSCALE_REQUIRED_FRACTION = 0.9


class ImageError(WorkerError):
    pass


class _BoundedImageBuffer(BytesIO):
    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self.max_bytes = max_bytes

    def write(self, data: Buffer) -> int:
        if self.tell() + memoryview(data).nbytes > self.max_bytes:
            raise ImageError("image_byte_limit")
        return super().write(data)


@dataclass(frozen=True, slots=True)
class ByteOutputPolicy:
    """Return-data mode always re-encodes with the byte consumer's quality."""


@dataclass(frozen=True, slots=True)
class FileOutputPolicy:
    """File mode returns bytes for publication, never a sidecar write."""


@dataclass(frozen=True, slots=True)
class EncodedImage:
    data: bytes
    format: RasterFormat
    media_type: str
    suffix: str


@dataclass(frozen=True, slots=True)
class ImageObservation:
    format: RasterFormat
    media_type: str
    suffix: str
    width: int
    height: int
    is_grayscale: bool


def _open_raster(data: bytes, *, max_pixels: int) -> Image.Image:
    try:
        from PIL import Image
    except ImportError:
        raise ImageError("covers_extra_required", "missing_dependency") from None
    if not data:
        raise ImageError("invalid_image")
    image = None
    try:
        image = Image.open(BytesIO(data))
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > max_pixels:
            raise ImageError("image_pixel_limit")
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
            raise ImageError("unsupported_animation", "unsupported")
        if image.format not in _FORMAT_DETAILS:
            raise ImageError("unsupported_raster_format", "unsupported")
        image.load()
        return image
    except ImageError:
        if image is not None:
            image.close()
        raise
    except Image.DecompressionBombError:
        if image is not None:
            image.close()
        raise ImageError("image_pixel_limit") from None
    except (OSError, SyntaxError, ValueError, OverflowError):
        if image is not None:
            image.close()
        raise ImageError("invalid_image") from None


def validate_image(data: bytes, *, max_pixels: int) -> RasterFormat:
    with _open_raster(data, max_pixels=max_pixels) as image:
        assert image.format in _FORMAT_DETAILS
        return cast(RasterFormat, image.format)


def _is_grayscale(image: Image.Image) -> bool:
    from PIL import ImageChops

    with image.convert("RGB") as rgb:
        red, green, blue = rgb.split()
        try:
            with ImageChops.difference(red, green) as rg, ImageChops.difference(green, blue) as gb:
                with ImageChops.lighter(rg, gb) as differences:
                    histogram = differences.histogram()
            count = sum(histogram[:GRAYSCALE_CHANNEL_TOLERANCE + 1])
            return bool(count / (image.width * image.height) > GRAYSCALE_REQUIRED_FRACTION)
        finally:
            red.close()
            green.close()
            blue.close()


def observe_image(data: bytes, *, max_pixels: int) -> ImageObservation:
    with _open_raster(data, max_pixels=max_pixels) as image:
        assert image.format in _FORMAT_DETAILS
        image_format = cast(RasterFormat, image.format)
        media_type, suffix = _FORMAT_DETAILS[image_format]
        return ImageObservation(
            format=image_format, media_type=media_type, suffix=suffix,
            width=image.width, height=image.height, is_grayscale=_is_grayscale(image),
        )


def _color_space(mode: str) -> str | None:
    if mode in {"RGB", "RGBA", "RGBX"}:
        return "RGB"
    if mode in {"L", "LA", "1"}:
        return "GRAY"
    if mode == "CMYK":
        return "CMYK"
    return None


def _retained_icc(image: Image.Image, source_mode: str, raw_profile: object) -> bytes | None:
    if not isinstance(raw_profile, bytes) or _color_space(source_mode) != _color_space(image.mode):
        return None
    try:
        from PIL import ImageCms
        profile = ImageCms.ImageCmsProfile(BytesIO(raw_profile))
        if profile.profile.xcolor_space.strip() == _color_space(image.mode):
            return raw_profile
    except ImportError:
        raise ImageError("icc_validation_required", "missing_dependency") from None
    except (OSError, TypeError, ValueError):
        return None
    return None


def _prepare_target(image: Image.Image, target: RasterFormat) -> Image.Image:
    from PIL import Image, ImageOps

    ImageOps.exif_transpose(image, in_place=True)
    if target == "JPEG":
        if image.mode in {"RGBA", "LA", "P"} and (
            "A" in image.getbands() or "transparency" in image.info
        ):
            with image.convert("RGBA") as rgba:
                result = Image.new("RGB", image.size, "white")
                with rgba.getchannel("A") as alpha:
                    result.paste(rgba, mask=alpha)
                return result
        return image.convert("L" if image.mode in {"1", "L"} else "RGB")
    if target == "WEBP":
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        return image.convert("RGBA" if has_alpha else "RGB")
    if image.mode in {"CMYK", "YCbCr", "HSV"}:
        return image.convert("RGB")
    return image.copy()


def encode_image(
    data: bytes, *, source_suffix: str, output: ByteOutputPolicy | FileOutputPolicy,
    output_covers_as_webp: bool = False, compress_image_option: bool = False,
    image_quality: int = 40, return_data_image_quality: int = 60,
    max_pixels: int = 100_000_000, max_bytes: int = 134_217_728,
) -> EncodedImage:
    if max_pixels <= 0 or max_bytes <= 0:
        raise ValueError("image limits must be positive")
    if not 1 <= image_quality <= 100 or not 1 <= return_data_image_quality <= 100:
        raise ValueError("image quality must be between 1 and 100")
    if len(data) > max_bytes:
        raise ImageError("image_byte_limit")
    with _open_raster(data, max_pixels=max_pixels) as image:
        assert image.format in _FORMAT_DETAILS
        source_format = cast(RasterFormat, image.format)
        media_type, suffix = _FORMAT_DETAILS[source_format]
        normalized_suffix = ".jpg" if source_suffix.lower() == ".jpeg" else source_suffix.lower()
        byte_mode = isinstance(output, ByteOutputPolicy)
        if byte_mode:
            target: RasterFormat = "WEBP" if output_covers_as_webp or source_format == "WEBP" else "JPEG"
            quality = return_data_image_quality
        else:
            if not isinstance(output, FileOutputPolicy):
                raise TypeError("unknown image output policy")
            if output_covers_as_webp:
                target = "WEBP"
            elif compress_image_option or source_format == "WEBP":
                target = "JPEG"
            else:
                target = source_format
            quality = image_quality
            if not output_covers_as_webp and not compress_image_option and source_format != "WEBP" and normalized_suffix == suffix:
                return EncodedImage(data, source_format, media_type, suffix)
        source_mode = image.mode
        profile = image.info.get("icc_profile")
        with _prepare_target(image, target) as transformed:
            icc = _retained_icc(transformed, source_mode, profile)
            transformed.info.clear()
            options: dict[str, object] = {"quality": quality}
            if not byte_mode:
                options["optimize"] = True
            if icc is not None:
                options["icc_profile"] = icc
            try:
                buffer = _BoundedImageBuffer(max_bytes)
                transformed.save(buffer, format=target, **options)
                encoded = buffer.getvalue()
            except (OSError, ValueError, TypeError, RuntimeError):
                raise ImageError("image_encode_failed", "failed") from None
        if len(encoded) > max_bytes:
            raise ImageError("image_byte_limit")
        with _open_raster(encoded, max_pixels=max_pixels) as verified:
            if verified.format != target:
                raise ImageError("encoded_format_mismatch", "failed")
        media_type, suffix = _FORMAT_DETAILS[target]
        return EncodedImage(encoded, target, media_type, suffix)

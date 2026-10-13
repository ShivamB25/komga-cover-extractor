"""Characterized worker-side SSIM and file-backed supervised cover comparison."""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module, resources
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from typing import Literal, cast

from ..domain.operations import FileFingerprint
from ..infrastructure.filesystem import FilesystemError, checked_path
from ..infrastructure.images import ImageError, validate_image
from ..infrastructure.workers import WORKER_CPU_SECONDS, WorkerError, run_worker

SimilarityStatus = Literal["matched", "no_match", "invalid_input", "unsupported", "missing_dependency", "failed", "cancelled", "stale_plan", "permission_denied"]
SIMILARITY_WIDTH = 400
SIMILARITY_HEIGHT = 600
BLANK_RESOURCE_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CoverSimilarityResult:
    status: SimilarityStatus
    reason: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class BlankReferenceImages:
    white: bytes
    black: bytes


def load_blank_references() -> BlankReferenceImages:
    try:
        root = resources.files("komga_cover_extractor").joinpath("resources")
        images = []
        for name in ("blank_white.jpg", "blank_black.png"):
            with root.joinpath(name).open("rb") as stream:
                data = stream.read(BLANK_RESOURCE_MAX_BYTES + 1)
            if not data or len(data) > BLANK_RESOURCE_MAX_BYTES:
                raise ImageError("blank_resources_invalid", "missing_dependency")
            images.append(data)
        return BlankReferenceImages(images[0], images[1])
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError):
        raise ImageError("packaged_blank_resources_required", "missing_dependency") from None


def compare_covers(
    left: bytes, right: bytes, *, required_score: float = 0.9,
    max_pixels: int = 100_000_000, reference_color: bool = False,
) -> CoverSimilarityResult:
    if not isfinite(required_score) or not 0 <= required_score <= 1:
        raise ValueError("required_score must be between zero and one")
    try:
        validate_image(left, max_pixels=max_pixels)
        validate_image(right, max_pixels=max_pixels)
        try:
            cv2 = import_module("cv2")
            np = import_module("numpy")
            metrics = import_module("skimage.metrics")
        except ImportError:
            return CoverSimilarityResult("missing_dependency", "image_similarity_extra_required")
        first = cv2.imdecode(np.frombuffer(left, np.uint8), cv2.IMREAD_COLOR if reference_color else cv2.IMREAD_UNCHANGED)
        second = cv2.imdecode(np.frombuffer(right, np.uint8), cv2.IMREAD_UNCHANGED)
        if first is None or second is None:
            return CoverSimilarityResult("invalid_input", "invalid_image")
        if first.dtype != np.uint8 or second.dtype != np.uint8:
            return CoverSimilarityResult("unsupported", "unsupported_similarity_pixel_depth")
        first = cv2.resize(first, (SIMILARITY_WIDTH, SIMILARITY_HEIGHT), interpolation=cv2.INTER_AREA)
        second = cv2.resize(second, (SIMILARITY_WIDTH, SIMILARITY_HEIGHT), interpolation=cv2.INTER_AREA)
        if first.ndim == 3 and second.ndim == 3:
            channels = min(first.shape[2], second.shape[2])
            if channels not in {1, 3, 4}:
                return CoverSimilarityResult("unsupported", "unsupported_similarity_channels")
            first, second = first[:, :, :channels], second[:, :, :channels]
        elif first.ndim == 3 and second.ndim == 2:
            first = first[:, :, 0]
        elif first.ndim == 2 and second.ndim == 3:
            second = second[:, :, 0]
        if first.ndim != second.ndim:
            if first.ndim == 3:
                first = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
            else:
                second = cv2.cvtColor(second, cv2.COLOR_BGR2GRAY)
        if first.ndim == 3:
            first = first[:, :, 0] if first.shape[2] == 1 else cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
        if second.ndim == 3:
            second = second[:, :, 0] if second.shape[2] == 1 else cv2.cvtColor(second, cv2.COLOR_BGR2GRAY)
        first = cv2.equalizeHist(first) / 255.0
        second = cv2.equalizeHist(second) / 255.0
        score = float(metrics.structural_similarity(first, second, data_range=1.0))
        if not isfinite(score):
            return CoverSimilarityResult("failed", "invalid_similarity_score")
        return CoverSimilarityResult("matched" if score >= required_score else "no_match", "ssim_threshold", score)
    except ImageError as error:
        status: SimilarityStatus = "missing_dependency" if error.status == "missing_dependency" else "unsupported" if error.status == "unsupported" else "invalid_input"
        return CoverSimilarityResult(status, error.reason)
    except (ValueError, TypeError, RuntimeError):
        return CoverSimilarityResult("failed", "image_similarity_failed")


def compare_to_blank(
    data: bytes, *, required_score: float = 0.9, max_pixels: int = 100_000_000,
    references: BlankReferenceImages | None = None,
) -> CoverSimilarityResult:
    try:
        references = load_blank_references() if references is None else references
    except ImageError as error:
        return CoverSimilarityResult("missing_dependency", error.reason)
    white = compare_covers(references.white, data, required_score=required_score, max_pixels=max_pixels, reference_color=True)
    if white.status not in {"matched", "no_match"}:
        return white
    black = compare_covers(references.black, data, required_score=required_score, max_pixels=max_pixels, reference_color=True)
    if black.status not in {"matched", "no_match"}:
        return black
    assert white.score is not None and black.score is not None
    blank = white.status == "matched" or black.status == "matched"
    return CoverSimilarityResult("matched" if blank else "no_match", "blank_reference_match" if blank else "artwork_not_blank", max(white.score, black.score))


COMPARISON_READ_CHUNK_BYTES = 64 * 1024
COMPARISON_SCRATCH_PREFIX = "kce-cover-comparison-"


def _write_comparison_input(
    root: Path, name: str, data: bytes, *, deadline: float, cancel: Event | None,
) -> FileFingerprint:
    path = root / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            if cancel is not None and cancel.is_set():
                raise WorkerError("shutdown_requested", "cancelled")
            if monotonic() >= deadline:
                raise WorkerError("worker_deadline", "failed")
            written = stream.write(view[offset:offset + COMPARISON_READ_CHUNK_BYTES])
            if written is None or written <= 0:
                raise WorkerError("comparison_scratch_write_failed", "failed")
            offset += written
        observed = os.fstat(stream.fileno())
    return FileFingerprint(root, name, observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, sha256(data).hexdigest())


def _read_comparison_input(
    value: object, *, root: Path, name: str, max_bytes: int,
) -> bytes:
    expected = {"root", "relative_path", "device", "inode", "size", "mtime_ns", "sha256"}
    if not isinstance(value, Mapping) or set(value) != expected or value["root"] != str(root) or value["relative_path"] != name:
        raise WorkerError("invalid_cover_comparison_request")
    for key in ("device", "inode", "size", "mtime_ns"):
        if type(value[key]) is not int or value[key] < 0:
            raise WorkerError("invalid_cover_comparison_request")
    digest = value["sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise WorkerError("invalid_cover_comparison_request")
    if value["size"] > max_bytes:
        raise WorkerError("image_byte_limit")
    try:
        path = checked_path(root / name, root)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            stream = os.fdopen(descriptor, "rb", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise WorkerError("invalid_cover_comparison_input")
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (value["device"], value["inode"], value["size"], value["mtime_ns"]):
                raise WorkerError("comparison_source_changed", "stale_plan")
            chunks: list[bytes] = []
            count = 0
            hasher = sha256()
            while chunk := stream.read(min(COMPARISON_READ_CHUNK_BYTES, before.st_size - count + 1, max_bytes - count + 1)):
                count += len(chunk)
                if count > max_bytes:
                    raise WorkerError("image_byte_limit")
                if count > before.st_size:
                    raise WorkerError("comparison_source_changed", "stale_plan")
                hasher.update(chunk)
                chunks.append(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if count != before.st_size or hasher.hexdigest() != digest or stat.S_ISLNK(current.st_mode):
            raise WorkerError("comparison_source_changed", "stale_plan")
        for observed in (after, current):
            if (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                raise WorkerError("comparison_source_changed", "stale_plan")
        return b"".join(chunks)
    except FilesystemError:
        raise WorkerError("invalid_cover_comparison_path") from None
    except PermissionError:
        raise WorkerError("comparison_scratch_permission_denied", "permission_denied") from None
    except OSError:
        raise WorkerError("comparison_scratch_read_failed", "failed") from None


def cover_comparison_job(request: dict[str, object]) -> dict[str, object]:
    if set(request) != {"comparison", "root", "left", "right", "required_score", "max_pixels", "max_bytes"} or request["comparison"] is not True:
        raise WorkerError("invalid_cover_comparison_request")
    root_value = request["root"]
    score, max_pixels, max_bytes = request["required_score"], request["max_pixels"], request["max_bytes"]
    if not isinstance(root_value, str) or type(max_pixels) is not int or max_pixels <= 0 or type(max_bytes) is not int or max_bytes <= 0:
        raise WorkerError("invalid_cover_comparison_request")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score) or not 0 <= score <= 1:
        raise WorkerError("invalid_cover_comparison_request")
    root = Path(root_value)
    if not root.is_absolute() or not root.name.startswith(COMPARISON_SCRATCH_PREFIX):
        raise WorkerError("invalid_cover_comparison_path")
    try:
        checked_path(root, root)
        observed = root.lstat()
    except OSError:
        raise WorkerError("invalid_cover_comparison_path") from None
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        raise WorkerError("comparison_scratch_not_private")
    left = _read_comparison_input(request["left"], root=root, name="left.image", max_bytes=max_bytes)
    right = _read_comparison_input(request["right"], root=root, name="right.image", max_bytes=max_bytes)
    result = compare_covers(left, right, required_score=float(score), max_pixels=max_pixels)
    if result.status not in {"matched", "no_match"}:
        raise WorkerError(result.reason, result.status)
    return {"status": "valid", "comparison_status": result.status, "score": result.score, "reason": result.reason}


def compare_cover_bytes(
    left: bytes, right: bytes, *, required_score: float = 0.9, max_pixels: int = 100_000_000,
    max_bytes: int = 134_217_728, timeout: float = 300, cancel: Event | None = None,
) -> CoverSimilarityResult:
    """Supervise decoder inputs in owned scratch, without library or state writes."""
    if not isfinite(required_score) or not 0 <= required_score <= 1 or type(max_pixels) is not int or max_pixels <= 0 or type(max_bytes) is not int or max_bytes <= 0 or not isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid cover comparison policy")
    if len(left) > max_bytes or len(right) > max_bytes:
        return CoverSimilarityResult("invalid_input", "image_byte_limit")
    if cancel is not None and cancel.is_set():
        return CoverSimilarityResult("cancelled", "shutdown_requested")
    deadline = monotonic() + min(timeout, WORKER_CPU_SECONDS)
    try:
        with TemporaryDirectory(prefix=COMPARISON_SCRATCH_PREFIX) as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            left_source = _write_comparison_input(root, "left.image", left, deadline=deadline, cancel=cancel)
            right_source = _write_comparison_input(root, "right.image", right, deadline=deadline, cancel=cancel)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WorkerError("worker_deadline", "failed")
            response = run_worker(
                "cover", {"comparison": True, "root": str(root), "left": left_source.as_dict(), "right": right_source.as_dict(),
                          "required_score": required_score, "max_pixels": max_pixels, "max_bytes": max_bytes},
                timeout=remaining, max_output_bytes=64 * 1024, cancel=cancel,
            )
        if set(response) != {"status", "comparison_status", "score", "reason"} or response["status"] != "valid":
            raise WorkerError("invalid_cover_comparison_result", "failed")
        status, score, reason = response["comparison_status"], response["score"], response["reason"]
        if status not in ("matched", "no_match") or isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score) or not -1 <= score <= 1 or not isinstance(reason, str):
            raise WorkerError("invalid_cover_comparison_result", "failed")
        if (status == "matched") != (score >= required_score):
            raise WorkerError("invalid_cover_comparison_result", "failed")
        return CoverSimilarityResult(status, reason, float(score))
    except WorkerError as error:
        status = cast(SimilarityStatus, error.status if error.status in {"invalid_input", "unsupported", "missing_dependency", "failed", "cancelled", "stale_plan", "permission_denied"} else "failed")
        return CoverSimilarityResult(status, error.reason)
    except PermissionError:
        return CoverSimilarityResult("permission_denied", "comparison_scratch_permission_denied")
    except OSError:
        return CoverSimilarityResult("failed", "comparison_scratch_failed")

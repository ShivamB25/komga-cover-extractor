"""Cover observation and transformation. Publication belongs to the executor."""
from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Literal, cast

from ..covers.selection import CoverPolicy, select_cover_candidates
from ..domain.operations import ArtifactSpec
from ..domain.releases import ArchivePageObservation
from ..infrastructure.filesystem import FilesystemError, checked_path
from ..infrastructure.images import (
    ByteOutputPolicy,
    FileOutputPolicy,
    RasterFormat,
    encode_image,
    observe_image,
)
from ..infrastructure.workers import WorkerError, run_worker
from ..infrastructure.xml_metadata import read_book_metadata, resolve_epub_cover
from ..infrastructure.zip_archive import ArchiveReader, validate_member_name
from ..matching.cover_similarity import compare_to_blank, load_blank_references
from ..parsing.metadata import MetadataResult

CoverStatus = Literal[
    "valid", "absent", "invalid_input", "unsupported", "missing_dependency",
    "cancelled", "failed", "permission_denied", "review_required", "conflict", "stale_plan", "ambiguous",
]
_IMAGE_DETAILS: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp"),
}
_RESULT_FIELDS = frozenset({
    "status", "reason", "selected_member", "format", "media_type", "suffix", "data_base64",
    "data_sha256", "metadata_status", "metadata", "metadata_reason", "archive_pages", "diagnostics", "container",
})


@dataclass(frozen=True, slots=True)
class CoverResult:
    status: CoverStatus
    reason: str
    selected_member: str | None = None
    format: RasterFormat | None = None
    media_type: str | None = None
    suffix: str | None = None
    data: bytes | None = None
    artifact: ArtifactSpec | None = None
    metadata: MetadataResult = field(default_factory=lambda: MetadataResult("absent"))
    archive_pages: tuple[ArchivePageObservation, ...] = ()
    diagnostics: tuple[str, ...] = ()
    container: str | None = None


def _wire_result(
    *, status: str, reason: str, metadata: MetadataResult,
    pages: tuple[ArchivePageObservation, ...], diagnostics: tuple[str, ...], container: str | None,
    selected_member: str | None = None, encoded: bytes | None = None,
    image_format: str | None = None, media_type: str | None = None, suffix: str | None = None,
) -> dict[str, object]:
    return {
        "status": status, "reason": reason, "selected_member": selected_member,
        "format": image_format, "media_type": media_type, "suffix": suffix,
        "data_base64": base64.b64encode(encoded).decode("ascii") if encoded is not None else None,
        "data_sha256": sha256(encoded).hexdigest() if encoded is not None else None,
        "metadata_status": metadata.status, "metadata": [list(pair) for pair in metadata.values],
        "metadata_reason": metadata.reason,
        "archive_pages": [{"name": page.name, "is_grayscale": page.is_grayscale, "is_directory": page.is_directory} for page in pages],
        "diagnostics": list(diagnostics), "container": container,
    }


def cover_job(request: dict[str, object]) -> dict[str, object]:
    """Execute actual work in the child. Never recursively start another worker."""
    if request.get("comparison") is True:
        from ..matching.cover_similarity import cover_comparison_job
        return cover_comparison_job(request)
    if set(request) != {"book", "root", "cover_policy", "output", "release_kind"}:
        raise WorkerError("invalid_cover_request")
    book_value, root_value = request["book"], request["root"]
    policy_value, output_value = request["cover_policy"], request["output"]
    release_kind = request["release_kind"]
    if not isinstance(book_value, str) or not isinstance(root_value, str) or not isinstance(policy_value, Mapping) or not all(isinstance(key, str) for key in policy_value):
        raise WorkerError("invalid_cover_request")
    if not isinstance(output_value, str) or output_value not in {"bytes", "file"} or not isinstance(release_kind, str) or release_kind not in {"volume", "chapter", "unknown"}:
        raise WorkerError("invalid_cover_request")
    try:
        policy = CoverPolicy.from_dict(cast(Mapping[str, object], policy_value))
    except (ValueError, TypeError):
        raise WorkerError("invalid_cover_policy") from None
    book, root = Path(book_value), Path(root_value)
    if not book.is_absolute() or not root.is_absolute():
        raise WorkerError("noncanonical_cover_path")
    try:
        book = checked_path(book, root)
    except FilesystemError as error:
        raise WorkerError(error.reason) from None
    if release_kind == "chapter" and not (policy.chapter_support_toggle and policy.extract_chapter_covers):
        return _wire_result(status="absent", reason="chapter_cover_disabled", metadata=MetadataResult("absent"), pages=(), diagnostics=(), container=None)
    from .convert import detect_container

    container = detect_container(book, policy=policy.archive_policy)
    if container not in {"zip", "epub"}:
        raise WorkerError("native_cover_requires_conversion", "unsupported")
    output = ByteOutputPolicy() if output_value == "bytes" else FileOutputPolicy()
    with ArchiveReader(book, policy.archive_policy) as reader:
        members = reader.members()
        metadata = read_book_metadata(reader)
        reference = resolve_epub_cover(reader) if container == "epub" else None
        selection = select_cover_candidates(members, policy=policy, is_epub=container == "epub", reference=reference, release_kind=release_kind)
        pages = tuple(ArchivePageObservation(member.name, is_directory=member.is_dir) for member in sorted(members, key=lambda item: item.raw_name))
        diagnostics = selection.diagnostics + ((metadata.reason,) if metadata.status == "invalid" else ())
        if selection.status == "absent":
            return _wire_result(status="absent", reason=selection.reason, metadata=metadata, pages=pages, diagnostics=diagnostics, container=container)
        if selection.status != "valid":
            raise WorkerError(selection.reason, "unsupported" if selection.status == "unsupported" else "review_required" if selection.status == "ambiguous" else "invalid_input")
        blanks = load_blank_references() if policy.compare_detected_cover_to_blank_images else None
        blank_members: set[str] = set()
        groups = selection.pattern_groups + (selection.fallback_candidates or selection.candidates,)
        for index, group in enumerate(groups):
            pattern_pass = index < len(selection.pattern_groups)
            for member in group:
                if member in blank_members:
                    if pattern_pass:
                        break
                    continue
                raw = reader.read_member(member, max_bytes=policy.archive_policy.cover_member_max_bytes)
                if blanks is not None:
                    comparison = compare_to_blank(raw, required_score=policy.blank_cover_required_similarity_score, max_pixels=policy.archive_policy.image_max_pixels, references=blanks)
                    if comparison.status == "matched":
                        blank_members.add(member)
                        diagnostics += ("blank_cover_skipped",)
                        if pattern_pass:
                            break
                        continue
                    if comparison.status != "no_match":
                        raise WorkerError(comparison.reason, comparison.status)
                observation = observe_image(raw, max_pixels=policy.archive_policy.image_max_pixels)
                encoded = encode_image(
                    raw, source_suffix=PurePosixPath(member).suffix, output=output,
                    output_covers_as_webp=policy.output_covers_as_webp,
                    compress_image_option=policy.compress_image_option,
                    image_quality=policy.image_quality, return_data_image_quality=policy.return_data_image_quality,
                    max_pixels=policy.archive_policy.image_max_pixels,
                    max_bytes=policy.archive_policy.cover_member_max_bytes,
                )
                pages = tuple(ArchivePageObservation(page.name, observation.is_grayscale if page.name == member else None, page.is_directory) for page in pages)
                reader.assert_unchanged()
                return _wire_result(
                    status="valid", reason=selection.reason, metadata=metadata, pages=pages,
                    diagnostics=diagnostics, container=container, selected_member=member,
                    encoded=encoded.data, image_format=encoded.format, media_type=encoded.media_type, suffix=encoded.suffix,
                )
        reader.assert_unchanged()
        return _wire_result(status="absent", reason="all_cover_candidates_blank", metadata=metadata, pages=pages, diagnostics=diagnostics, container=container)


def _decode_result(response: Mapping[str, object], *, policy: CoverPolicy, output: ByteOutputPolicy | FileOutputPolicy) -> CoverResult:
    if set(response) != _RESULT_FIELDS:
        raise WorkerError("invalid_cover_worker_result", "failed")
    status, reason = response["status"], response["reason"]
    metadata_status, metadata_values, metadata_reason = response["metadata_status"], response["metadata"], response["metadata_reason"]
    if status not in ("valid", "absent") or not isinstance(reason, str):
        raise WorkerError("invalid_cover_worker_result", "failed")
    if metadata_status not in ("valid", "absent", "invalid") or not isinstance(metadata_values, list) or not isinstance(metadata_reason, str):
        raise WorkerError("invalid_cover_worker_result", "failed")
    pairs: list[tuple[str, str]] = []
    for pair in metadata_values:
        if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) for value in pair):
            raise WorkerError("invalid_cover_worker_metadata", "failed")
        pairs.append((cast(str, pair[0]), cast(str, pair[1])))
    if metadata_status != "valid" and pairs:
        raise WorkerError("invalid_cover_worker_metadata", "failed")
    metadata = MetadataResult(metadata_status, tuple(pairs), metadata_reason)
    raw_pages, raw_diagnostics, container = response["archive_pages"], response["diagnostics"], response["container"]
    if not isinstance(raw_pages, list) or len(raw_pages) > policy.archive_policy.archive_max_entries or not isinstance(raw_diagnostics, list) or not all(isinstance(value, str) for value in raw_diagnostics) or container not in (None, "zip", "epub") or (status == "valid" and container is None):
        raise WorkerError("invalid_cover_worker_result", "failed")
    pages = []
    for page in raw_pages:
        if not isinstance(page, dict) or set(page) != {"name", "is_grayscale", "is_directory"} or not isinstance(page["name"], str) or type(page["is_directory"]) is not bool or (page["is_grayscale"] is not None and type(page["is_grayscale"]) is not bool):
            raise WorkerError("invalid_cover_worker_pages", "failed")
        name = validate_member_name(page["name"])
        pages.append(ArchivePageObservation(name, page["is_grayscale"], page["is_directory"]))
    diagnostics = tuple(cast(list[str], raw_diagnostics))
    if status == "absent":
        if any(response[name] is not None for name in ("selected_member", "format", "media_type", "suffix", "data_base64", "data_sha256")):
            raise WorkerError("invalid_cover_worker_result", "failed")
        return CoverResult("absent", reason, metadata=metadata, archive_pages=tuple(pages), diagnostics=diagnostics, container=container if isinstance(container, str) else None)
    member, image_format = response["selected_member"], response["format"]
    base64_data, digest = response["data_base64"], response["data_sha256"]
    if not isinstance(member, str) or not isinstance(image_format, str) or image_format not in _IMAGE_DETAILS or not isinstance(base64_data, str) or not isinstance(digest, str):
        raise WorkerError("invalid_cover_worker_image", "failed")
    member = validate_member_name(member)
    if member not in {page.name for page in pages if not page.is_directory}:
        raise WorkerError("invalid_cover_worker_image", "failed")
    media_type, suffix = _IMAGE_DETAILS[image_format]
    if response["media_type"] != media_type or response["suffix"] != suffix or len(base64_data) > ((policy.archive_policy.cover_member_max_bytes + 2) // 3) * 4:
        raise WorkerError("invalid_cover_worker_image", "failed")
    try:
        data = base64.b64decode(base64_data, validate=True)
    except (binascii.Error, ValueError):
        raise WorkerError("invalid_cover_worker_image", "failed") from None
    if not data or len(data) > policy.archive_policy.cover_member_max_bytes or sha256(data).hexdigest() != digest:
        raise WorkerError("invalid_cover_worker_image", "failed")
    truthful = (
        image_format == "JPEG" and data.startswith(b"\xff\xd8\xff")
        or image_format == "PNG" and data.startswith(b"\x89PNG\r\n\x1a\n")
        or image_format == "WEBP" and data.startswith(b"RIFF") and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data)
    )
    if not truthful:
        raise WorkerError("invalid_cover_worker_image", "failed")
    return CoverResult(
        "valid", reason, member, cast(RasterFormat, image_format), media_type, suffix,
        data=data if isinstance(output, ByteOutputPolicy) else None,
        artifact=ArtifactSpec(content=data, media_type=media_type) if isinstance(output, FileOutputPolicy) else None,
        metadata=metadata, archive_pages=tuple(pages), diagnostics=diagnostics, container=cast(str, container),
    )


def extract_cover(
    book: Path, *, policy: CoverPolicy, output: ByteOutputPolicy | FileOutputPolicy,
    root: Path | None = None, release_kind: str = "volume", cancel: Event | None = None,
) -> CoverResult:
    if not isinstance(output, (ByteOutputPolicy, FileOutputPolicy)):
        raise TypeError("unknown cover output policy")
    book = book.absolute()
    root = book.parent if root is None else root.absolute()
    try:
        response = run_worker(
            "cover", {"book": str(book), "root": str(root), "cover_policy": policy.as_dict(),
                      "output": "bytes" if isinstance(output, ByteOutputPolicy) else "file", "release_kind": release_kind},
            timeout=policy.archive_policy.conversion_timeout_seconds, cancel=cancel,
        )
        return _decode_result(response, policy=policy, output=output)
    except WorkerError as error:
        known = {"invalid_input", "unsupported", "missing_dependency", "cancelled", "failed", "permission_denied", "review_required", "conflict", "stale_plan", "ambiguous"}
        status = cast(CoverStatus, error.status if error.status in known else "failed")
        return CoverResult(status, error.reason)

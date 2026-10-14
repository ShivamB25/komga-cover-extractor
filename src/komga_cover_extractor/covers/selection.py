"""Ordered cover discovery policy with exact archive member identities."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from pathlib import PurePosixPath
from typing import Literal, cast

import regex

from ..infrastructure.xml_metadata import EpubCoverReference
from ..infrastructure.zip_archive import ArchiveMember, ArchivePolicy

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tbn", ".webp"})
DEFAULT_COVER_PATTERNS = (
    r"(cover\.([A-Za-z]+))$",
    r"(\b(Cover([0-9]+|)|CoverDesign|page([-_. ]+)?cover)\b)",
    r"(\b(p000|page_000)\b)",
    r"((\s+)0+\.(.{2,}))",
    r"(\bindex[-_. ]1[-_. ]1\b)",
    r"(9([-_. :]+)?7([-_. :]+)?(8|9)(([-_. :]+)?[0-9]){10})",
)


@dataclass(frozen=True, slots=True)
class CoverPolicy:
    archive_policy: ArchivePolicy
    cover_patterns: tuple[str, ...] = DEFAULT_COVER_PATTERNS
    chapter_support_toggle: bool = False
    extract_chapter_covers: bool = False
    compare_detected_cover_to_blank_images: bool = False
    blank_cover_required_similarity_score: float = 0.9
    compress_image_option: bool = False
    image_quality: int = 40
    output_covers_as_webp: bool = False
    return_data_image_quality: int = 60
    compiled_patterns: tuple[regex.Pattern[str], ...] = field(init=False, repr=False, compare=False)
    generation_fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.archive_policy, ArchivePolicy):
            raise TypeError("archive_policy must be an ArchivePolicy")
        for name in _BOOLEAN_FIELDS:
            if type(getattr(self, name)) is not bool:
                raise ValueError("cover flags must be boolean")
        for quality in (self.image_quality, self.return_data_image_quality):
            if type(quality) is not int or not 1 <= quality <= 100:
                raise ValueError("image quality must be between 1 and 100")
        score = self.blank_cover_required_similarity_score
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score) or not 0 <= score <= 1:
            raise ValueError("blank threshold must be between zero and one")
        patterns = tuple(self.cover_patterns)
        if any(not isinstance(pattern, str) for pattern in patterns):
            raise ValueError("cover_patterns must contain regular expressions")
        object.__setattr__(self, "cover_patterns", patterns)
        object.__setattr__(self, "compiled_patterns", tuple(regex.compile(pattern, regex.IGNORECASE) for pattern in patterns))
        encoded = json.dumps({"rule_version": 2, "policy": self.as_dict()},
                             sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        object.__setattr__(self, "generation_fingerprint", sha256(encoded).hexdigest())

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_policy": self.archive_policy.as_dict(),
            "cover_patterns": list(self.cover_patterns),
            "chapter_support_toggle": self.chapter_support_toggle,
            "extract_chapter_covers": self.extract_chapter_covers,
            "compare_detected_cover_to_blank_images": self.compare_detected_cover_to_blank_images,
            "blank_cover_required_similarity_score": self.blank_cover_required_similarity_score,
            "compress_image_option": self.compress_image_option,
            "image_quality": self.image_quality,
            "output_covers_as_webp": self.output_covers_as_webp,
            "return_data_image_quality": self.return_data_image_quality,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CoverPolicy:
        if set(data) != _POLICY_FIELDS:
            raise ValueError("invalid cover policy fields")
        archive = data["archive_policy"]
        patterns = data["cover_patterns"]
        score = data["blank_cover_required_similarity_score"]
        if not isinstance(archive, Mapping) or not all(isinstance(key, str) for key in archive):
            raise ValueError("invalid archive policy")
        if not isinstance(patterns, (list, tuple)) or any(not isinstance(pattern, str) for pattern in patterns):
            raise ValueError("invalid cover patterns")
        if any(type(data[name]) is not bool for name in _BOOLEAN_FIELDS):
            raise ValueError("invalid cover flags")
        if type(data["image_quality"]) is not int or type(data["return_data_image_quality"]) is not int:
            raise ValueError("invalid image quality")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("invalid blank threshold")
        return cls(
            archive_policy=ArchivePolicy.from_dict(cast(Mapping[str, object], archive)),
            cover_patterns=tuple(cast(Sequence[str], patterns)),
            chapter_support_toggle=cast(bool, data["chapter_support_toggle"]),
            extract_chapter_covers=cast(bool, data["extract_chapter_covers"]),
            compare_detected_cover_to_blank_images=cast(bool, data["compare_detected_cover_to_blank_images"]),
            blank_cover_required_similarity_score=float(score),
            compress_image_option=cast(bool, data["compress_image_option"]),
            image_quality=data["image_quality"],
            output_covers_as_webp=cast(bool, data["output_covers_as_webp"]),
            return_data_image_quality=data["return_data_image_quality"],
        )


_BOOLEAN_FIELDS = (
    "chapter_support_toggle", "extract_chapter_covers", "compare_detected_cover_to_blank_images",
    "compress_image_option", "output_covers_as_webp",
)
_POLICY_FIELDS = frozenset({
    "archive_policy", "cover_patterns", *_BOOLEAN_FIELDS, "blank_cover_required_similarity_score",
    "image_quality", "return_data_image_quality",
})


@dataclass(frozen=True, slots=True)
class CoverSelection:
    status: Literal["valid", "absent", "invalid_input", "ambiguous", "unsupported"]
    reason: str
    candidates: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    pattern_groups: tuple[tuple[str, ...], ...] = ()
    fallback_candidates: tuple[str, ...] = ()


def image_members(members: Sequence[ArchiveMember]) -> tuple[str, ...]:
    return tuple(
        member.name for member in sorted(members, key=lambda item: item.raw_name)
        if not member.is_dir
        and PurePosixPath(member.name).suffix.lower() in IMAGE_SUFFIXES
        and not PurePosixPath(member.name).name.startswith((".", "__"))
    )


def select_cover_candidates(
    members: Sequence[ArchiveMember], *, policy: CoverPolicy, is_epub: bool,
    reference: EpubCoverReference | None = None, release_kind: str = "volume",
) -> CoverSelection:
    if release_kind == "chapter" and not (policy.chapter_support_toggle and policy.extract_chapter_covers):
        return CoverSelection("absent", "chapter_cover_disabled")
    candidates = image_members(members)
    if len(candidates) > policy.archive_policy.cover_max_candidates:
        return CoverSelection("invalid_input", "cover_candidate_limit")
    diagnostics: tuple[str, ...] = ()
    authoritative: str | None = None
    if is_epub and reference is not None:
        if reference.status == "ambiguous":
            return CoverSelection("ambiguous", reference.reason)
        if reference.status == "invalid" and reference.authoritative:
            return CoverSelection("invalid_input", reference.reason)
        if reference.status == "found":
            if reference.member is None:
                return CoverSelection("invalid_input", "missing_epub_cover_member")
            if PurePosixPath(reference.member).suffix.lower() == ".svg":
                return CoverSelection("unsupported", "svg_cover_unsupported")
            if reference.member not in candidates:
                return CoverSelection("invalid_input", "unusable_epub_cover_member")
            if not policy.compare_detected_cover_to_blank_images:
                return CoverSelection("valid", "authoritative_epub_cover", (reference.member,))
            authoritative = reference.member
            candidates = tuple(name for name in candidates if name != authoritative)
        if reference.status in {"absent", "invalid"}:
            diagnostics = (reference.reason, "epub_image_fallback")
    if not candidates and authoritative is None:
        return CoverSelection("absent", "no_cover_images", diagnostics=diagnostics)
    if not is_epub:
        return CoverSelection("valid", "lexical_comic_page", candidates, diagnostics)
    ordered: list[str] = []
    seen: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for pattern in policy.compiled_patterns:
        matches = tuple(
            name for name in candidates
            if pattern.pattern == PurePosixPath(name).name or pattern.search(PurePosixPath(name).name)
        )
        if matches:
            groups.append(matches)
        for name in matches:
            if name not in seen:
                ordered.append(name)
                seen.add(name)
    ordered.extend(name for name in candidates if name not in seen)
    if authoritative is not None:
        return CoverSelection("valid", "authoritative_epub_cover", (authoritative,) + tuple(ordered),
                              diagnostics, ((authoritative,),) + tuple(groups), candidates)
    return CoverSelection("valid", "ordered_epub_fallback", tuple(ordered), diagnostics, tuple(groups), candidates)

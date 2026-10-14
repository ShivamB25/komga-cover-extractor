from __future__ import annotations

from dataclasses import replace

import pytest

from komga_cover_extractor.covers.selection import CoverPolicy, select_cover_candidates
from komga_cover_extractor.infrastructure.xml_metadata import EpubCoverReference
from komga_cover_extractor.infrastructure.zip_archive import (
    ArchiveMember,
    ArchivePolicy,
)


def _members(*names: str) -> tuple[ArchiveMember, ...]:
    return tuple(ArchiveMember(name, 1, 1, name.endswith("/"), name) for name in names)


def test_comic_uses_full_path_lexical_order_not_natural_or_cover_priority() -> None:
    policy = CoverPolicy(ArchivePolicy())
    result = select_cover_candidates(_members("cover.png", "10.png", "2.png", "01.png"), policy=policy, is_epub=False)
    assert (result.status, result.candidates) == ("valid", ("01.png", "10.png", "2.png", "cover.png"))


def test_epub_fallback_preserves_configured_pattern_order() -> None:
    policy = CoverPolicy(ArchivePolicy(), cover_patterns=(r"page_000", r"cover"))
    result = select_cover_candidates(_members("OPS/01.png", "OPS/cover.jpg", "OPS/page_000.png"), policy=policy, is_epub=True)
    assert result.candidates == ("OPS/page_000.png", "OPS/cover.jpg", "OPS/01.png")
    reverse = select_cover_candidates(_members("OPS/01.png", "OPS/cover.jpg", "OPS/page_000.png"), policy=replace(policy, cover_patterns=(r"cover", r"page_000")), is_epub=True)
    assert reverse.candidates == ("OPS/cover.jpg", "OPS/page_000.png", "OPS/01.png")


def test_hidden_basenames_and_unsupported_members_do_not_become_cover() -> None:
    result = select_cover_candidates(_members("OPS/.hidden.jpg", "OPS/__cover.png", "01.svg", "02.gif", "folder/"), policy=CoverPolicy(ArchivePolicy()), is_epub=False)
    assert (result.status, result.reason, result.candidates) == ("absent", "no_cover_images", ())


@pytest.mark.parametrize(("support", "extract", "status"), [(False, False, "absent"), (True, False, "absent"), (False, True, "absent"), (True, True, "valid")])
def test_chapter_cover_requires_both_gates(support: bool, extract: bool, status: str) -> None:
    policy = CoverPolicy(ArchivePolicy(), chapter_support_toggle=support, extract_chapter_covers=extract)
    chapter = select_cover_candidates(_members("01.png"), policy=policy, is_epub=False, release_kind="chapter")
    assert chapter.status == status
    volume = select_cover_candidates(_members("01.png"), policy=policy, is_epub=False, release_kind="volume")
    assert volume.candidates == ("01.png",)


def test_authoritative_epub_identity_never_matches_decoy_basename() -> None:
    reference = EpubCoverReference("found", "OPS/images/Front Cover.jpg", "epub3_cover", True)
    result = select_cover_candidates(_members("AAA/Front Cover.jpg", "OPS/images/Front Cover.jpg", "cover.jpg"), policy=CoverPolicy(ArchivePolicy()), is_epub=True, reference=reference)
    assert (result.status, result.candidates) == ("valid", ("OPS/images/Front Cover.jpg",))


@pytest.mark.parametrize(("reference", "status"), [
    (EpubCoverReference("invalid", None, "unsafe_epub_cover", True), "invalid_input"),
    (EpubCoverReference("ambiguous", None, "multiple_epub_covers", True), "ambiguous"),
    (EpubCoverReference("found", "cover.svg", "epub3_cover", True), "unsupported"),
    (EpubCoverReference("found", "missing.jpg", "epub3_cover", True), "invalid_input"),
])
def test_authoritative_bad_references_do_not_fall_back(reference: EpubCoverReference, status: str) -> None:
    result = select_cover_candidates(_members("01.png", "cover.svg"), policy=CoverPolicy(ArchivePolicy()), is_epub=True, reference=reference)
    assert result.status == status
    assert result.candidates == ()


def test_non_authoritative_invalid_metadata_has_diagnosed_fallback() -> None:
    result = select_cover_candidates(_members("01.png", "cover.jpg"), policy=CoverPolicy(ArchivePolicy()), is_epub=True, reference=EpubCoverReference("invalid", None, "invalid_container", False))
    assert (result.status, result.candidates, result.diagnostics) == ("valid", ("cover.jpg", "01.png"), ("invalid_container", "epub_image_fallback"))


def test_cover_candidate_limit_at_boundary_and_one_above() -> None:
    policy = CoverPolicy(ArchivePolicy(cover_max_candidates=2))
    accepted = select_cover_candidates(_members("01.png", "02.png"), policy=policy, is_epub=False)
    assert accepted.candidates == ("01.png", "02.png")
    rejected = select_cover_candidates(_members("01.png", "02.png", "03.png"), policy=policy, is_epub=False)
    assert (rejected.status, rejected.reason) == ("invalid_input", "cover_candidate_limit")


def test_empty_optional_patterns_retain_lexical_fallback() -> None:
    result = select_cover_candidates(_members("cover.png", "01.png"), policy=CoverPolicy(ArchivePolicy(), cover_patterns=()), is_epub=True)
    assert result.candidates == ("01.png", "cover.png")


def test_worker_policy_round_trip_keeps_effective_decisions() -> None:
    original = CoverPolicy(ArchivePolicy(), cover_patterns=(r"page_000",), output_covers_as_webp=True, return_data_image_quality=72)
    recovered = CoverPolicy.from_dict(original.as_dict())
    result = select_cover_candidates(_members("01.png", "page_000.png"), policy=recovered, is_epub=True)
    assert result.candidates == ("page_000.png", "01.png")
    invalid = original.as_dict() | {"arbitrary_function": "extractall"}
    with pytest.raises(ValueError):
        CoverPolicy.from_dict(invalid)


@pytest.mark.parametrize(("raw_first", "canonical_first", "other"), [
    ("./z.png", "z.png", "a.png"),
    ("dir//01.png", "dir/01.png", "dir/00.png"),
])
def test_comic_preserves_raw_lexical_order_with_canonical_lookup(
    raw_first: str, canonical_first: str, other: str,
) -> None:
    members = (
        ArchiveMember(other, 1, 1, False, other),
        ArchiveMember(canonical_first, 1, 1, False, raw_first),
    )
    result = select_cover_candidates(members, policy=CoverPolicy(ArchivePolicy()), is_epub=False)
    assert (result.status, result.candidates) == ("valid", (canonical_first, other))


def test_authoritative_blank_filter_keeps_authority_before_ordered_safe_fallback() -> None:
    policy = CoverPolicy(ArchivePolicy(), compare_detected_cover_to_blank_images=True,
                         cover_patterns=(r"p000", r"cover"))
    reference = EpubCoverReference("found", "OPS/front.png", "epub3_cover", True)
    result = select_cover_candidates(
        _members("OPS/01.png", "OPS/cover.png", "OPS/p000.png", "OPS/front.png"),
        policy=policy, is_epub=True, reference=reference,
    )
    assert result.candidates == ("OPS/front.png", "OPS/p000.png", "OPS/cover.png", "OPS/01.png")
    assert result.pattern_groups == (("OPS/front.png",), ("OPS/p000.png",), ("OPS/cover.png",))


@pytest.mark.parametrize("reference", [
    EpubCoverReference("invalid", None, "unsafe_epub_cover", True),
    EpubCoverReference("ambiguous", None, "multiple_epub_covers", True),
    EpubCoverReference("found", "missing.jpg", "epub3_cover", True),
])
def test_blank_filter_never_turns_invalid_authority_into_fallback(reference: EpubCoverReference) -> None:
    result = select_cover_candidates(
        _members("OPS/cover.png", "OPS/p000.png"),
        policy=CoverPolicy(ArchivePolicy(), compare_detected_cover_to_blank_images=True),
        is_epub=True, reference=reference,
    )
    assert result.status == ("ambiguous" if reference.status == "ambiguous" else "invalid_input")
    assert (result.candidates, result.pattern_groups, result.fallback_candidates) == ((), (), ())

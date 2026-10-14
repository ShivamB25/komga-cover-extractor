from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.operations import FileFingerprint
from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseNumber,
    ReleaseSnapshot,
)
from komga_cover_extractor.matching.classification import (
    ClassificationPolicy,
    LibraryType,
)
from komga_cover_extractor.matching.series import (
    MatchEvidenceCache,
    MatchingPolicy,
    MatchScoreCache,
    match_series,
)
from komga_cover_extractor.parsing.normalization import clean_str


def _release(
    path: str,
    series: str,
    *,
    number: ReleaseNumber | None = None,
    kind: str = "volume",
    identifiers: tuple[tuple[str, str], ...] = (),
    digest: str | None = None,
) -> ReleaseSnapshot:
    file_path = Path(path)
    fingerprint = None
    if digest is not None:
        fingerprint = FileFingerprint(
            root=file_path.parent,
            relative_path=file_path.name,
            device=1,
            inode=1,
            size=len(digest),
            mtime_ns=1,
            sha256=sha256(digest.encode()).hexdigest(),
        )
    return ReleaseSnapshot(
        observation=FileObservation(file_path, fingerprint=fingerprint),
        number=number or ReleaseNumber.missing(),
        kind=kind,
        display_series=series,
        canonical_series_key=clean_str(series),
        identifiers=identifiers,
    )


def _policy(
    *,
    image: bool = False,
    identifiers: bool = True,
    similarity: float = 0.979,
    classification: ClassificationPolicy | None = None,
) -> MatchingPolicy:
    return MatchingPolicy(
        required_similarity_score=similarity,
        match_through_image_similarity=image,
        match_through_identifiers=identifiers,
        classification=classification or ClassificationPolicy(),
    )


def _library(
    releases: tuple[ReleaseSnapshot, ...],
    *,
    extensions: tuple[str, ...] = (".cbz",),
    library_types: tuple[str, ...] = (),
) -> LibrarySnapshot:
    root = LibraryRoot(
        Path("/library"), extensions=extensions, library_types=library_types
    )
    return LibrarySnapshot(releases, (root,))


def test_matching_root_classifies_incoming_release_not_existing_contents() -> None:
    classification = ClassificationPolicy(
        library_types=(
            LibraryType(
                "light novel",
                (".epub",),
                (r"\[Publisher\]",),
                (),
                90,
            ),
        )
    )
    root = LibraryRoot(
        Path("/library"),
        extensions=(".epub",),
        library_types=("light novel",),
    )
    incoming = _release("/downloads/Title v02.epub", "Title")
    existing = _release("/library/Title/Title v01 [Publisher].epub", "Title")

    decision = match_series(
        incoming,
        LibrarySnapshot((existing,), (root,)),
        _policy(classification=classification),
    )

    assert decision.status == "no_match"
    allowed = match_series(
        _release("/downloads/Title v02 [Publisher].epub", "Title"),
        LibrarySnapshot((existing,), (root,)),
        _policy(classification=classification),
    )
    assert allowed.status == "matched"
    assert allowed.selected is not None
    assert allowed.selected.series_path == existing.parent


def test_alternative_cover_filter_does_not_compare_unrelated_volumes_over_ten() -> None:
    incoming = _release(
        "/downloads/Azure Adventures v99.cbz",
        "Azure Adventures",
        number=ReleaseNumber.scalar("99"),
    )
    releases = tuple(
        _release(
            f"/library/Azure Chronicles/Azure Chronicles v{index:02}.cbz",
            "Azure Chronicles",
            number=ReleaseNumber.scalar(str(index)),
        )
        for index in range(1, 12)
    )
    compared: list[Path] = []

    decision = match_series(
        incoming,
        _library(releases),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared.append(existing.path) or 0.95
        ),
    )

    assert decision.status == "no_match"
    assert compared == []


def test_alternative_cover_filter_compares_matching_index_before_bounded_rest() -> None:
    incoming = _release(
        "/downloads/Azure Adventures v01.cbz",
        "Azure Adventures",
        number=ReleaseNumber.scalar("1"),
    )
    releases = tuple(
        _release(
            f"/library/Azure Chronicles/Azure Chronicles v{index:02}.cbz",
            "Azure Chronicles",
            number=ReleaseNumber.scalar(str(index)),
        )
        for index in range(1, 12)
    )
    compared: list[Path] = []

    decision = match_series(
        incoming,
        _library(releases),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared.append(existing.path)
            or (0.95 if existing.number == ReleaseNumber.scalar("2") else 0.0)
        ),
    )

    assert decision.status == "matched"
    assert compared[:2] == [releases[0].path, releases[1].path]


def test_cover_index_matching_does_not_expand_a_legacy_range_interior() -> None:
    incoming = _release(
        "/downloads/Azure Adventures v02.cbz",
        "Azure Adventures",
        number=ReleaseNumber.scalar("2"),
    )
    range_release = _release(
        "/library/Azure Chronicles/Azure Chronicles v01-03.cbz",
        "Azure Chronicles",
        number=ReleaseNumber.range("1", "3"),
    )
    unrelated = tuple(
        _release(
            f"/library/Azure Chronicles/Azure Chronicles v{index:02}.cbz",
            "Azure Chronicles",
            number=ReleaseNumber.scalar(str(index)),
        )
        for index in range(10, 20)
    )
    compared: list[Path] = []

    decision = match_series(
        incoming,
        _library((range_release, *unrelated)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared.append(existing.path) or 0.95
        ),
    )

    assert decision.status == "no_match"
    assert compared == []


def test_cover_index_matching_preserves_explicit_collections_and_parts() -> None:
    collection_incoming = _release(
        "/downloads/Azure Adventures v03.cbz",
        "Azure Adventures",
        number=ReleaseNumber.scalar("3"),
    )
    collection = _release(
        "/library/Azure Chronicles/Azure Chronicles v01-03.cbz",
        "Azure Chronicles",
        number=ReleaseNumber.explicit_set(("1", "3")),
    )
    collection_rest = tuple(
        _release(
            f"/library/Azure Chronicles/Azure Chronicles v{index:02}.cbz",
            "Azure Chronicles",
            number=ReleaseNumber.scalar(str(index)),
        )
        for index in range(10, 20)
    )
    compared_collection: list[Path] = []
    collection_decision = match_series(
        collection_incoming,
        _library((collection, *collection_rest)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared_collection.append(existing.path)
            or (0.95 if existing is collection else 0.0)
        ),
    )

    part_incoming = _release(
        "/downloads/Azure Adventures v01 Part 12.cbz",
        "Azure Adventures",
        number=ReleaseNumber.scalar("1", part=12),
    )
    decimal_volume = _release(
        "/library/Azure Chronicles/Azure Chronicles v01.2.cbz",
        "Azure Chronicles",
        number=ReleaseNumber.scalar("1.2"),
    )
    matching_part = _release(
        "/library/Azure Chronicles/Azure Chronicles v01 Part 12.cbz",
        "Azure Chronicles",
        number=ReleaseNumber.scalar("1", part=12),
    )
    part_rest = tuple(
        _release(
            f"/library/Azure Chronicles/Azure Chronicles v{index:02}.cbz",
            "Azure Chronicles",
            number=ReleaseNumber.scalar(str(index)),
        )
        for index in range(2, 11)
    )
    compared_part: list[Path] = []
    part_decision = match_series(
        part_incoming,
        _library((decimal_volume, matching_part, *part_rest)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared_part.append(existing.path)
            or (0.95 if existing is matching_part else 0.0)
        ),
    )

    assert collection_decision.status == "matched"
    assert compared_collection[0] == collection.path
    assert part_decision.status == "matched"
    assert compared_part[0] == matching_part.path


def test_cover_match_survives_missing_comparison_in_same_proven_folder() -> None:
    incoming = _release(
        "/downloads/Azure Adventures - Shared Tale v01.cbz",
        "Azure Adventures - Shared Tale",
        number=ReleaseNumber.scalar("1"),
    )
    missing = _release(
        "/library/Azure Chronicles - Shared Tale/Azure Chronicles - Shared Tale v01.cbz",
        "Azure Chronicles - Shared Tale",
        number=ReleaseNumber.scalar("1"),
    )
    qualifying = _release(
        "/library/Azure Chronicles - Shared Tale/Azure Chronicles - Shared Tale v02.cbz",
        "Azure Chronicles - Shared Tale",
        number=ReleaseNumber.scalar("2"),
    )
    compared: list[Path] = []

    decision = match_series(
        incoming,
        _library((missing, qualifying)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared.append(existing.path)
            or (None if existing is missing else 0.95)
        ),
    )

    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == missing.parent
    assert compared == [missing.path, qualifying.path]


def test_missing_cover_in_another_unproven_folder_blocks_unique_selection() -> None:
    incoming = _release(
        "/downloads/Azure Adventures - Shared Tale v01.cbz",
        "Azure Adventures - Shared Tale",
        number=ReleaseNumber.scalar("1"),
    )
    proven_missing = _release(
        "/library/Azure Chronicles - Shared Tale/Azure Chronicles - Shared Tale v01.cbz",
        "Azure Chronicles - Shared Tale",
        number=ReleaseNumber.scalar("1"),
    )
    proven_match = _release(
        "/library/Azure Chronicles - Shared Tale/Azure Chronicles - Shared Tale v02.cbz",
        "Azure Chronicles - Shared Tale",
        number=ReleaseNumber.scalar("2"),
    )
    unresolved = _release(
        "/library/Azure Legends - Shared Tale/Azure Legends - Shared Tale v01.cbz",
        "Azure Legends - Shared Tale",
        number=ReleaseNumber.scalar("1"),
    )

    decision = match_series(
        incoming,
        _library((proven_missing, proven_match, unresolved)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            None
            if existing in {proven_missing, unresolved}
            else 0.95
        ),
    )

    assert decision.status == "missing_dependency"
    assert decision.selected is None
    assert tuple(item.series_path for item in decision.candidates) == (
        proven_missing.parent,
    )


def test_identifier_fallback_requires_a_volume_and_matching_extension() -> None:
    shared_identifier = (("isbn", "9780000000000"),)
    chapter_incoming = _release(
        "/downloads/Alias c02.cbz",
        "Alias",
        kind="chapter",
        identifiers=shared_identifier,
    )
    chapter_existing = _release(
        "/library/Other Chapter/Other Chapter c01.cbz",
        "Other Chapter",
        kind="chapter",
        identifiers=shared_identifier,
    )
    chapter_decision = match_series(
        chapter_incoming,
        _library((chapter_existing,)),
        _policy(image=False),
    )

    volume_incoming = _release(
        "/downloads/Alias v02.cbz",
        "Alias",
        identifiers=shared_identifier,
    )
    zip_existing = _release(
        "/library/Other Series/Other Series v01.zip",
        "Other Series",
        identifiers=shared_identifier,
    )
    extension_decision = match_series(
        volume_incoming,
        _library((zip_existing,), extensions=(".cbz", ".zip")),
        _policy(image=False),
    )

    assert chapter_decision.status == "no_match"
    assert extension_decision.status == "no_match"


def test_filename_alias_requires_identifier_evidence_when_its_folder_name_does_not_match() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release("/downloads/Incoming/Series v02.cbz", "Series", identifiers=identifier)
    collection = _release(
        "/library/Other Collection/Series v01.cbz", "Series", identifiers=identifier,
    )
    named_folder = _release("/library/Series/Series v03.cbz", "Series")

    primary = match_series(incoming, _library((collection, named_folder)), _policy(image=False))
    fallback = match_series(incoming, _library((collection,)), _policy(image=False))

    assert primary.status == "matched"
    assert primary.selected is not None
    assert primary.selected.series_path == Path("/library/Series")
    assert fallback.status == "matched"
    assert fallback.selected is not None
    assert fallback.selected.series_path == Path("/library/Other Collection")
    assert fallback.selected.evidence == "unique_identifier"


def test_cached_identifier_observation_precedes_a_prioritized_name_match() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release(
        "/downloads/Alias v02.cbz",
        "Alias",
        identifiers=identifier,
        digest="incoming archive",
    )
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="real series archive",
    )
    alias = _release(
        "/library/Alias/Alias v01.cbz",
        "Alias",
        digest="alias archive",
    )
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)

    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    second = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        evidence_cache=cache,
        now=150.0,
    )

    assert first.status == "matched"
    assert len(cache) == 1
    assert second.status == "matched"
    assert second.selected is not None
    assert second.selected.series_path == existing.parent


def test_cached_cover_observation_precedes_a_prioritized_name_match() -> None:
    incoming = _release(
        "/downloads/Alias - Shared Tale v01.cbz",
        "Alias - Shared Tale",
        number=ReleaseNumber.scalar("1"),
        digest="incoming archive",
    )
    existing = _release(
        "/library/Real Series - Shared Tale/Real Series - Shared Tale v01.cbz",
        "Real Series - Shared Tale",
        number=ReleaseNumber.scalar("1"),
        digest="real series archive",
    )
    alias = _release(
        "/library/Alias - Shared Tale/Alias - Shared Tale v01.cbz",
        "Alias - Shared Tale",
        number=ReleaseNumber.scalar("1"),
        digest="alias archive",
    )
    policy = _policy(image=True, similarity=1.0)
    cache = MatchEvidenceCache(policy)

    first = match_series(
        incoming,
        _library((existing,)),
        policy,
        evidence_cache=cache,
        cover_similarity=lambda _incoming, _existing: 0.95,
        now=100.0,
    )
    second = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        cover_similarity=lambda _incoming, _existing: 0.1,
        evidence_cache=cache,
        now=150.0,
    )

    assert first.status == "matched"
    assert second.status == "matched"
    assert second.selected is not None
    assert second.selected.series_path == existing.parent


@pytest.mark.parametrize(
    "change", ("expired", "incoming_changed", "candidate_changed", "candidate_removed")
)
def test_expired_or_changed_evidence_does_not_prune_current_candidates(change: str) -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release(
        "/downloads/Alias v02.cbz",
        "Alias",
        identifiers=identifier,
        digest="incoming archive",
    )
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="real series archive",
    )
    alias = _release("/library/Alias/Alias v01.cbz", "Alias", digest="alias archive")
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    assert first.status == "matched"
    assert len(cache) == 1

    if change == "incoming_changed":
        incoming = _release(
            incoming.path.as_posix(),
            "Alias",
            identifiers=identifier,
            digest="replacement incoming archive",
        )
    if change == "candidate_changed":
        existing = _release(
            existing.path.as_posix(),
            "Real Series",
            identifiers=identifier,
            digest="replacement existing archive",
        )
    releases = (alias,) if change == "candidate_removed" else (existing, alias)
    decision = match_series(
        incoming,
        _library(releases),
        policy,
        evidence_cache=cache,
        now=400.0 if change == "expired" else 150.0,
    )

    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == alias.parent
    assert len(cache) == 0


def test_evidence_cache_is_policy_scoped() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release(
        "/downloads/Alias v02.cbz",
        "Alias",
        identifiers=identifier,
        digest="incoming archive",
    )
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="real series archive",
    )
    alias = _release(
        "/library/Alias/Alias v01.cbz", "Alias", digest="alias archive"
    )
    first_policy = _policy(image=False, similarity=0.9)
    changed_policy = _policy(image=False, similarity=0.95)
    cache = MatchEvidenceCache(first_policy)

    first = match_series(
        incoming,
        _library((existing,)),
        first_policy,
        evidence_cache=cache,
        now=100.0,
    )
    second = match_series(
        incoming,
        _library((existing, alias)),
        changed_policy,
        evidence_cache=cache,
        now=150.0,
    )

    assert first.status == "matched"
    assert second.status == "matched"
    assert second.selected is not None
    assert second.selected.series_path == alias.parent


def test_evidence_cache_is_bounded_to_4096_observed_candidates() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release(
        "/downloads/Incoming Series v02.cbz",
        "Incoming Series",
        identifiers=identifier,
        digest="incoming archive",
    )
    releases = tuple(
        _release(
            f"/library/Series {index:04}/Series {index:04} v01.cbz",
            f"Series {index:04}",
            identifiers=identifier,
            digest=f"series archive {index}",
        )
        for index in range(4100)
    )
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)

    decision = match_series(
        incoming,
        _library(releases),
        policy,
        evidence_cache=cache,
        now=100.0,
    )

    assert decision.status == "ambiguous"
    assert len(cache) == 4096
    second = match_series(
        incoming,
        _library(releases),
        policy,
        evidence_cache=cache,
        now=150.0,
    )
    assert second.status == "ambiguous"
    assert second.selected is None
    assert len(second.candidates) == 4100
    assert tuple(candidate.series_path for candidate in second.candidates[:4]) == (
        Path("/library/Series 0000"),
        Path("/library/Series 0001"),
        Path("/library/Series 0002"),
        Path("/library/Series 0003"),
    )
    assert len(cache) == 4096


@pytest.fixture
def cover_evidence() -> tuple[
    ReleaseSnapshot, ReleaseSnapshot, ReleaseSnapshot, MatchingPolicy, MatchEvidenceCache
]:
    incoming = _release(
        "/downloads/Alias - Shared Tale v01.cbz",
        "Alias - Shared Tale",
        number=ReleaseNumber.scalar("1"),
        digest="incoming archive",
    )
    existing = _release(
        "/library/Real Series - Shared Tale/Real Series - Shared Tale v01.cbz",
        "Real Series - Shared Tale",
        number=ReleaseNumber.scalar("1"),
        digest="existing archive",
    )
    alias = _release(
        "/library/Alias - Shared Tale/Alias - Shared Tale v01.cbz",
        "Alias - Shared Tale",
        number=ReleaseNumber.scalar("1"),
        digest="alias archive",
    )
    policy = _policy(image=True, similarity=1.0)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming,
        _library((existing,)),
        policy,
        cover_similarity=lambda _incoming, _existing: 0.95,
        evidence_cache=cache,
        now=100.0,
    )
    assert first.status == "matched"
    assert first.selected is not None
    assert first.selected.series_path == Path("/library/Real Series - Shared Tale")
    return incoming, existing, alias, policy, cache


def test_cached_cover_checks_new_candidates_before_unique_selection(cover_evidence) -> None:
    incoming, existing, alias, policy, cache = cover_evidence
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.96,
        evidence_cache=cache,
        now=150.0,
    )
    assert decision.status == "ambiguous"
    assert decision.selected is None
    assert {candidate.series_path for candidate in decision.candidates} == {
        existing.parent, alias.parent
    }
    assert compared == [alias.path]


def test_cached_cover_cannot_hide_an_unresolved_new_folder(cover_evidence) -> None:
    incoming, existing, alias, policy, cache = cover_evidence
    decision = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        cover_similarity=lambda _incoming, _release: None,
        evidence_cache=cache,
        now=150.0,
    )
    assert decision.status == "missing_dependency"
    assert decision.selected is None
    assert tuple(candidate.series_path for candidate in decision.candidates) == (
        existing.parent,
    )


@pytest.mark.parametrize("other_score", [-1.0, -0.5, 0.1])
def test_cached_negative_cover_observations_preserve_all_candidates(cover_evidence, other_score: float) -> None:
    incoming, existing, alias, policy, cache = cover_evidence
    compared: list[Path] = []
    second = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or other_score,
        evidence_cache=cache,
        now=150.0,
    )
    third = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        evidence_cache=cache,
        now=200.0,
    )
    for decision in (second, third):
        assert decision.status == "matched"
        assert decision.selected is not None
        assert decision.selected.series_path == existing.parent
    assert compared == [alias.path]
    assert len(cache) == 2


def test_cached_cover_read_does_not_extend_its_ttl(cover_evidence) -> None:
    incoming, existing, alias, policy, cache = cover_evidence
    fresh = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=399.0
    )
    expired = match_series(
        incoming,
        _library((existing, alias)),
        policy,
        evidence_cache=cache,
        now=400.0,
    )
    assert fresh.status == "matched"
    assert fresh.selected is not None
    assert fresh.selected.series_path == existing.parent
    assert expired.status == "matched"
    assert expired.selected is not None
    assert expired.selected.series_path == alias.parent
    assert len(cache) == 0


@pytest.mark.parametrize("source", ("incoming", "candidate"))
@pytest.mark.parametrize("field", ("mtime_ns", "inode", "size", "sha256"))
def test_cover_cache_rechecks_every_observed_source_fingerprint_change(
    cover_evidence, source: str, field: str
) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    changed = incoming if source == "incoming" else existing
    fingerprint = changed.observation.fingerprint
    assert fingerprint is not None
    value = sha256(b"replacement archive").hexdigest() if field == "sha256" else (
        getattr(fingerprint, field) + 1
    )
    changed = replace(
        changed,
        observation=replace(
            changed.observation, fingerprint=replace(fingerprint, **{field: value})
        ),
    )
    if source == "incoming":
        incoming = changed
    else:
        existing = changed
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.1,
        evidence_cache=cache,
        now=150.0,
    )
    assert decision.status == "no_match"
    assert decision.selected is None
    assert compared == [existing.path]
    assert len(cache) == 1


@pytest.mark.parametrize("source", ("incoming", "candidate", "incoming_folder", "candidate_folder"))
def test_explicit_cover_cache_invalidation_rechecks_the_source(cover_evidence, source: str) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    paths = {
        "incoming": incoming.path,
        "candidate": existing.path,
        "incoming_folder": incoming.parent,
        "candidate_folder": existing.parent,
    }
    cache.invalidate(paths[source])
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.1,
        evidence_cache=cache,
        now=150.0,
    )
    assert decision.status == "no_match"
    assert compared == [existing.path]


def test_cover_cache_policy_change_rechecks_current_observations(cover_evidence) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    changed_policy = replace(policy, required_image_similarity_score=0.99)
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        changed_policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.1,
        evidence_cache=cache,
        now=150.0,
    )
    assert decision.status == "no_match"
    assert compared == [existing.path]


@pytest.mark.parametrize("now", (99.0, 400.0))
def test_cover_cache_clock_rollback_and_expiry_recheck_observations(cover_evidence, now: float) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.1,
        evidence_cache=cache,
        now=now,
    )
    assert decision.status == "no_match"
    assert compared == [existing.path]


def test_cached_identifier_considers_new_matching_folders_before_names() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release("/downloads/Alias v02.cbz", "Alias", identifiers=identifier, digest="in")
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="existing",
    )
    alias = _release(
        "/library/Alias/Alias v01.cbz", "Alias", identifiers=identifier, digest="alias"
    )
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    second = match_series(
        incoming, _library((existing, alias)), policy, evidence_cache=cache, now=150.0
    )
    assert first.status == "matched"
    assert second.status == "ambiguous"
    assert second.selected is None
    assert tuple(candidate.series_path for candidate in second.candidates) == (
        alias.parent, existing.parent
    )


def test_cached_identifier_read_does_not_extend_its_ttl() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release("/downloads/Alias v02.cbz", "Alias", identifiers=identifier, digest="in")
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="existing",
    )
    alias = _release("/library/Alias/Alias v01.cbz", "Alias", digest="alias")
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    fresh = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=399.0
    )
    expired = match_series(
        incoming, _library((existing, alias)), policy, evidence_cache=cache, now=400.0
    )
    assert first.status == "matched"
    assert fresh.status == "matched"
    assert fresh.selected is not None
    assert fresh.selected.series_path == existing.parent
    assert expired.status == "matched"
    assert expired.selected is not None
    assert expired.selected.series_path == alias.parent


def test_name_score_cache_invalidation_preserves_new_valid_candidates() -> None:
    incoming = _release("/downloads/Azure Tale v02.cbz", "Azure Tale", digest="incoming")
    existing = _release(
        "/library/Azure Tale Q/Azure Tale Q v01.cbz", "Azure Tale Q", digest="existing"
    )
    new = _release("/library/Azure Tale/Azure Tale v01.cbz", "Azure Tale", digest="new")
    policy = _policy(similarity=0.9)
    cache = MatchScoreCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, cache=cache, now=100.0
    )
    cache.invalidate(existing.parent)
    second = match_series(
        incoming, _library((existing, new)), policy, cache=cache, now=150.0
    )
    assert first.status == "matched"
    assert first.selected is not None
    assert first.selected.series_path == existing.parent
    assert second.status == "matched"
    assert second.selected is not None
    assert second.selected.series_path == new.parent
    assert len(cache) == 0


def test_cached_image_evidence_precedes_fresh_identifiers_and_names(cover_evidence) -> None:
    incoming, existing, alias, policy, cache = cover_evidence
    incoming = replace(incoming, identifiers=(("isbn", "9780000000000"),))
    identified = _release(
        "/library/Unrelated Book/Unrelated Book v01.cbz",
        "Unrelated Book",
        identifiers=incoming.identifiers,
        digest="identified archive",
    )
    decision = match_series(
        incoming,
        _library((existing, identified, alias)),
        policy,
        evidence_cache=cache,
        cover_similarity=lambda _incoming, _release: 0.1,
        now=150.0,
    )
    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == existing.parent


@pytest.mark.parametrize("missing_source", ("incoming", "candidate"))
def test_cover_evidence_requires_both_current_fingerprints(cover_evidence, missing_source: str) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    if missing_source == "incoming":
        incoming = replace(incoming, observation=replace(incoming.observation, fingerprint=None))
    else:
        existing = replace(existing, observation=replace(existing.observation, fingerprint=None))
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        policy,
        evidence_cache=cache,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.1,
        now=150.0,
    )
    assert decision.status == "no_match"
    assert compared == [existing.path]
    assert len(cache) == 0


def test_cover_cache_invalidation_uses_path_components(cover_evidence) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    cache.invalidate(Path("/library/Real Series"))
    decision = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=150.0
    )
    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == existing.parent


@pytest.mark.parametrize("volume_count", (8, 9))
def test_cached_identifier_rechecks_mixed_folder_suitability(volume_count: int) -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release("/downloads/Alias v02.cbz", "Alias", identifiers=identifier, digest="in")
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="existing",
    )
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    additional = tuple(
        _release(
            f"/library/Real Series/Real Series {'v' if index <= volume_count else 'c'}{index:02}.cbz",
            "Real Series",
            kind="volume" if index <= volume_count else "chapter",
            digest=f"additional {index}",
        )
        for index in range(2, 11)
    )
    decision = match_series(
        incoming,
        _library((existing, *additional)),
        policy,
        evidence_cache=cache,
        now=150.0,
    )
    assert first.status == "matched"
    if volume_count == 9:
        assert decision.status == "matched"
        assert decision.selected is not None
        assert decision.selected.series_path == existing.parent
    else:
        assert decision.status == "no_match"
        assert decision.selected is None


@pytest.mark.parametrize("epub_count", (1, 2))
def test_folder_suitability_checks_medium_and_kind_independently(epub_count: int) -> None:
    incoming = _release("/downloads/Series v99.cbz", "Series")
    releases = tuple(
        _release(
            f"/library/Series/Series v{index + 1:02}.{'epub' if index < epub_count else 'cbz'}",
            "Series",
            kind="volume",
        )
        for index in range(10)
    )
    decision = match_series(
        incoming, _library(releases, extensions=(".cbz", ".epub")), _policy()
    )
    if epub_count == 1:
        assert decision.status == "matched"
        assert decision.selected is not None
        assert decision.selected.series_path == Path("/library/Series")
    else:
        assert decision.status == "no_match"


def test_alternative_gate_counts_empty_and_grouping_sibling_directories() -> None:
    incoming = _release("/downloads/Azure Adventures v02.cbz", "Azure Adventures")
    existing = _release(
        "/library/Azure Chronicles/Azure Chronicles v01.cbz", "Azure Chronicles"
    )
    policy = _policy(image=True, similarity=1.0)
    compared: list[Path] = []
    allowed = match_series(
        incoming,
        _library((existing,)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.95,
        directory_siblings={Path("/library"): ("Azure Chronicles",)},
    )
    blocked = match_series(
        incoming,
        _library((existing,)),
        policy,
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.95,
        directory_siblings={
            Path("/library"): (
                "Azure Chronicles", "Azure Empty", "Azure Grouping", "Azure Other"
            )
        },
    )
    assert allowed.status == "matched"
    assert allowed.selected is not None
    assert allowed.selected.series_path == existing.parent
    assert blocked.status == "no_match"
    assert compared == [existing.path]


def test_alternative_gate_derives_grouping_directories_from_observed_descendants() -> None:
    incoming = _release("/downloads/Azure Adventures v02.cbz", "Azure Adventures")
    existing = _release(
        "/library/Azure Chronicles/Azure Chronicles v01.cbz", "Azure Chronicles"
    )
    grouped = tuple(
        _release(
            f"/library/Azure Group {index}/Child {index}/Other v01.cbz",
            f"Other {index}",
        )
        for index in range(3)
    )
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing, *grouped)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.95,
    )
    assert decision.status == "no_match"
    assert compared == []


@pytest.mark.parametrize(
    ("folder", "expected"),
    (
        ("Old Delta Amber Cedar Chronicles", "matched"),
        ("Old Amber Cedar Delta Chronicles", "no_match"),
    ),
)
def test_alternative_gate_preserves_consecutive_word_order(folder: str, expected: str) -> None:
    incoming = _release(
        "/downloads/Incoming Delta Amber Cedar Arc v02.cbz", "Incoming Delta Amber Cedar Arc"
    )
    existing = _release(f"/library/{folder}/{folder} v01.cbz", folder)
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.95,
        directory_siblings={
            Path("/library"): (
                folder,
                "Delta Amber Cedar Empty",
                "Delta Amber Cedar Grouping",
                "Delta Amber Cedar Other",
            )
        },
    )
    assert decision.status == expected
    if expected == "matched":
        assert decision.selected is not None
        assert decision.selected.series_path == existing.parent
        assert compared == [existing.path]
    else:
        assert decision.selected is None
        assert compared == []


@pytest.mark.parametrize("repetitions", (3, 4))
def test_alternative_gate_counts_repeated_words_within_each_directory(repetitions: int) -> None:
    incoming = _release("/downloads/Azure Journey v02.cbz", "Azure Journey")
    folder = " ".join(("Azure",) * repetitions) + " Story"
    existing = _release(f"/library/{folder}/{folder} v01.cbz", folder)
    compared: list[Path] = []
    decision = match_series(
        incoming,
        _library((existing,)),
        _policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, release: compared.append(release.path) or 0.95,
    )
    if repetitions == 3:
        assert decision.status == "matched"
        assert decision.selected is not None
        assert decision.selected.series_path == existing.parent
        assert compared == [existing.path]
    else:
        assert decision.status == "no_match"
        assert compared == []


def test_identifier_namespace_and_extension_matching_are_case_insensitive() -> None:
    incoming = _release(
        "/downloads/Alias v02.cbz", "Alias", identifiers=((" ISBN ", " SHARED-ID "),)
    )
    existing = _release(
        "/library/Other Series/Other Series v01.CBZ",
        "Other Series",
        identifiers=(("isbn", "shared-id"),),
    )
    decision = match_series(incoming, _library((existing,)), _policy(image=False))
    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == existing.parent
    assert decision.selected.identifiers == (("isbn", "shared-id"),)


def test_new_identifier_observations_replace_obsolete_cached_identifiers() -> None:
    incoming = _release(
        "/downloads/Alias v02.cbz", "Alias", identifiers=(("isbn", "first-id"),), digest="in"
    )
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=(("isbn", "first-id"),),
        digest="existing",
    )
    alias = _release("/library/Alias/Alias v01.cbz", "Alias", digest="alias")
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    incoming = replace(incoming, identifiers=(("isbn", "newly-observed-id"),))
    existing = replace(existing, identifiers=(("isbn", "newly-observed-id"),))
    updated = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=150.0
    )
    cached = match_series(
        incoming, _library((existing, alias)), policy, evidence_cache=cache, now=200.0
    )
    assert first.status == "matched"
    assert updated.status == "matched"
    assert updated.selected is not None
    assert updated.selected.identifiers == (("isbn", "newly-observed-id"),)
    assert updated.selected.releases == (existing,)
    assert cached.status == "matched"
    assert cached.selected is not None
    assert cached.selected.series_path == existing.parent
    assert cached.selected.releases == (existing,)
    assert len(cache) == 1


def test_identifier_cache_invalidation_restores_normal_tier_evaluation() -> None:
    identifier = (("isbn", "9780000000000"),)
    incoming = _release("/downloads/Alias v02.cbz", "Alias", identifiers=identifier, digest="in")
    existing = _release(
        "/library/Real Series/Real Series v01.cbz",
        "Real Series",
        identifiers=identifier,
        digest="existing",
    )
    alias = _release("/library/Alias/Alias v01.cbz", "Alias", digest="alias")
    policy = _policy(image=False)
    cache = MatchEvidenceCache(policy)
    first = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=100.0
    )
    cache.invalidate(existing.parent)
    decision = match_series(
        incoming, _library((existing, alias)), policy, evidence_cache=cache, now=150.0
    )
    assert first.status == "matched"
    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == alias.parent
    assert len(cache) == 0


@pytest.mark.parametrize("score", (False, -1.1, 1.1, float("nan"), float("inf"), "0.95"))
def test_invalid_cover_observations_are_not_cached_as_success(cover_evidence, score) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    cache.invalidate(existing.path)
    decision = match_series(
        incoming,
        _library((existing,)),
        policy,
        evidence_cache=cache,
        cover_similarity=lambda _incoming, _release: score,
        now=150.0,
    )
    assert decision.status == "invalid_input"
    assert decision.selected is None
    assert len(cache) == 0


@pytest.mark.parametrize("now", (True, float("nan"), float("inf")))
def test_invalid_cache_times_do_not_consume_cached_proof(cover_evidence, now) -> None:
    incoming, existing, _alias, policy, cache = cover_evidence
    invalid = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=now
    )
    valid = match_series(
        incoming, _library((existing,)), policy, evidence_cache=cache, now=150.0
    )
    assert invalid.status == "invalid_input"
    assert valid.status == "matched"
    assert valid.selected is not None
    assert valid.selected.series_path == existing.parent

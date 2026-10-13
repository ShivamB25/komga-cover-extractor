from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.operations import FileFingerprint
from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseSnapshot,
)
from komga_cover_extractor.matching.classification import (
    ClassificationPolicy,
    LibraryType,
    check_root_suitability,
    classify_library_type,
    classify_root,
    folder_name_is_rename_candidate,
    folder_series_agreement,
    route_releases_to_root,
)
from komga_cover_extractor.matching.ranking import (
    RankingPolicy,
    RankRule,
    decide_upgrade,
    rank_release,
)
from komga_cover_extractor.matching.series import (
    MatchingPolicy,
    MatchScoreCache,
    match_series,
)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _release(
    path: str,
    series: str,
    *,
    kind: str = "volume",
    digest: str | None = None,
    identifiers: tuple[tuple[str, str], ...] = (),
    metadata: tuple[tuple[str, str], ...] = (),
) -> ReleaseSnapshot:
    fingerprint = None
    if digest is not None:
        file_path = Path(path)
        fingerprint = FileFingerprint(
            root=file_path.parent,
            relative_path=file_path.name,
            device=1,
            inode=1,
            size=0,
            mtime_ns=0,
            sha256=digest,
        )
    return ReleaseSnapshot(
        observation=FileObservation(
            Path(path), metadata=metadata, fingerprint=fingerprint
        ),
        kind=kind,
        display_series=series,
        canonical_series_key=series.casefold().strip(),
        identifiers=identifiers,
    )


def _matching_policy(
    *,
    similarity: float = 0.979,
    image: bool = False,
    identifiers: bool = True,
    required_matching_percentage: float = 90.0,
    classification: ClassificationPolicy | None = None,
) -> MatchingPolicy:
    return MatchingPolicy(
        required_similarity_score=similarity,
        required_matching_percentage=required_matching_percentage,
        match_through_image_similarity=image,
        required_image_similarity_score=0.9,
        match_through_identifiers=identifiers,
        classification=classification or ClassificationPolicy(),
    )


def test_rank_release_applies_ordered_regex_and_file_type_rules() -> None:
    release = _release(
        "/downloads/Series v01 (Digital) [Web].cbz", "Series", kind="volume"
    )
    rules = (
        RankRule(r"Digital", 2.0),
        RankRule(r"Web", 4.0, "chapter"),
        RankRule(r"\[Web\]", 1.0, "volume"),
    )

    ranked = rank_release(release, rules)

    assert ranked.status == "ranked"
    assert ranked.reason == "matched_rules"
    assert ranked.total_score == 3.0
    assert [match.rule for match in ranked.candidates] == [rules[0], rules[2]]
    assert ranked.scores == (2.0, 1.0)


def test_rank_release_reports_unknown_release_kind_without_scoring() -> None:
    ranked = rank_release(
        _release("/downloads/Series.cbz", "Series", kind="unknown"),
        (RankRule(".*", 1),),
    )

    assert ranked.status == "invalid_input"
    assert ranked.reason == "unknown_release_kind"
    assert ranked.total_score == 0
    assert ranked.candidates == ()


def test_upgrade_requires_strictly_higher_rank() -> None:
    incoming = _release(
        "/downloads/Series.cbz", "Series", digest=_digest("incoming")
    )
    existing = _release(
        "/library/Series.cbz", "Series", digest=_digest("existing")
    )
    policy = RankingPolicy((RankRule("Preferred", 2),))

    incoming_wins = decide_upgrade(
        _release(
            "/downloads/Series (Preferred).cbz", "Series", digest=_digest("new")
        ),
        existing,
        policy,
    )
    equal_rank = decide_upgrade(incoming, existing, policy)

    assert incoming_wins.status == "upgrade"
    assert incoming_wins.is_upgrade is True
    assert incoming_wins.reason == "strictly_higher_rank"
    assert equal_rank.status == "review_required"
    assert equal_rank.reason == "equal_rank_different_content"
    assert equal_rank.is_upgrade is None


def test_equal_rank_unknown_hash_is_review_and_equal_hash_is_not_an_upgrade() -> None:
    policy = RankingPolicy((RankRule("Digital", 1),))
    unknown = decide_upgrade(
        _release("/downloads/Series.cbz", "Series"),
        _release("/library/Series.cbz", "Series"),
        policy,
    )
    same = _digest("same bytes")
    identical = decide_upgrade(
        _release("/downloads/Series.cbz", "Series", digest=same),
        _release("/library/Series.cbz", "Series", digest=same),
        policy,
    )

    assert unknown.status == "review_required"
    assert unknown.reason == "equal_rank_content_identity_unknown"
    assert identical.status == "not_upgrade"
    assert identical.reason == "identical_content"


def test_lower_rank_is_not_an_upgrade_even_with_different_content() -> None:
    policy = RankingPolicy((RankRule("Digital", 1),))
    decision = decide_upgrade(
        _release("/downloads/Series.cbz", "Series", digest=_digest("plain")),
        _release("/library/Series (Digital).cbz", "Series", digest=_digest("digital")),
        policy,
    )

    assert decision.status == "not_upgrade"
    assert decision.reason == "lower_rank"
    assert decision.scores == (0.0, 1.0)


def test_library_type_classification_uses_ordered_rules_and_empty_is_no_match() -> None:
    general = LibraryType("general", (".cbz",), (r"Digital",), (), 90)
    specialized = LibraryType(
        "digital_comps", (".cbz",), (r"Digital", r"Compilation"), (), 90
    )
    policy = ClassificationPolicy(library_types=(general, specialized))

    result = classify_library_type(
        ("Series (Digital Compilation).cbz", "Another (Digital).cbz"), policy
    )
    empty = classify_library_type((), policy)
    no_files = classify_library_type(("Series.cbz",), ClassificationPolicy())

    assert result.status == "matched"
    assert result.library_type is general
    assert result.reason == "first_ordered_rule_matched"
    assert result.scores[0].matched_files == 2
    assert empty.status == "no_match" and empty.reason == "empty_input"
    assert no_files.status == "no_match" and no_files.library_type is None


def test_root_auto_classification_uses_named_sample_and_thresholds() -> None:
    releases = tuple(
        _release(
            f"/library/Book {index:03}.cbz" if index < 30
            else f"/library/Book {index:03}.epub",
            "Book",
            kind="chapter" if index < 75 else "volume",
        )
        for index in range(100)
    )

    classified = classify_root(releases, ClassificationPolicy())

    assert classified.status == "matched"
    assert classified.sample_size == 75
    assert classified.chapter_percentage == 100.0
    assert classified.formats == ("chapter",)
    assert classified.extension_percentages == ((".cbz", 40.0), (".epub", 60.0))
    assert classified.extensions == (".cbz", ".epub", ".zip")
    assert classify_root((), ClassificationPolicy()).reason == "empty_input"


def test_folder_consensus_and_rename_floor_are_explicit_policy() -> None:
    releases = (
        _release("/library/Series/01.cbz", "Series Name"),
        _release("/library/Series/02.cbz", "Series Name"),
        _release("/library/Series/03.cbz", "Series Name"),
        _release("/library/Series/04.cbz", "Series Name"),
        _release("/library/Series/05.cbz", "Unrelated Title"),
    )
    policy = ClassificationPolicy(required_similarity_score=0.95)

    agreement = folder_series_agreement(releases, policy)

    assert agreement.status == "matched"
    assert agreement.agreement == pytest.approx(0.8)
    assert not folder_name_is_rename_candidate("A", "B", policy)
    assert folder_name_is_rename_candidate("Series", "S", policy)
    assert folder_series_agreement((), policy).status == "no_match"


def test_root_suitability_checks_every_extension_and_format() -> None:
    root = LibraryRoot(
        Path("/library"), formats=("volume",), extensions=(".cbz",)
    )
    policy = ClassificationPolicy()
    allowed = _release("/downloads/Series v01.cbz", "Series", kind="volume")
    disallowed = _release("/downloads/Series.epub", "Series", kind="volume")
    wrong_kind = _release("/downloads/Series v01.cbz", "Series", kind="chapter")

    assert check_root_suitability((allowed,), root, policy).status == "suitable"
    assert check_root_suitability((allowed, disallowed), root, policy).reason == "extension_restriction"
    assert check_root_suitability((wrong_kind,), root, policy).reason == "format_restriction"
    assert check_root_suitability((), root, policy).reason == "empty_input"


def test_root_routing_rejects_zero_and_multiple_destinations() -> None:
    release = _release("/downloads/Series v01.cbz", "Series")
    policy = ClassificationPolicy()
    rejected = route_releases_to_root(
        (release,),
        (LibraryRoot(Path("/manga"), extensions=(".epub",)),),
        policy,
    )
    ambiguous = route_releases_to_root(
        (release,),
        (
            LibraryRoot(Path("/manga"), extensions=(".cbz",)),
            LibraryRoot(Path("/comics"), extensions=(".cbz",)),
        ),
        policy,
    )

    assert rejected.status == "no_match"
    assert rejected.reason == "no_suitable_destination"
    assert ambiguous.status == "ambiguous"
    assert ambiguous.reason == "multiple_suitable_destinations"
    assert len(ambiguous.candidates) == 2


def test_series_matching_prioritizes_exact_name_and_reports_ambiguity() -> None:
    incoming = _release("/downloads/Series v02.cbz", "Series")
    first = LibraryRoot(Path("/library-a"), extensions=(".cbz",))
    second = LibraryRoot(Path("/library-b"), extensions=(".cbz",))
    releases = (
        _release("/library-a/Series/Series v01.cbz", "Series"),
        _release("/library-b/Series/Series v01.cbz", "Series"),
    )
    library = LibrarySnapshot(releases=releases, roots=(first, second))

    decision = match_series(incoming, library, _matching_policy())

    assert decision.status == "ambiguous"
    assert decision.reason == "prioritized_name_match"
    assert decision.selected is None
    assert {candidate.series_path for candidate in decision.candidates} == {
        Path("/library-a/Series"), Path("/library-b/Series")
    }


def test_series_matching_full_name_tier_does_not_choose_first_candidate() -> None:
    incoming = _release("/downloads/Series.cbz", "Series")
    releases = (
        _release("/library/Series Alpha/Series Alpha.cbz", "Series Alpha"),
        _release("/library/Series Beta/Series Beta.cbz", "Series Beta"),
    )
    library = LibrarySnapshot(
        releases=releases,
        roots=(LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )

    decision = match_series(
        incoming, library, _matching_policy(similarity=0.6)
    )

    assert decision.status == "ambiguous"
    assert decision.reason == "full_name_match"
    assert len(decision.candidates) == 2
    assert decision.selected is None


def test_identifier_tier_requires_a_unique_candidate_and_library_availability() -> None:
    incoming = _release(
        "/downloads/Incoming v02.cbz", "Incoming",
        identifiers=(("isbn", "9780000000000"),),
    )
    matching_one = _release(
        "/library/Series One/Series One v01.cbz", "Series One",
        identifiers=(("isbn", "9780000000000"),),
    )
    matching_two = _release(
        "/library/Series Two/Series Two v01.cbz", "Series Two",
        identifiers=(("isbn", "9780000000000"),),
    )
    root = LibraryRoot(Path("/library"), extensions=(".cbz",))
    policy = _matching_policy(identifiers=True)
    ambiguous = match_series(
        incoming,
        LibrarySnapshot((matching_one, matching_two), (root,)),
        policy,
    )
    unique = match_series(
        incoming, LibrarySnapshot((matching_one,), (root,)), policy
    )
    unavailable = match_series(
        incoming, LibrarySnapshot(available=False, reason="root_unavailable"), policy
    )

    assert ambiguous.status == "ambiguous"
    assert ambiguous.reason == "unique_identifier_match"
    assert unique.status == "matched"
    assert unique.selected is not None
    assert unique.selected.evidence == "unique_identifier"
    assert unavailable.status == "missing_dependency"
    assert unavailable.reason == "root_unavailable"


def test_candidate_extension_restrictions_prevent_name_match() -> None:
    incoming = _release("/downloads/Series v02.epub", "Series")
    existing = _release("/library/Series/Series v01.cbz", "Series")
    library = LibrarySnapshot(
        (existing,),
        (LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )

    decision = match_series(incoming, library, _matching_policy())

    assert decision.status == "no_match"
    assert decision.reason == "no_suitable_series_folder_or_root"


def test_observed_cover_similarity_is_a_tier_before_identifiers() -> None:
    incoming = _release(
        "/downloads/Different Series - Shared Subtitle v02.cbz",
        "Different Series - Shared Subtitle",
        identifiers=(("isbn", "shared-id"),),
    )
    candidate = _release(
        "/library/Other Series - Shared Subtitle/Other Series - Shared Subtitle v01.cbz",
        "Other Series - Shared Subtitle",
        identifiers=(("isbn", "shared-id"),),
    )
    library = LibrarySnapshot(
        (candidate,),
        (LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )
    policy = _matching_policy(image=True, similarity=1.0)

    decision = match_series(
        incoming,
        library,
        policy,
        cover_similarity=lambda _incoming, _candidate: 0.95,
    )

    assert decision.status == "matched"
    assert decision.reason == "cover_similarity_match"
    assert decision.selected is not None
    assert decision.selected.evidence == "cover_similarity"


def test_name_score_cache_is_fingerprint_and_policy_scoped_without_pruning() -> None:
    incoming = _release(
        "/downloads/Series v02.cbz", "Series", digest=_digest("incoming")
    )
    near = _release(
        "/library/Series Q/Series Q v01.cbz", "Series Q",
        digest=_digest("candidate"),
    )
    root = LibraryRoot(Path("/library"), extensions=(".cbz",))
    library = LibrarySnapshot((near,), (root,))
    strict_policy = _matching_policy(similarity=0.9)
    permissive_policy = _matching_policy(similarity=0.8)
    cache = MatchScoreCache(strict_policy)

    strict = match_series(incoming, library, strict_policy, cache=cache, now=10)
    permissive = match_series(incoming, library, permissive_policy, cache=cache, now=11)
    exact = _release(
        "/library/Series/Series v01.cbz", "Series", digest=_digest("other")
    )
    expanded = match_series(
        incoming,
        LibrarySnapshot((near, exact), (root,)),
        strict_policy,
        cache=cache,
        now=12,
    )

    assert strict.status == "no_match"
    assert permissive.status == "matched"
    assert expanded.status == "matched"
    assert expanded.selected is not None
    assert expanded.selected.series_path == Path("/library/Series")
    assert len(cache) <= 4096


def test_name_score_cache_is_bounded_to_4096_observed_candidates() -> None:
    incoming = _release(
        "/downloads/Incoming.cbz", "Incoming", digest=_digest("incoming")
    )
    releases = tuple(
        _release(
            f"/library/Series {index:04}.cbz/Book {index:04}.cbz",
            f"Unrelated {index:04}",
            digest=_digest(str(index)),
        )
        for index in range(4100)
    )
    policy = _matching_policy(similarity=0.99)
    cache = MatchScoreCache(policy)

    match_series(
        incoming,
        LibrarySnapshot(releases),
        policy,
        cache=cache,
        now=100,
    )

    assert len(cache) == 4096


def test_missing_cover_observation_does_not_fall_through_to_identifier() -> None:
    incoming = _release(
        "/downloads/Incoming Series - Shared Subtitle v02.cbz",
        "Incoming Series - Shared Subtitle",
        identifiers=(("isbn", "shared-id"),),
    )
    candidate = _release(
        "/library/Other Series - Shared Subtitle/Other Series - Shared Subtitle v01.cbz",
        "Other Series - Shared Subtitle",
        identifiers=(("isbn", "shared-id"),),
    )
    library = LibrarySnapshot(
        (candidate,),
        (LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )

    decision = match_series(
        incoming, library, _matching_policy(image=True, similarity=1.0)
    )

    assert decision.status == "missing_dependency"
    assert decision.reason == "cover_similarity_observation_unavailable"
    assert decision.selected is None


def test_common_extension_threshold_accepts_exactly_thirty_percent() -> None:
    releases = tuple(
        _release(
            f"/library/Book {index}.zip" if index < 3 else f"/library/Book {index}.epub",
            "Book",
        )
        for index in range(10)
    )

    decision = classify_root(releases, ClassificationPolicy())

    assert decision.status == "matched"
    assert decision.extension_percentages == ((".zip", 30.0), (".epub", 70.0))
    assert decision.extensions == (".zip", ".epub", ".cbz")


def test_root_library_type_restriction_uses_supplied_ordered_rules() -> None:
    rule = LibraryType(
        "light novel",
        (".epub",),
        (r"\[Publisher\]",),
        (),
        90,
    )
    policy = ClassificationPolicy(library_types=(rule,))
    root = LibraryRoot(
        Path("/library"),
        extensions=(".epub",),
        library_types=("light novel",),
    )
    allowed = _release(
        "/downloads/Title [Publisher].epub", "Title", kind="volume"
    )
    unclassified = _release("/downloads/Title.epub", "Title", kind="volume")

    assert check_root_suitability((allowed,), root, policy).status == "suitable"
    rejected = check_root_suitability((unclassified,), root, policy)
    assert rejected.status == "no_match"
    assert rejected.reason == "library_type_unclassified"


def test_series_folder_requires_ninety_percent_medium_and_kind_match() -> None:
    incoming_chapter = _release(
        "/downloads/Series c99.cbz", "Series", kind="chapter"
    )
    existing = tuple(
        _release(
            f"/library/Series/Series {index:02}.{'epub' if index == 0 else 'cbz'}",
            "Series",
            kind="volume" if index == 0 else "chapter",
        )
        for index in range(10)
    )
    library = LibrarySnapshot(existing, (LibraryRoot(Path("/library")),))
    policy = _matching_policy()

    chapter_match = match_series(incoming_chapter, library, policy)
    volume_mismatch = match_series(
        _release("/downloads/Series v99.cbz", "Series", kind="volume"),
        library,
        policy,
    )

    assert chapter_match.status == "matched"
    assert volume_mismatch.status == "no_match"
    assert volume_mismatch.reason == "no_suitable_series_folder_or_root"


def test_series_folder_match_percentage_is_policy_scoped() -> None:
    incoming = _release("/downloads/Series c99.cbz", "Series", kind="chapter")
    existing = tuple(
        _release(
            f"/library/Series/Series {index:02}.cbz",
            "Series",
            kind="volume" if index >= 8 else "chapter",
        )
        for index in range(10)
    )
    library = LibrarySnapshot(existing, (LibraryRoot(Path("/library")),))

    strict = match_series(
        incoming, library, _matching_policy(required_matching_percentage=90)
    )
    relaxed = match_series(
        incoming, library, _matching_policy(required_matching_percentage=80)
    )

    assert strict.status == "no_match"
    assert relaxed.status == "matched"


def test_alternative_cover_gate_uses_shortened_folder_names() -> None:
    incoming = _release(
        "/downloads/Series Alpha - Starry Night v02.cbz",
        "Series Alpha - Starry Night",
    )
    primary = _release(
        "/library/Series Alpha - Moonlit Path/Series Alpha - Moonlit Path v01.cbz",
        "Series Alpha - Moonlit Path",
    )
    sibling_names = (
        "Series Alpha Other One",
        "Series Alpha Other Two",
        "Series Alpha Other Three",
    )
    siblings = tuple(
        _release(
            f"/library/{name}/{name} v01.cbz",
            name,
        )
        for name in sibling_names
    )
    library = LibrarySnapshot(
        (primary, *siblings),
        (LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )
    compared: list[Path] = []

    decision = match_series(
        incoming,
        library,
        _matching_policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared.append(existing.parent) or 0.95
        ),
    )

    assert decision.status == "matched"
    assert decision.selected is not None
    assert decision.selected.series_path == primary.parent
    assert compared == [primary.parent]


def test_alternative_cover_gate_uses_sibling_directory_word_counts() -> None:
    incoming = _release("/downloads/Tales of Azure v02.cbz", "Tales of Azure")
    candidate = _release(
        "/library/Chronicles of Azure/Chronicles of Azure v01.cbz",
        "Chronicles of Azure",
    )
    library = LibrarySnapshot(
        (candidate,),
        (LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )

    decision = match_series(
        incoming,
        library,
        _matching_policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, _existing: 0.95,
    )

    assert decision.status == "matched"
    assert decision.reason == "cover_similarity_match"


def test_alternative_cover_gate_skips_unrelated_folder_observations() -> None:
    incoming = _release("/downloads/Incoming Story v02.cbz", "Incoming Story")
    candidate = _release(
        "/library/Unrelated Novel/Unrelated Novel v01.cbz", "Unrelated Novel"
    )
    library = LibrarySnapshot(
        (candidate,),
        (LibraryRoot(Path("/library"), extensions=(".cbz",)),),
    )
    compared: list[ReleaseSnapshot] = []

    decision = match_series(
        incoming,
        library,
        _matching_policy(image=True, similarity=1.0),
        cover_similarity=lambda _incoming, existing: (
            compared.append(existing) or 0.99
        ),
    )

    assert decision.status == "no_match"
    assert compared == []


def test_higher_ranked_filename_wins_even_when_archive_bytes_are_identical() -> None:
    digest = _digest("same archive")
    incoming = _release("/download/Series v01 (Digital).cbz", "Series", digest=digest)
    existing = _release("/library/Series/Series v01.cbz", "Series", digest=digest)
    decision = decide_upgrade(incoming, existing, RankingPolicy((RankRule("Digital", 1),)))
    assert decision.status == "upgrade"
    assert decision.incoming.total_score > decision.existing.total_score


def test_folder_rename_floor_preserves_bracketed_title_content() -> None:
    assert not folder_name_is_rename_candidate(
        "A (BBBBBBBBBBBBBBBBBBBB)", "A (CCCCCCCCCCCCCCCCCCCC)", ClassificationPolicy(),
    )

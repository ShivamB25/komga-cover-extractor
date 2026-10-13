from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from komga_cover_extractor.covers.series_policy import (
    ArtworkImageObservation,
    BookArtworkObservation,
    SeriesArtworkObservation,
    SeriesCoverPolicy,
    VolumeSeriesArtworkObservation,
    check_book_artwork_collisions,
    owned_artwork_for_release,
    plan_series_artwork,
)
from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseNumber,
    ReleaseSnapshot,
)

ROOT = Path("/owned/library/Series")


def _release(name: str, number: ReleaseNumber, *, directory: Path = ROOT, kind: str = "volume", key: str = "series") -> ReleaseSnapshot:
    return ReleaseSnapshot(FileObservation(directory / name, root_role="library"), number, kind, key.title(), key)


def _art(book: ReleaseSnapshot, label: str, *, suffix: str = ".jpg") -> BookArtworkObservation:
    image = ArtworkImageObservation(book.path.with_suffix(suffix), sha256(label.encode()).hexdigest(), suffix, "image/webp" if suffix == ".webp" else "image/jpeg", owner_book=book.path)
    return BookArtworkObservation(book.path, (image,))


def _observation(*books: ReleaseSnapshot, existing: tuple[ArtworkImageObservation, ...] = ()) -> SeriesArtworkObservation:
    return SeriesArtworkObservation(ROOT, tuple(_art(book, book.name) for book in books), existing)


def test_first_and_latest_choose_exact_associated_book_images() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    latest = _release("Series v02.cbz", ReleaseNumber.scalar("2"))
    observation = _observation(first, latest)
    old = plan_series_artwork((latest, first), policy=SeriesCoverPolicy(observation=observation))
    new = plan_series_artwork((first, latest), policy=SeriesCoverPolicy(mode="latest", observation=observation))
    assert (old.status, old.selected_release.path, old.associations[0].source_image.path, old.associations[0].destination) == ("copy", first.path, ROOT / "Series v01.jpg", ROOT / "cover.jpg")
    assert (new.status, new.selected_release.path, new.associations[0].source_image.path) == ("copy", latest.path, ROOT / "Series v02.jpg")


def test_parts_do_not_alias_decimal_volumes() -> None:
    part1 = _release("Series v01 part1.cbz", ReleaseNumber.scalar("1", part=1))
    part12 = _release("Series v01 part12.cbz", ReleaseNumber.scalar("1", part=12))
    fractional = _release("Series v01.1.cbz", ReleaseNumber.scalar("1.1"))
    observation = _observation(part1, part12)
    first = plan_series_artwork((part12, part1), policy=SeriesCoverPolicy(observation=observation))
    latest = plan_series_artwork((part1, part12), policy=SeriesCoverPolicy(mode="latest", observation=observation))
    assert first.selected_release.path == part1.path
    assert latest.selected_release.path == part12.path
    decimal_latest = plan_series_artwork((part12, fractional), policy=SeriesCoverPolicy(mode="latest", observation=_observation(part12, fractional)))
    assert decimal_latest.selected_release.path == fractional.path


def test_ranges_and_sets_keep_actual_membership_for_first_and_latest() -> None:
    collection = _release("Series v01-03.cbz", ReleaseNumber.range("1", "3"))
    second = _release("Series v02.cbz", ReleaseNumber.scalar("2"))
    observation = _observation(collection, second)
    first = plan_series_artwork((second, collection), policy=SeriesCoverPolicy(observation=observation))
    latest = plan_series_artwork((collection, second), policy=SeriesCoverPolicy(mode="latest", observation=observation))
    assert first.selected_release.path == collection.path
    assert latest.selected_release.path == collection.path
    explicit = _release("Series v00-02-03.cbz", ReleaseNumber.explicit_set(("0", "2", "3")))
    no_one = plan_series_artwork((explicit,), policy=SeriesCoverPolicy(observation=_observation(explicit)))
    assert (no_one.status, no_one.reason) == ("no_match", "volume_one_required")


def test_multiple_distinct_volume_ones_are_ambiguous_regardless_order() -> None:
    first = _release("Series v01 [A].cbz", ReleaseNumber.scalar("1"))
    other = _release("Series v01 [B].cbz", ReleaseNumber.scalar("1"))
    observation = _observation(first, other)
    for books in ((first, other), (other, first)):
        result = plan_series_artwork(books, policy=SeriesCoverPolicy(observation=observation))
        assert (result.status, result.reason) == ("ambiguous", "multiple_qualifying_releases")
        assert set(result.candidates) == {first.path, other.path}
        assert result.associations == ()


def test_latest_equal_highest_endpoints_require_review() -> None:
    collection = _release("Series v01-03.cbz", ReleaseNumber.range("1", "3"))
    scalar = _release("Series v03.cbz", ReleaseNumber.scalar("3"))
    result = plan_series_artwork((collection, scalar), policy=SeriesCoverPolicy(mode="latest", observation=_observation(collection, scalar)))
    assert (result.status, result.reason) == ("ambiguous", "multiple_qualifying_releases")


def test_folder_subfolder_mixed_series_and_configured_root_restrictions() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    observation = _observation(first)
    subfolder = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=replace(observation, subdirectories=(ROOT / "extras",))))
    assert (subfolder.status, subfolder.reason) == ("no_match", "series_contains_subfolders")
    download = replace(first, observation=replace(first.observation, root_role="download"))
    configured = plan_series_artwork((download,), policy=SeriesCoverPolicy(observation=replace(observation, at_configured_root=True)))
    assert (configured.status, configured.reason) == ("no_match", "configured_root_artwork_disabled")
    library = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=replace(observation, at_configured_root=True)))
    assert (library.status, library.associations[0].destination) == ("copy", ROOT / "cover.jpg")
    other = _release("Other v02.cbz", ReleaseNumber.scalar("2"), key="other")
    mixed = plan_series_artwork((first, other), policy=SeriesCoverPolicy(observation=_observation(first, other)))
    assert (mixed.status, mixed.reason) == ("ambiguous", "mixed_series_directory")


def test_same_mtime_different_digest_updates_only_recorded_generated_art() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    source = replace(_art(first, "new").images[0], mtime_ns=100)
    old = ArtworkImageObservation(ROOT / "cover.jpg", sha256(b"old").hexdigest(), ".jpg", "image/jpeg", generated=True, owner_book=first.path, mtime_ns=100)
    observation = SeriesArtworkObservation(ROOT, (BookArtworkObservation(first.path, (source,)),), (old,))
    result = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=observation))
    assert (result.status, result.associations[0].source_image.sha256, result.associations[0].retained_existing) == ("update", sha256(b"new").hexdigest(), (old,))
    custom = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=replace(observation, existing_artwork=(replace(old, generated=False),))))
    assert (custom.status, custom.candidates, custom.associations) == ("ambiguous", (ROOT / "cover.jpg",), ())


def test_equal_digest_different_mtime_is_kept_without_rewrite() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    source = _art(first, "same").images[0]
    existing = ArtworkImageObservation(ROOT / "cover.jpg", source.sha256, ".jpg", "image/jpeg", mtime_ns=1)
    result = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=SeriesArtworkObservation(ROOT, (BookArtworkObservation(first.path, (replace(source, mtime_ns=999),)),), (existing,))))
    assert (result.status, result.associations[0].action, result.associations[0].destination) == ("keep", "keep", ROOT / "cover.jpg")


def test_cross_extension_update_retains_old_art_until_commit() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    source = _art(first, "new-webp", suffix=".webp").images[0]
    old = ArtworkImageObservation(ROOT / "cover.jpg", sha256(b"old-jpeg").hexdigest(), ".jpg", "image/jpeg", generated=True, owner_book=first.path)
    observation = SeriesArtworkObservation(ROOT, (BookArtworkObservation(first.path, (source,)),), (old,))
    result = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=observation))
    assert (result.status, result.associations[0].destination, result.associations[0].retained_existing, result.associations[0].retire_after_commit) == ("update", ROOT / "cover.webp", (old,), (ROOT / "cover.jpg",))


def test_custom_poster_blocks_group_but_is_never_retired() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    poster = ArtworkImageObservation(ROOT / "poster.jpg", sha256(b"custom poster").hexdigest(), ".jpg", "image/jpeg")
    result = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=_observation(first, existing=(poster,))))
    assert (result.status, result.reason, result.candidates, result.associations) == ("ambiguous", "custom_series_artwork", (ROOT / "poster.jpg",), ())
    assert owned_artwork_for_release(first, observation=_observation(first, existing=(poster,))) == _art(first, first.name).images


def test_recorded_poster_role_updates_but_missing_poster_is_not_invented() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    poster = ArtworkImageObservation(ROOT / "poster.jpg", sha256(b"old").hexdigest(), ".jpg", "image/jpeg", generated=True, owner_book=first.path)
    result = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=_observation(first, existing=(poster,))))
    assert tuple((association.role, association.action) for association in result.associations) == (("cover", "copy"), ("poster", "update"))
    missing = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=_observation(first)))
    assert tuple(association.role for association in missing.associations) == ("cover",)


def test_two_archives_sharing_stem_conflict_before_sidecar_plan() -> None:
    comic = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    epub = _release("Series v01.epub", ReleaseNumber.scalar("1"))
    result = plan_series_artwork((comic, epub), policy=SeriesCoverPolicy(observation=_observation(comic, epub)))
    assert (result.status, result.reason, set(result.candidates), result.associations) == ("conflict", "shared_book_sidecar_stem", {comic.path, epub.path}, ())


def test_one_shot_does_not_arbitrarily_supply_volume_one_art() -> None:
    first = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    art = replace(_art(first, "one shot"), is_one_shot=True)
    result = plan_series_artwork((first,), policy=SeriesCoverPolicy(observation=SeriesArtworkObservation(ROOT, (art,))))
    assert (result.status, result.reason) == ("no_match", "volume_one_required")


def test_chapter_copy_requires_unique_explicit_volume_match_and_volume_one() -> None:
    chapter_root = Path("/owned/chapters/Series")
    chapter = _release("Series c001.cbz", ReleaseNumber.scalar("1"), directory=chapter_root, kind="chapter")
    volume = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    matched = VolumeSeriesArtworkObservation(ROOT, (volume,), (_art(volume, "volume cover"),), "mapped")
    observation = SeriesArtworkObservation(chapter_root, matched_volume_series=(matched,))
    policy = SeriesCoverPolicy(mode="latest", copy_existing_volume_covers_toggle=True, observation=observation)
    result = plan_series_artwork((chapter,), policy=policy)
    assert (result.status, result.selected_release.path, result.associations[0].destination) == ("copy", volume.path, chapter_root / "cover.jpg")
    unmatched = plan_series_artwork((chapter,), policy=replace(policy, observation=replace(observation, matched_volume_series=())))
    assert (unmatched.status, unmatched.reason) == ("no_match", "volume_series_match_required")
    ambiguous = plan_series_artwork((chapter,), policy=replace(policy, observation=replace(observation, matched_volume_series=(matched, matched))))
    assert (ambiguous.status, ambiguous.reason) == ("ambiguous", "multiple_volume_series_matches")
    second = _release("Series v02.cbz", ReleaseNumber.scalar("2"))
    no_one = replace(matched, releases=(second,), book_artwork=(_art(second, "second cover"),))
    refused = plan_series_artwork((chapter,), policy=replace(policy, observation=replace(observation, matched_volume_series=(no_one,))))
    assert (refused.status, refused.reason) == ("no_match", "volume_one_required")


def test_local_chapter_art_uses_chapter_one_not_latest_or_known_zero() -> None:
    first = _release("Series c001.cbz", ReleaseNumber.scalar("1"), kind="chapter")
    zero = _release("Series c000.cbz", ReleaseNumber.scalar("0"), kind="chapter")
    latest = _release("Series c020.cbz", ReleaseNumber.scalar("20"), kind="chapter")
    result = plan_series_artwork((zero, latest, first), policy=SeriesCoverPolicy(mode="latest", chapter_support_toggle=True, extract_chapter_covers=True, observation=_observation(zero, latest, first)))
    assert (result.status, result.selected_release.path) == ("copy", first.path)


def test_book_sidecar_preflight_detects_stems_before_any_series_decision() -> None:
    comic = _release("Series v01.cbz", ReleaseNumber.scalar("1"))
    epub = _release("Series v01.epub", ReleaseNumber.scalar("1"))
    other = _release("Series v02.cbz", ReleaseNumber.scalar("2"))
    collision = check_book_artwork_collisions((comic, epub, other))
    unique = check_book_artwork_collisions((comic, other))
    assert (collision.status, collision.reason, set(collision.candidates)) == ("conflict", "shared_book_sidecar_stem", {comic.path, epub.path})
    assert (unique.status, unique.reason) == ("keep", "unique_book_sidecar_stems")

"""Series and book artwork decisions over caller-owned observations."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from ..domain.operations import ArtifactSpec
from ..domain.releases import ReleaseSnapshot


@dataclass(frozen=True, slots=True)
class ArtworkImageObservation:
    path: Path
    sha256: str
    suffix: str
    media_type: str
    artifact: ArtifactSpec | None = None
    generated: bool = False
    owner_book: Path | None = None
    mtime_ns: int | None = None

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("artwork requires an observed SHA-256 digest")
        if self.suffix not in {".jpg", ".png", ".webp"}:
            raise ValueError("artwork suffix must identify a supported raster")
        if self.artifact is not None and self.artifact.digest != self.sha256:
            raise ValueError("artwork bytes and observed digest disagree")


@dataclass(frozen=True, slots=True)
class BookArtworkObservation:
    book: Path
    images: tuple[ArtworkImageObservation, ...]
    is_one_shot: bool = False


@dataclass(frozen=True, slots=True)
class VolumeSeriesArtworkObservation:
    directory: Path
    releases: tuple[ReleaseSnapshot, ...]
    book_artwork: tuple[BookArtworkObservation, ...]
    match_evidence: Literal["matched", "mapped"]
    contains_subfolders: bool = False

    def __post_init__(self) -> None:
        if self.match_evidence not in {"matched", "mapped"}:
            raise ValueError("chapter copies require explicit matched or mapped evidence")


@dataclass(frozen=True, slots=True)
class SeriesArtworkObservation:
    directory: Path
    book_artwork: tuple[BookArtworkObservation, ...] = ()
    existing_artwork: tuple[ArtworkImageObservation, ...] = ()
    subdirectories: tuple[Path, ...] = ()
    at_configured_root: bool = False
    matched_volume_series: tuple[VolumeSeriesArtworkObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesCoverPolicy:
    mode: Literal["first", "latest"] = "first"
    roles: tuple[str, ...] = ("cover", "poster")
    copy_existing_volume_covers_toggle: bool = False
    chapter_support_toggle: bool = False
    extract_chapter_covers: bool = False
    observation: SeriesArtworkObservation | None = None
    create_roles: tuple[str, ...] = ("cover",)

    def __post_init__(self) -> None:
        if self.mode not in {"first", "latest"}:
            raise ValueError("series artwork mode must be first or latest")
        roles = tuple(self.roles)
        create_roles = tuple(self.create_roles)
        if len(set(roles)) != len(roles) or any(
            not role or role != role.strip() or role.startswith(".")
            or any(character in role for character in "/\\:\x00")
            for role in roles
        ):
            raise ValueError("series artwork roles must be distinct safe basenames")
        if len(set(create_roles)) != len(create_roles) or any(role not in roles for role in create_roles):
            raise ValueError("created artwork roles must be recognized roles")
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "create_roles", create_roles)


@dataclass(frozen=True, slots=True)
class ArtworkAssociation:
    role: str
    source_book: Path
    source_image: ArtworkImageObservation
    destination: Path
    action: Literal["keep", "update", "copy"]
    retained_existing: tuple[ArtworkImageObservation, ...] = ()
    retire_after_commit: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtworkDecision:
    status: Literal["keep", "update", "copy", "ambiguous", "conflict", "no_match", "invalid_input"]
    reason: str
    selected_release: ReleaseSnapshot | None = None
    associations: tuple[ArtworkAssociation, ...] = ()
    candidates: tuple[Path, ...] = ()


def _shared_stem(releases: Sequence[ReleaseSnapshot]) -> tuple[Path, ...]:
    stems: dict[tuple[Path, str], list[Path]] = {}
    for release in releases:
        paths = stems.setdefault((release.parent, release.stem), [])
        if release.path not in paths:
            paths.append(release.path)
    return tuple(path for paths in stems.values() if len(paths) > 1 for path in paths)


def _select_release(
    releases: Sequence[ReleaseSnapshot], *, mode: Literal["first", "latest"],
    book_artwork: Sequence[BookArtworkObservation],
) -> tuple[ReleaseSnapshot | None, tuple[Path, ...]]:
    one_shots = {book.book for book in book_artwork if book.is_one_shot}
    candidates = [release for release in releases if release.path not in one_shots and release.number.kind != "missing"]
    if mode == "first":
        candidates = [release for release in candidates if release.number.contains(Decimal(1))]
        if not candidates:
            return None, ()
        first_part = min(release.number.part or 0 for release in candidates)
        candidates = [release for release in candidates if (release.number.part or 0) == first_part]
    else:
        if not candidates:
            return None, ()
        highest = max(max(release.number.values) for release in candidates)
        candidates = [release for release in candidates if max(release.number.values) == highest]
        latest_part = max(release.number.part or 0 for release in candidates)
        candidates = [release for release in candidates if (release.number.part or 0) == latest_part]
    unique = {release.path: release for release in candidates}
    if len(unique) != 1:
        return None, tuple(sorted(unique))
    return next(iter(unique.values())), ()


def _source_image(
    release: ReleaseSnapshot, observations: Sequence[BookArtworkObservation],
) -> tuple[ArtworkImageObservation | None, tuple[Path, ...]]:
    images = [image for book in observations if book.book == release.path for image in book.images]
    if not images:
        return None, ()
    if any(image.path.parent != release.parent or image.path.stem != release.stem or (image.owner_book is not None and image.owner_book != release.path) for image in images):
        return None, tuple(image.path for image in images)
    if len({image.sha256 for image in images}) != 1:
        return None, tuple(image.path for image in images)
    by_path = {image.path: image for image in images}
    return by_path[sorted(by_path)[0]], ()


def _folder_eligibility(
    releases: Sequence[ReleaseSnapshot], observation: SeriesArtworkObservation,
) -> ArtworkDecision | None:
    if not releases:
        return ArtworkDecision("no_match", "empty_series")
    if any(release.parent != observation.directory for release in releases):
        return ArtworkDecision("invalid_input", "artwork_directory_mismatch")
    collision = _shared_stem(releases)
    if collision:
        return ArtworkDecision("conflict", "shared_book_sidecar_stem", candidates=collision)
    if observation.subdirectories:
        return ArtworkDecision("no_match", "series_contains_subfolders")
    at_root = observation.at_configured_root or any(release.observation.at_configured_root for release in releases)
    if at_root and any(release.observation.root_role in {"download", "both"} for release in releases):
        return ArtworkDecision("no_match", "configured_root_artwork_disabled")
    keys = {release.canonical_series_key for release in releases}
    if len(keys) != 1 or "" in keys:
        return ArtworkDecision("ambiguous", "mixed_series_directory", candidates=tuple(release.path for release in releases))
    if len({release.kind for release in releases}) != 1 or releases[0].kind not in {"volume", "chapter"}:
        return ArtworkDecision("ambiguous", "mixed_release_kinds", candidates=tuple(release.path for release in releases))
    return None


def plan_series_artwork(
    releases: Sequence[ReleaseSnapshot], *, policy: SeriesCoverPolicy,
) -> ArtworkDecision:
    observation = policy.observation
    if observation is None:
        return ArtworkDecision("invalid_input", "series_artwork_observation_required")
    ineligible = _folder_eligibility(releases, observation)
    if ineligible is not None:
        return ineligible
    chapter_directory = releases[0].kind == "chapter"
    source_releases: Sequence[ReleaseSnapshot] = releases
    source_artwork: Sequence[BookArtworkObservation] = observation.book_artwork
    mode: Literal["first", "latest"] = "first" if chapter_directory else policy.mode
    if chapter_directory and policy.copy_existing_volume_covers_toggle:
        matched = observation.matched_volume_series
        if not matched:
            return ArtworkDecision("no_match", "volume_series_match_required")
        if len(matched) != 1:
            return ArtworkDecision("ambiguous", "multiple_volume_series_matches", candidates=tuple(item.directory for item in matched))
        volume = matched[0]
        if volume.contains_subfolders or not volume.releases or any(release.kind != "volume" or release.parent != volume.directory for release in volume.releases):
            return ArtworkDecision("no_match", "ineligible_volume_series")
        if len({release.canonical_series_key for release in volume.releases}) != 1:
            return ArtworkDecision("ambiguous", "mixed_volume_series", candidates=tuple(release.path for release in volume.releases))
        collision = _shared_stem(volume.releases)
        if collision:
            return ArtworkDecision("conflict", "shared_book_sidecar_stem", candidates=collision)
        source_releases, source_artwork = volume.releases, volume.book_artwork
    elif chapter_directory and not (policy.chapter_support_toggle and policy.extract_chapter_covers):
        return ArtworkDecision("no_match", "chapter_cover_disabled")
    selected, ambiguous = _select_release(source_releases, mode=mode, book_artwork=source_artwork)
    if ambiguous:
        return ArtworkDecision("ambiguous", "multiple_qualifying_releases", candidates=ambiguous)
    if selected is None:
        return ArtworkDecision("no_match", "volume_one_required" if mode == "first" else "known_release_required")
    source, ambiguous_images = _source_image(selected, source_artwork)
    if ambiguous_images:
        return ArtworkDecision("ambiguous", "ambiguous_book_artwork", selected, candidates=ambiguous_images)
    if source is None:
        return ArtworkDecision("no_match", "selected_book_artwork_absent", selected)
    associations: list[ArtworkAssociation] = []
    for role in policy.roles:
        existing = tuple(image for image in observation.existing_artwork if image.path.parent == observation.directory and image.path.stem == role)
        if not existing and role not in policy.create_roles:
            continue
        destination = observation.directory / (role + source.suffix)
        if existing and all(image.sha256 == source.sha256 for image in existing):
            associations.append(ArtworkAssociation(role, selected.path, source, existing[0].path, "keep", existing))
            continue
        custom = tuple(image for image in existing if not image.generated and image.sha256 != source.sha256)
        if custom:
            return ArtworkDecision("ambiguous", "custom_series_artwork", selected, candidates=tuple(image.path for image in custom))
        action: Literal["keep", "update", "copy"] = "update" if existing else "copy"
        retire = tuple(image.path for image in existing if image.path != destination and image.generated)
        associations.append(ArtworkAssociation(role, selected.path, source, destination, action, existing, retire))
    if not associations:
        return ArtworkDecision("keep", "no_requested_artwork_roles", selected)
    status: Literal["keep", "update", "copy"] = "update" if any(item.action == "update" for item in associations) else "copy" if any(item.action == "copy" for item in associations) else "keep"
    return ArtworkDecision(status, "selected_series_artwork" if status != "keep" else "identical_artwork", selected, tuple(associations))


def owned_artwork_for_release(
    release: ReleaseSnapshot, *, observation: SeriesArtworkObservation,
) -> tuple[ArtworkImageObservation, ...]:
    associated = tuple(
        image for book in observation.book_artwork if book.book == release.path
        for image in book.images
        if image.path.parent == release.parent and image.path.stem == release.stem
        and image.owner_book in {None, release.path}
    )
    owned_series = tuple(
        image for image in observation.existing_artwork
        if image.generated and image.owner_book == release.path
        and image.path.parent == release.parent
    )
    return associated + owned_series


def check_book_artwork_collisions(releases: Sequence[ReleaseSnapshot]) -> ArtworkDecision:
    collisions = _shared_stem(releases)
    if collisions:
        return ArtworkDecision("conflict", "shared_book_sidecar_stem", candidates=collisions)
    return ArtworkDecision("keep", "unique_book_sidecar_stems")

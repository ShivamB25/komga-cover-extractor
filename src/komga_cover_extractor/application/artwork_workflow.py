"""Observe book and series art, then publish recoverable per-book groups."""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import Literal, cast
from uuid import uuid4

from ..covers.selection import IMAGE_SUFFIXES, CoverPolicy
from ..covers.series_policy import (
    ArtworkDecision,
    ArtworkImageObservation,
    BookArtworkObservation,
    SeriesArtworkObservation,
    SeriesCoverPolicy,
    VolumeSeriesArtworkObservation,
    check_book_artwork_collisions,
    plan_series_artwork,
)
from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.libraries import LibraryRoot, LibrarySnapshot, normalize_library_roots
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationPlan,
    PlanReport,
    RootPermission,
)
from ..domain.releases import FileObservation, ReleaseNumber, ReleaseSnapshot
from ..infrastructure.filesystem import (
    BOOK_SUFFIXES,
    FilesystemError,
    checked_path,
    fingerprint,
)
from ..infrastructure.images import FileOutputPolicy
from ..infrastructure.state import CommittedArtifact, StateStore, StateStoreError
from ..infrastructure.transactions import TransactionExecutor
from ..infrastructure.workers import WorkerError, run_worker
from ..matching.classification import ClassificationPolicy, check_root_suitability
from ..matching.series import MatchingPolicy, match_series
from ..parsing.filenames import ParsingPolicy, parse_release
from .extract_covers import extract_cover

_IMAGE_DETAILS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
_ERROR_STATUSES = frozenset({"invalid_input", "unsupported", "missing_dependency", "cancelled", "failed", "permission_denied", "review_required", "conflict", "stale_plan", "ambiguous"})


class _ArtworkError(Exception):
    def __init__(self, status: Status, reason: str, path: Path, candidates: Sequence[Path] = ()):
        self.decision = Decision(status, reason, path, tuple(str(candidate) for candidate in candidates))
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _Folder:
    directory: Path
    root: LibraryRoot
    entries: tuple[Path, ...]
    subdirectories: tuple[Path, ...]
    releases: tuple[ReleaseSnapshot, ...]
    errors: tuple[Decision, ...] = ()


@dataclass(frozen=True, slots=True)
class _ObservedImage:
    image: ArtworkImageObservation
    fingerprint: FileFingerprint
    owner_source: FileFingerprint | None = None

    def current_for(self, release: ReleaseSnapshot, generation: str, store: StateStore) -> bool:
        source = release.observation.fingerprint
        return (source is not None and self.image.generated and self.image.owner_book == release.path
                and self.owner_source == source
                and self.image.path.suffix.lower() in {self.image.suffix, ".jpeg"}
                and store.artifact_assessment_matches(self.fingerprint, source, generation))


@dataclass(frozen=True, slots=True)
class _SourceImage:
    release: ReleaseSnapshot
    image: ArtworkImageObservation | None
    diagnostics: tuple[Decision, ...] = ()


@dataclass(frozen=True, slots=True)
class _SeriesSource:
    selection: ArtworkDecision
    folder: _Folder | None = None


def _decode_fingerprint(value: object) -> FileFingerprint:
    if not isinstance(value, Mapping) or set(value) != {"root", "relative_path", "device", "inode", "size", "mtime_ns", "sha256"}:
        raise ValueError("invalid fingerprint fields")
    if any(not isinstance(value[key], str) for key in ("root", "relative_path", "sha256")) or any(type(value[key]) is not int for key in ("device", "inode", "size", "mtime_ns")):
        raise ValueError("invalid fingerprint values")
    return FileFingerprint(Path(cast(str, value["root"])), cast(str, value["relative_path"]),
                           cast(int, value["device"]), cast(int, value["inode"]),
                           cast(int, value["size"]), cast(int, value["mtime_ns"]), cast(str, value["sha256"]))


def artwork_image_job(request: dict[str, object]) -> dict[str, object]:
    """The inspect worker owns decoding existing, potentially hostile sidecars."""
    if set(request) != {"artwork_image", "fingerprint", "max_bytes", "max_pixels"} or request["artwork_image"] is not True:
        raise WorkerError("invalid_artwork_image_request")
    max_bytes, max_pixels = request["max_bytes"], request["max_pixels"]
    if type(max_bytes) is not int or max_bytes <= 0 or type(max_pixels) is not int or max_pixels <= 0:
        raise WorkerError("invalid_artwork_image_request")
    try:
        source = _decode_fingerprint(request["fingerprint"])
    except (TypeError, ValueError):
        raise WorkerError("invalid_artwork_image_request") from None
    if source.size > max_bytes:
        raise WorkerError("image_byte_limit")
    try:
        path = checked_path(source.path, source.root)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise WorkerError("artwork_not_regular_file", "conflict")
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (source.device, source.inode, source.size, source.mtime_ns):
                raise WorkerError("artwork_fingerprint_changed", "stale_plan")
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise WorkerError("image_byte_limit")
            if len(data) != source.size or sha256(data).hexdigest() != source.sha256:
                raise WorkerError("artwork_fingerprint_changed", "stale_plan")
            after = os.fstat(stream.fileno())
        from ..infrastructure.images import observe_image
        observed = observe_image(data, max_pixels=max_pixels)
        current = checked_path(path, source.root).lstat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if any((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != identity for info in (after, current)):
            raise WorkerError("artwork_fingerprint_changed", "stale_plan")
        return {"status": "valid", "image_format": observed.format, "media_type": observed.media_type,
                "suffix": observed.suffix, "sha256": source.sha256}
    except FilesystemError as error:
        raise WorkerError(error.reason, "conflict" if error.reason == "symlink_traversal" else "invalid_input") from None


def _root_for(path: Path, roots: tuple[LibraryRoot, ...]) -> LibraryRoot:
    matches = [root for root in roots if root.contains(path)]
    if len(matches) != 1:
        raise _ArtworkError("permission_denied", "outside_selected_roots", path)
    return matches[0]


def _role(root: LibraryRoot) -> str:
    if {"library", "download"} <= set(root.roles):
        return "both"
    return "download" if "download" in root.roles else "library" if "library" in root.roles else "unknown"


def _os_decision(error: OSError, path: Path) -> Decision:
    if isinstance(error, FilesystemError):
        status: Status = "conflict" if error.reason in {"symlink_traversal", "not_regular_file"} else "stale_plan" if error.reason == "source_changed" else "invalid_input"
        return Decision(status, error.reason, error.path)
    if isinstance(error, PermissionError):
        return Decision("permission_denied", "artwork_observation_permission_denied", path)
    if isinstance(error, FileNotFoundError):
        return Decision("stale_plan", "artwork_input_disappeared", path)
    return Decision("failed", "artwork_observation_failed", path)


def _observe_folder(directory: Path, roots: tuple[LibraryRoot, ...], parsing: ParsingPolicy,
                    known: Mapping[Path, ReleaseSnapshot]) -> _Folder:
    root = _root_for(directory, roots)
    directory = checked_path(directory, root.path)
    entries = tuple(sorted(directory.iterdir()))
    files: list[Path] = []
    folders: list[Path] = []
    archive_paths: list[Path] = []
    for path in entries:
        mode = path.lstat().st_mode
        if (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)) and not path.name.startswith(".") and path.suffix.lower() not in IMAGE_SUFFIXES:
            folders.append(path)
        elif stat.S_ISREG(mode):
            files.append(path)
            if not path.name.startswith(".") and path.suffix.lower() in BOOK_SUFFIXES:
                archive_paths.append(path)
    siblings = tuple(files)
    releases: list[ReleaseSnapshot] = []
    errors: list[Decision] = []
    for path in archive_paths:
        prior = known.get(path)
        observed = None
        try:
            observed = fingerprint(path, root.path)
            if prior is not None and prior.observation.fingerprint is not None and prior.observation.fingerprint != observed:
                errors.append(Decision("stale_plan", "source_fingerprint_changed", path))
        except OSError as error:
            errors.append(_os_decision(error, path))
        observation = FileObservation(path, siblings=siblings, metadata=prior.observation.metadata if prior else (),
                                      archive_pages=prior.observation.archive_pages if prior else (),
                                      root_role=_role(root), fingerprint=observed, at_configured_root=directory == root.path)
        try:
            releases.append(parse_release(observation, parsing))
        except ValueError:
            errors.append(Decision("invalid_input", "invalid_release_observation", path))
            if prior is not None:
                releases.append(prior)
    return _Folder(directory, root, entries, tuple(folders), tuple(releases), tuple(errors))


def _empty_book_observations(folder: _Folder) -> tuple[BookArtworkObservation, ...]:
    return tuple(BookArtworkObservation(release.path, (), "classification:one_shot" in release.provenance) for release in folder.releases)


def _series_selection(folder: _Folder, policy: SeriesCoverPolicy, classification: ClassificationPolicy,
                      volume: _Folder | None = None) -> ArtworkDecision:
    matched = () if volume is None else (VolumeSeriesArtworkObservation(
        volume.directory, volume.releases, _empty_book_observations(volume), "matched", bool(volume.subdirectories)),)
    observation = SeriesArtworkObservation(folder.directory, _empty_book_observations(folder),
                                            subdirectories=folder.subdirectories,
                                            at_configured_root=folder.directory == folder.root.path,
                                            matched_volume_series=matched)
    decision = plan_series_artwork(folder.releases, policy=replace(policy, observation=observation))
    if decision.selected_release is not None:
        source_folder = volume or folder
        suitability = check_root_suitability((decision.selected_release,), source_folder.root, classification,
                                             classification_files=source_folder.releases)
        if suitability.status != "suitable":
            return ArtworkDecision("no_match", suitability.reason)
    return decision


def _choose_series_source(folder: _Folder, catalog: LibrarySnapshot, roots: tuple[LibraryRoot, ...],
                          parsing: ParsingPolicy, policy: SeriesCoverPolicy, matching: MatchingPolicy,
                          known: Mapping[Path, ReleaseSnapshot]) -> _SeriesSource:
    preliminary = _series_selection(folder, policy, matching.classification)
    if preliminary.reason != "volume_series_match_required":
        return _SeriesSource(preliminary, folder if preliminary.selected_release is not None else None)
    if not catalog.available:
        raise _ArtworkError("missing_dependency", catalog.reason or "library_unavailable", folder.directory)
    # This is a name-only volume-folder probe, not a chapter upgrade decision.
    incoming = replace(folder.releases[0], kind="volume", number=ReleaseNumber.missing(), identifiers=())
    volumes = tuple(release for release in catalog.releases if release.kind == "volume")
    matched = match_series(incoming, LibrarySnapshot(volumes, roots), matching)
    if matched.status != "matched" or matched.selected is None:
        status: Literal["ambiguous", "no_match"] = "ambiguous" if matched.status == "ambiguous" else "no_match"
        return _SeriesSource(ArtworkDecision(status, "multiple_volume_series_matches" if status == "ambiguous" else "volume_series_match_required",
                                             candidates=tuple(candidate.series_path for candidate in matched.candidates)))
    volume = _observe_folder(matched.selected.series_path, roots, parsing, known)
    return _SeriesSource(_series_selection(folder, policy, matching.classification, volume), volume)


def _record_plan(record: CommittedArtifact) -> dict[str, object]:
    try:
        value: object = json.loads(record.plan_json)
    except (ValueError, TypeError):
        raise StateStoreError("invalid artwork provenance plan") from None
    if not isinstance(value, dict) or not isinstance(value.get("policy_version"), str) or not isinstance(value.get("operations"), list):
        raise StateStoreError("invalid artwork provenance plan")
    return cast(dict[str, object], value)


def _record_member(record: CommittedArtifact, plan: Mapping[str, object]) -> Mapping[str, object]:
    operations = plan["operations"]
    assert isinstance(operations, list)
    matches = [operation for operation in operations if isinstance(operation, dict)
               and operation.get("destination") == str(record.published.path) and operation.get("kind") == record.kind]
    if len(matches) != 1 or matches[0].get("source") != (record.source.as_dict() if record.source else None):
        raise StateStoreError("artwork provenance does not match committed member")
    return cast(Mapping[str, object], matches[0])


def _book_derived_from(current: FileFingerprint, old: FileFingerprint, store: StateStore) -> bool:
    before_ns: int | None = None
    for _ in range(32):
        record = store.committed_artifact(current.path, before_ns=before_ns)
        if record is None or record.source is None or record.published != current:
            return False
        operation = _record_member(record, _record_plan(record))
        if record.kind == "publish":
            artifact = operation.get("artifact")
            if not isinstance(artifact, Mapping) or artifact.get("media_type") not in {"application/zip", "application/epub+zip"} or (
                artifact.get("sha256"), artifact.get("size")
            ) != (current.sha256, current.size):
                return False
        elif record.kind != "move":
            return False
        if record.source == old:
            return True
        current, before_ns = record.source, record.created_ns
    return False


def _image_provenance(source: FileFingerprint, books: Mapping[Path, ReleaseSnapshot], store: StateStore) -> tuple[Path | None, FileFingerprint | None]:
    record = store.committed_artifact(source.path)
    if record is None or record.published.sha256 != source.sha256 or record.published.size != source.size:
        return None, None
    for _ in range(32):
        plan = _record_plan(record)
        operation = _record_member(record, plan)
        owner = record.source
        if owner is None:
            return None, None
        if record.kind == "publish":
            artifact = operation.get("artifact")
            if not isinstance(artifact, Mapping) or artifact.get("sha256") != source.sha256 or owner.path.suffix.lower() not in BOOK_SUFFIXES:
                return None, None
            if owner.path in books:
                return owner.path, owner
            derived = [release for release in books.values() if release.observation.fingerprint is not None
                     and _book_derived_from(release.observation.fingerprint, owner, store)]
            if len(derived) == 1:
                current = derived[0].observation.fingerprint
                assert current is not None
                return derived[0].path, current
            return None, None
        previous = store.committed_artifact(owner.path, before_ns=record.created_ns)
        if previous is None or previous.published.sha256 != owner.sha256 or previous.published.size != owner.size:
            return None, None
        record = previous
    raise StateStoreError("artwork provenance chain exceeds limit")


def _observe_image(path: Path, root: LibraryRoot, books: Mapping[Path, ReleaseSnapshot],
                   store: StateStore, policy: CoverPolicy, cancel: Event) -> _ObservedImage:
    source = fingerprint(path, root.path)
    if any(release.observation.fingerprint is not None
           and (source.device, source.inode) == (release.observation.fingerprint.device, release.observation.fingerprint.inode)
           for release in books.values()):
        raise _ArtworkError("conflict", "artwork_aliases_book", path)
    response = run_worker("inspect", {"artwork_image": True, "fingerprint": source.as_dict(),
                                  "max_bytes": policy.archive_policy.cover_member_max_bytes,
                                  "max_pixels": policy.archive_policy.image_max_pixels},
                                  timeout=policy.archive_policy.conversion_timeout_seconds,
                                  max_output_bytes=64 * 1024, cancel=cancel)
    image_format = response.get("image_format")
    if set(response) != {"status", "image_format", "media_type", "suffix", "sha256"} or not isinstance(image_format, str) or image_format not in _IMAGE_DETAILS:
        raise _ArtworkError("failed", "invalid_artwork_image_result", path)
    media_type, suffix = _IMAGE_DETAILS[image_format]
    if response["media_type"] != media_type or response["suffix"] != suffix or response["sha256"] != source.sha256:
        raise _ArtworkError("failed", "invalid_artwork_image_result", path)
    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".tbn": "JPEG", ".png": "PNG", ".webp": "WEBP"}
    if expected.get(path.suffix.lower()) != image_format:
        raise _ArtworkError("invalid_input", "artwork_suffix_mismatch", path)
    owner, owner_source = _image_provenance(source, books, store)
    image = ArtworkImageObservation(path, source.sha256, suffix, media_type,
                                    ArtifactSpec(source=source, media_type=media_type), owner is not None,
                                    owner, source.mtime_ns)
    return _ObservedImage(image, source, owner_source)


def _observe_images(paths: Sequence[Path], folder: _Folder, books: Mapping[Path, ReleaseSnapshot],
                    store: StateStore, policy: CoverPolicy, cancel: Event) -> tuple[_ObservedImage, ...]:
    observations: list[_ObservedImage] = []
    for path in paths:
        checked_path(path, folder.root.path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise _ArtworkError("conflict", "artwork_not_regular_file", path)
        observations.append(_observe_image(path, folder.root, books, store, policy, cancel))
    return tuple(observations)


def _book_images(folder: _Folder, book: Path) -> tuple[Path, ...]:
    return tuple(path for path in folder.entries if path.stem == book.stem and path.suffix.lower() in IMAGE_SUFFIXES)


def _load_source(release: ReleaseSnapshot, folder: _Folder, existing: tuple[_ObservedImage, ...],
                 parsing: ParsingPolicy, policy: CoverPolicy, store: StateStore, cancel: Event) -> _SourceImage:
    error = next((decision for decision in folder.errors if decision.path == release.path), None)
    if error is not None:
        raise _ArtworkError(error.status, error.reason, release.path)
    source = release.observation.fingerprint
    if source is None:
        raise _ArtworkError("invalid_input", "source_fingerprint_required", release.path)
    if cancel.is_set():
        raise _ArtworkError("cancelled", "shutdown_requested", release.path)
    if fingerprint(release.path, folder.root.path) != source:
        raise _ArtworkError("stale_plan", "source_fingerprint_changed", release.path)
    current = [image for image in existing if image.current_for(release, policy.generation_fingerprint, store)]
    if len(current) == 1 and all(image.image.sha256 == current[0].image.sha256 or image.image.generated for image in existing):
        return _SourceImage(release, current[0].image)
    result = extract_cover(release.path, root=folder.root.path, policy=policy, output=FileOutputPolicy(),
                           release_kind=release.kind, cancel=cancel)
    if result.status not in {"valid", "absent"}:
        raise _ArtworkError(cast(Status, result.status), result.reason, release.path)
    if fingerprint(release.path, folder.root.path) != source:
        raise _ArtworkError("stale_plan", "source_fingerprint_changed", release.path)
    observation = replace(release.observation, metadata=result.metadata.values, archive_pages=result.archive_pages)
    release = parse_release(observation, parsing)
    diagnostics = tuple(Decision("observed", reason, release.path) for reason in result.diagnostics)
    if result.status == "absent":
        return _SourceImage(release, None, diagnostics + (Decision("no_match", result.reason, release.path),))
    if result.artifact is None or result.suffix is None or result.media_type is None:
        raise _ArtworkError("failed", "invalid_cover_artifact", release.path)
    image = ArtworkImageObservation(release.path.with_suffix(result.suffix), result.artifact.digest,
                                    result.suffix, result.media_type, result.artifact, owner_book=release.path)
    identical = next((item for item in existing if item.image.sha256 == image.sha256 and item.image.suffix == image.suffix), None)
    if identical is not None:
        image = replace(image, path=identical.image.path, artifact=identical.image.artifact)
    return _SourceImage(release, image, diagnostics)


def _validate_book_artwork(source: _SourceImage, existing: tuple[_ObservedImage, ...]) -> _ObservedImage | None:
    image = source.image
    if image is None:
        return None
    custom = tuple(item.image.path for item in existing
                   if (not item.image.generated or item.image.owner_book != source.release.path)
                   and item.image.sha256 != image.sha256)
    if custom:
        raise _ArtworkError("review_required", "custom_book_artwork", source.release.path, custom)
    exact = next((item for item in existing if item.image.sha256 == image.sha256 and item.image.suffix == image.suffix), None)
    if exact is None and image.artifact is None:
        raise _ArtworkError("failed", "book_artwork_artifact_required", source.release.path)
    return exact


def _book_operations(source: _SourceImage, existing: tuple[_ObservedImage, ...]) -> tuple[list[Operation], list[FileFingerprint]]:
    exact = _validate_book_artwork(source, existing)
    image = source.image
    if image is None:
        return [], []
    destination = source.release.path.with_suffix(image.suffix)
    operations: list[Operation] = []
    if exact is None:
        before = next((item.fingerprint for item in existing if item.image.path == destination), None)
        operations.append(Operation("book-artwork", "publish", destination, source=source.release.observation.fingerprint,
                                    destination_before=before, artifact=image.artifact,
                                    collision="replace" if before else "absent", reason="book_artwork"))
    retire = [item.fingerprint for item in existing
              if item.image.generated and item.image.owner_book == source.release.path
              and item.image.sha256 != image.sha256 and item.image.path != destination]
    return operations, retire


def _series_operations(folder: _Folder, strategy: _SeriesSource, source: _SourceImage,
                       existing: tuple[_ObservedImage, ...], policy: SeriesCoverPolicy,
                       dependencies: tuple[str, ...]) -> tuple[list[Operation], list[FileFingerprint], ArtworkDecision]:
    assert strategy.folder is not None
    source_folder = replace(strategy.folder, releases=tuple(source.release if release.path == source.release.path else release for release in strategy.folder.releases))
    book_observations = tuple(BookArtworkObservation(release.path,
                              (source.image,) if source.image is not None and release.path == source.release.path else (),
                              "classification:one_shot" in release.provenance) for release in source_folder.releases)
    volume: VolumeSeriesArtworkObservation | None = None
    releases = folder.releases
    if source_folder.directory != folder.directory:
        volume = VolumeSeriesArtworkObservation(source_folder.directory, source_folder.releases, book_observations,
                                                "matched", bool(source_folder.subdirectories))
        book_observations = _empty_book_observations(folder)
    else:
        releases = source_folder.releases
    observation = SeriesArtworkObservation(folder.directory, book_observations, tuple(item.image for item in existing),
                                            folder.subdirectories, folder.directory == folder.root.path,
                                            (volume,) if volume is not None else ())
    decision = plan_series_artwork(releases, policy=replace(policy, observation=observation))
    if decision.status in {"ambiguous", "conflict", "invalid_input"}:
        status: Status = "review_required" if decision.status == "ambiguous" else "conflict" if decision.status == "conflict" else "invalid_input"
        raise _ArtworkError(status, decision.reason, folder.directory, decision.candidates)
    if decision.selected_release is not None and decision.selected_release.path != source.release.path:
        raise _ArtworkError("stale_plan", "series_artwork_selection_changed", folder.directory)
    operations: list[Operation] = []
    retire: list[FileFingerprint] = []
    for index, association in enumerate(decision.associations):
        if association.action == "keep":
            continue
        artifact = association.source_image.artifact
        if artifact is None:
            raise _ArtworkError("failed", "series_artwork_artifact_required", folder.directory)
        before = next((item.fingerprint for item in existing if item.image.path == association.destination), None)
        member_id = "series-" + association.role if association.role in {"cover", "poster"} else f"series-configured-{index}"
        operations.append(Operation(member_id, "publish", association.destination,
                                    source=source.release.observation.fingerprint, destination_before=before,
                                    artifact=artifact, depends_on=dependencies, collision="replace" if before else "absent",
                                    reason="series_artwork_" + association.role))
        retire.extend(item.fingerprint for item in existing if item.image.path in association.retire_after_commit)
    return operations, retire, decision


def _record_book_assessment(
    source: _SourceImage, root: LibraryRoot, policy: CoverPolicy, store: StateStore,
    authorization: RunAuthorization,
) -> None:
    image = source.image
    book = source.release.observation.fingerprint
    if not authorization.apply or image is None or book is None or not image.path.exists():
        return
    observed = fingerprint(image.path, root.path)
    if observed.sha256 != image.sha256:
        raise _ArtworkError("stale_plan", "artwork_changed_before_assessment", image.path)
    if not store.artifact_assessment_matches(observed, book, policy.generation_fingerprint):
        store.record_artifact_assessment(observed, book, policy.generation_fingerprint,
                                         authorization=authorization)


def _one_book(release: ReleaseSnapshot, folder: _Folder, strategy: _SeriesSource | None,
              *, roots: tuple[LibraryRoot, ...], parsing: ParsingPolicy, cover_policy: CoverPolicy,
              series_policy: SeriesCoverPolicy, state_dir: Path, policy_version: str,
              authorization: RunAuthorization, cancel: Event, classification: ClassificationPolicy) -> RunResult:
    try:
        if cancel.is_set():
            raise _ArtworkError("cancelled", "shutdown_requested", release.path)
        suitability = check_root_suitability((release,), folder.root, classification, classification_files=folder.releases)
        if suitability.status != "suitable":
            return RunResult((Decision("no_match", suitability.reason, release.path),), skipped=1)
        books = {item.path: item for item in folder.releases}
        if strategy is not None and strategy.folder is not None:
            books.update((item.path, item) for item in strategy.folder.releases)
        store = StateStore(state_dir)
        observed = _observe_images(_book_images(folder, release.path), folder, books, store, cover_policy, cancel)
        source = _load_source(release, folder, observed, parsing, cover_policy, store, cancel)
        operations, retire = _book_operations(source, observed)
        diagnostics = list(source.diagnostics)
        if strategy is not None and strategy.folder is not None and strategy.selection.selected_release is not None:
            chosen = strategy.selection.selected_release
            if chosen.path == release.path:
                series_source = source
            else:
                source_folder = strategy.folder
                source_observations = _observe_images(_book_images(source_folder, chosen.path), source_folder, books, store, cover_policy, cancel)
                series_source = _load_source(chosen, source_folder, source_observations, parsing, cover_policy, store, cancel)
                _validate_book_artwork(series_source, source_observations)
                diagnostics.extend(series_source.diagnostics)
            art_paths = tuple(path for path in folder.entries if path.stem in series_policy.roles and path.suffix.lower() in IMAGE_SUFFIXES)
            existing_series = _observe_images(art_paths, folder, books, store, cover_policy, cancel)
            publications, old_art, art_decision = _series_operations(folder, strategy, series_source, existing_series, series_policy,
                                                                 tuple(operation.member_id for operation in operations))
            operations.extend(publications)
            retire.extend(old_art)
            if art_decision.status == "no_match":
                diagnostics.append(Decision("no_match", art_decision.reason, folder.directory))
        if not operations:
            if retire:
                raise _ArtworkError("review_required", "superseded_artwork_requires_review", release.path,
                                    tuple(item.path for item in retire))
            _record_book_assessment(source, folder.root, cover_policy, store, authorization)
            return RunResult(tuple(diagnostics) + (Decision("skipped", "artwork_unchanged" if source.image is not None else "book_cover_absent", release.path),),
                             skipped=1, releases=(source.release,))
        dependencies = tuple(operation.member_id for operation in operations)
        for index, old in enumerate({item.path: item for item in retire}.values()):
            operations.append(Operation(f"retire-artwork-{index}", "quarantine", old.path, source=old,
                                        depends_on=dependencies, reason="superseded_owned_artwork"))
        plan = OperationPlan(policy_version, "artwork-" + uuid4().hex, tuple(operations),
                             tuple(RootPermission(root.path) for root in roots), "book_and_series_artwork")
        executor = TransactionExecutor(state_dir, tuple(root.path for root in roots), cancel=cancel,
                                       max_stage_bytes=cover_policy.archive_policy.conversion_scratch_max_bytes)
        result = executor.apply(plan, authorization=authorization)
        if result.committed:
            try:
                _record_book_assessment(source, folder.root, cover_policy, store, authorization)
            except _ArtworkError as error:
                diagnostics.append(error.decision)
            except (OSError, StateStoreError):
                diagnostics.append(Decision("review_required", "artwork_assessment_pending", release.path))
        decisions = tuple(replace(item, path=release.path) if item.path is None else item for item in result.decisions)
        return RunResult(tuple(diagnostics) + decisions, committed=result.committed, skipped=result.skipped,
                         failed=result.failed, deferred_deliveries=result.deferred_deliveries,
                         releases=(source.release,), plan_reports=(PlanReport(plan),))
    except _ArtworkError as error:
        decision = error.decision
    except WorkerError as error:
        decision = Decision(cast(Status, error.status if error.status in _ERROR_STATUSES else "failed"), error.reason, release.path)
    except StateStoreError:
        decision = Decision("review_required", "artwork_provenance_unavailable", release.path)
    except OSError as error:
        decision = _os_decision(error, release.path)
    except ValueError:
        decision = Decision("invalid_input", "invalid_artwork_observation", release.path)
    failed = int(decision.status in {"failed", "permission_denied", "publication_failed"})
    return RunResult((decision,), skipped=0 if failed else 1, failed=failed)


def extract_artwork(
    selected: Sequence[ReleaseSnapshot], catalog: LibrarySnapshot, *, roots: tuple[LibraryRoot, ...],
    parsing: ParsingPolicy, cover_policy: CoverPolicy, series_policy: SeriesCoverPolicy,
    state_dir: Path, policy_version: str, authorization: RunAuthorization, cancel: Event,
    classification: ClassificationPolicy,
    interactive: bool = False,
) -> RunResult:
    """Preview or apply the same concrete groups without retaining encoded batches."""
    try:
        roots = normalize_library_roots(roots)
    except ValueError:
        return RunResult((Decision("invalid_input", "invalid_selected_roots"),))
    selected_by_path = {release.path: release for release in selected}
    known = {release.path: release for release in catalog.releases}
    known.update(selected_by_path)
    folders: dict[Path, _Folder] = {}
    errors: dict[Path, Decision] = {}
    strategies: dict[Path, _SeriesSource] = {}
    diagnostics: list[Decision] = []
    name_matching = MatchingPolicy(
        required_similarity_score=1.0, required_matching_percentage=100.0,
        match_through_image_similarity=False, match_through_identifiers=False,
        classification=classification,
    )
    for directory in sorted({release.parent for release in selected_by_path.values()}):
        try:
            folder = _observe_folder(directory, roots, parsing, known)
            collision = check_book_artwork_collisions(folder.releases)
            if collision.status == "conflict":
                errors[directory] = Decision("conflict", collision.reason, directory,
                                             tuple(str(path) for path in collision.candidates))
                continue
            overlaps = tuple(release.path for release in folder.releases if release.stem in series_policy.roles)
            if overlaps:
                errors[directory] = Decision("conflict", "series_role_book_sidecar_overlap", directory,
                                             tuple(str(path) for path in overlaps))
                continue
            folders[directory] = folder
            if not series_policy.roles:
                continue
            try:
                strategy = _choose_series_source(folder, catalog, roots, parsing, series_policy, name_matching, known)
            except (_ArtworkError, OSError, ValueError) as error:
                decision = error.decision if isinstance(error, _ArtworkError) else _os_decision(error, directory) if isinstance(error, OSError) else Decision("invalid_input", "invalid_series_observation", directory)
                diagnostics.append(decision)
                continue
            if strategy.selection.status in {"ambiguous", "conflict", "invalid_input"}:
                selection_status: Status = "review_required" if strategy.selection.status == "ambiguous" else "conflict" if strategy.selection.status == "conflict" else "invalid_input"
                diagnostics.append(Decision(selection_status, strategy.selection.reason, directory,
                                            tuple(str(path) for path in strategy.selection.candidates)))
            elif strategy.folder is not None:
                strategies[directory] = strategy
            else:
                status: Status = "missing_dependency" if strategy.selection.reason == "library_unavailable" else "no_match"
                diagnostics.append(Decision(status, strategy.selection.reason, directory))
        except _ArtworkError as error:
            errors[directory] = error.decision
        except OSError as error:
            errors[directory] = _os_decision(error, directory)
        except ValueError:
            errors[directory] = Decision("invalid_input", "invalid_folder_observation", directory)
    decisions = diagnostics
    reports: list[PlanReport] = []
    releases: list[ReleaseSnapshot] = []
    committed = skipped = failed = 0
    for directory in sorted({release.parent for release in selected_by_path.values()}):
        jobs = sorted((release for release in selected_by_path.values() if release.parent == directory), key=lambda release: release.path)
        folder_error = errors.get(directory)
        if folder_error is not None:
            decisions.extend(replace(folder_error, path=release.path) for release in jobs)
            count_failed = folder_error.status in {"failed", "permission_denied", "publication_failed"}
            failed += len(jobs) if count_failed else 0
            skipped += 0 if count_failed else len(jobs)
            continue
        folder = folders[directory]
        observed_by_path = {release.path: release for release in folder.releases}
        job_strategy = strategies.get(directory)
        chosen = job_strategy.selection.selected_release if job_strategy is not None else None
        if chosen is not None:
            chosen_path = chosen.path
            jobs.sort(key=lambda release: (release.path != chosen_path, release.path))
        for index, selected_release in enumerate(jobs):
            release = observed_by_path.get(selected_release.path)
            if release is None:
                decisions.append(Decision("invalid_input", "selected_book_not_regular_archive", selected_release.path))
                skipped += 1
                continue
            result = _one_book(release, folder, job_strategy if index == 0 else None, roots=roots, parsing=parsing,
                               cover_policy=cover_policy, series_policy=series_policy, state_dir=state_dir,
                               policy_version=policy_version, authorization=authorization, cancel=cancel,
                               classification=classification)
            decisions.extend(result.decisions)
            reports.extend(result.plan_reports)
            releases.extend(result.releases)
            committed += result.committed
            skipped += result.skipped
            failed += result.failed
    return RunResult(tuple(decisions), committed=committed, skipped=skipped, failed=failed,
                     releases=tuple(releases), plan_reports=tuple(reports))

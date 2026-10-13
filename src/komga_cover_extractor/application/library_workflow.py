"""Original-byte artwork observations for a composed library publication group."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from time import monotonic
from typing import cast
from uuid import uuid4

from ..covers.selection import IMAGE_SUFFIXES, CoverPolicy
from ..covers.series_policy import (
    ArtworkImageObservation,
    BookArtworkObservation,
    SeriesArtworkObservation,
    SeriesCoverPolicy,
    VolumeSeriesArtworkObservation,
    check_book_artwork_collisions,
    plan_series_artwork,
)
from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.libraries import LibraryRoot, LibrarySnapshot
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationPlan,
    RootPermission,
)
from ..domain.releases import ReleaseNumber, ReleaseSnapshot
from ..infrastructure.filesystem import (
    BOOK_SUFFIXES,
    checked_path,
    fingerprint,
    matches,
    sync_directory,
)
from ..infrastructure.images import ByteOutputPolicy, FileOutputPolicy
from ..infrastructure.state import StateStore, StateStoreError
from ..infrastructure.workers import WorkerError
from ..matching.classification import ClassificationPolicy, check_root_suitability
from ..matching.cover_similarity import compare_cover_bytes
from ..matching.series import CoverSimilarity, MatchingPolicy, match_series
from ..parsing.filenames import ParsingPolicy, parse_release
from .artwork_workflow import _observe_image
from .extract_covers import extract_cover

_WORKSPACE_PREFIX = ".kce-workflow-"
_MARKER = ".owner.json"
_MARKER_MAX_BYTES = 1_048_576


def release_source(release: ReleaseSnapshot) -> FileFingerprint:
    source = release.observation.fingerprint
    if source is None:
        raise WorkerError("release_source_observation_required", "invalid_input")
    return source


def resolve_book_bytes(release: ReleaseSnapshot, plans: Sequence[OperationPlan]) -> FileFingerprint:
    source = release_source(release)
    for plan in reversed(plans):
        for operation in reversed(plan.operations):
            if (operation.kind == "publish" and operation.source == source
                    and operation.destination.suffix.lower() in BOOK_SUFFIXES
                    and operation.artifact is not None):
                if operation.artifact.source is None:
                    raise WorkerError("book_artifact_requires_file_source", "invalid_input")
                return operation.artifact.source
    return source

@contextmanager
def open_book_bytes(
    release: ReleaseSnapshot, plans: Sequence[OperationPlan], *, policy: CoverPolicy,
    cancel: Event, convert_native: bool,
) -> Iterator[FileFingerprint]:
    source = resolve_book_bytes(release, plans)
    if not matches(release_source(release)) or not matches(source):
        raise WorkerError("book_byte_source_changed", "stale_plan")
    chapter_disabled = release.kind == "chapter" and not (policy.chapter_support_toggle and policy.extract_chapter_covers)
    if convert_native and source == release_source(release) and not chapter_disabled:
        from .conversion_workflow import prepare_conversion
        from .convert import observe_header
        header = observe_header(source.path, root=source.root, policy=policy.archive_policy, cancel=cancel)
        if header.source != source:
            raise WorkerError("book_byte_source_changed", "stale_plan")
        if header.container in {"rar", "7z"}:
            with prepare_conversion(
                header, roots=(LibraryRoot(source.root),), archive_policy=policy.archive_policy,
                authorization=RunAuthorization(), cancel=cancel,
            ) as prepared:
                if prepared.result.status != "valid":
                    raise WorkerError(prepared.result.reason, prepared.result.status)
                assert prepared.result.artifact is not None
                yield prepared.result.artifact
                for part in prepared.result.sources:
                    if not matches(part):
                        raise WorkerError("book_byte_source_changed", "stale_plan")
            return
    yield source
    if not matches(source) or not matches(release_source(release)):
        raise WorkerError("book_byte_source_changed", "stale_plan")


def make_cover_similarity(
    *, plans: Sequence[OperationPlan], policy: CoverPolicy,
    matching: MatchingPolicy, cancel: Event, convert_native: bool = False,
) -> CoverSimilarity:
    scores: OrderedDict[tuple[FileFingerprint, FileFingerprint, str, str], tuple[float, float]] = OrderedDict()

    def compare(left: ReleaseSnapshot, right: ReleaseSnapshot) -> float | None:
        with ExitStack() as sources:
            first = sources.enter_context(open_book_bytes(left, plans, policy=policy, cancel=cancel, convert_native=convert_native))
            second = sources.enter_context(open_book_bytes(right, plans, policy=policy, cancel=cancel, convert_native=convert_native))
            scratch = sum(source.size for release, source in ((left, first), (right, second))
                          if source != release_source(release))
            if scratch > policy.archive_policy.conversion_scratch_max_bytes:
                raise WorkerError("cover_comparison_scratch_limit", "review_required")
            key = first, second, left.kind, right.kind
            now = monotonic()
            cached = scores.get(key)
            if cached is not None and 0 <= now - cached[0] < 300:
                scores.move_to_end(key)
                return cached[1]
            scores.pop(key, None)
            comparison_policy = replace(policy, compare_detected_cover_to_blank_images=True)
            a = extract_cover(first.path, root=first.root, policy=comparison_policy,
                              output=ByteOutputPolicy(), release_kind=left.kind, cancel=cancel)
            b = extract_cover(second.path, root=second.root, policy=comparison_policy,
                              output=ByteOutputPolicy(), release_kind=right.kind, cancel=cancel)
            for result in (a, b):
                if result.status not in {"valid", "absent"}:
                    raise WorkerError(result.reason, result.status)
                if result.metadata.status == "invalid":
                    raise WorkerError(result.metadata.reason or "invalid_book_metadata", "invalid_input")
            for source in {release_source(left), release_source(right), first, second}:
                if not matches(source):
                    raise WorkerError("cover_comparison_source_changed", "stale_plan")
            if a.status == "absent" or b.status == "absent":
                # An observed absence cannot satisfy the permitted zero threshold.
                value = -1.0
            else:
                assert a.data is not None and b.data is not None
                similarity = compare_cover_bytes(
                    a.data, b.data, required_score=matching.required_image_similarity_score,
                    max_pixels=policy.archive_policy.image_max_pixels,
                    max_bytes=policy.archive_policy.cover_member_max_bytes,
                    timeout=policy.archive_policy.conversion_timeout_seconds, cancel=cancel,
                )
                if similarity.status not in {"matched", "no_match"} or similarity.score is None:
                    raise WorkerError(similarity.reason, similarity.status)
                value = similarity.score
            scores[key] = monotonic(), value
            if len(scores) > 4096:
                scores.popitem(last=False)
            return value

    return compare


class ArtworkWorkspace:
    """Spill encoded images, retaining only journal-owned unresolved publication inputs."""

    def __init__(self, *, root: Path, operation_id: str, authorization: RunAuthorization,
                 max_bytes: int) -> None:
        self.root = root
        self.operation_id = operation_id
        self.authorization = authorization
        self.max_bytes = max_bytes
        self.path: Path | None = None
        self.files: list[FileFingerprint] = []
        self.size = 0
        self.retained = False
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._identity: tuple[int, int] | None = None
        self._marker_source: FileFingerprint | None = None

    def __enter__(self) -> ArtworkWorkspace:
        return self

    def spill(self, artifact: ArtifactSpec) -> ArtifactSpec:
        if artifact.source is not None:
            return artifact
        assert artifact.content is not None
        if self.size + artifact.size > self.max_bytes:
            raise WorkerError("artwork_group_scratch_limit", "review_required")
        if self.path is None:
            if self.authorization.apply:
                checked_path(self.root, self.root)
                self.path = Path(tempfile.mkdtemp(prefix=_WORKSPACE_PREFIX, dir=self.root))
                info = self.path.lstat()
                self._identity = info.st_dev, info.st_ino
                self._save_marker(None)
            else:
                self._temporary = tempfile.TemporaryDirectory(prefix="kce-artwork-preview-")
                self.path = Path(self._temporary.name)
        path = self.path / f"image-{len(self.files)}"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        stream = None
        try:
            stream = os.fdopen(descriptor, "wb")
            with stream:
                if stream.write(artifact.content) != artifact.size:
                    raise OSError("artwork_stage_short_write")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if stream is None:
                os.close(descriptor)
        source = fingerprint(path, self.root if self.authorization.apply else self.path)
        if source.sha256 != artifact.digest or source.size != artifact.size:
            raise WorkerError("artwork_stage_identity_mismatch", "failed")
        self.files.append(source)
        self.size += source.size
        return ArtifactSpec(source=source, media_type=artifact.media_type)

    def spill_plans(self, plans: Sequence[OperationPlan]) -> list[OperationPlan]:
        converted: list[OperationPlan] = []
        spills: dict[tuple[str, str | None], ArtifactSpec] = {}
        for plan in plans:
            operations: list[Operation] = []
            permissions = {permission.path: permission for permission in plan.roots}
            for operation in plan.operations:
                artifact = operation.artifact
                if artifact is not None and artifact.content is not None:
                    key = artifact.digest, artifact.media_type
                    spilled = spills.get(key)
                    if spilled is None:
                        spilled = self.spill(artifact)
                        spills[key] = spilled
                    assert spilled.source is not None
                    if spilled.source.root not in permissions:
                        permissions[spilled.source.root] = RootPermission(spilled.source.root, write=False)
                    operation = replace(operation, artifact=spilled)
                operations.append(operation)
            converted.append(replace(plan, operations=tuple(operations), roots=tuple(permissions.values())))
        return converted

    def _save_marker(self, plan_hash: str | None) -> None:
        assert self.path is not None and self._identity is not None
        checked_path(self.path, self.root)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (info.st_dev, info.st_ino) != self._identity:
            raise WorkerError("artwork_workspace_identity_changed", "review_required")
        value = {"format": "kce-artwork-workspace-v1", "operation_id": self.operation_id,
                 "plan_sha256": plan_hash, "device": info.st_dev, "inode": info.st_ino,
                 "files": [source.as_dict() for source in self.files]}
        content = json.dumps(value).encode("utf-8")
        if len(content) > _MARKER_MAX_BYTES:
            raise WorkerError("artwork_workspace_manifest_limit", "review_required")
        marker = self.path / _MARKER
        target = marker if self._marker_source is None else self.path / ".owner-next.json"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        stream = None
        try:
            stream = os.fdopen(descriptor, "wb")
            with stream:
                if stream.write(content) != len(content):
                    raise OSError("artwork_marker_short_write")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if stream is None:
                os.close(descriptor)
        if self._marker_source is not None:
            if not matches(self._marker_source):
                raise WorkerError("artwork_workspace_marker_changed", "review_required")
            os.replace(target, marker)
        self._marker_source = fingerprint(marker, self.root)
        sync_directory(self.path)
        sync_directory(self.root)

    def record(self, plan: OperationPlan) -> None:
        if self.path is None or not self.authorization.apply:
            return
        if plan.operation_id != self.operation_id:
            raise WorkerError("artwork_workspace_operation_mismatch", "invalid_input")
        self._save_marker(sha256(json.dumps(plan.as_dict(), sort_keys=True).encode("utf-8")).hexdigest())

    def __exit__(self, *error: object) -> None:
        if self.path is None or self.retained:
            return
        if self._temporary is not None:
            self._temporary.cleanup()
            return
        checked_path(self.path, self.root)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (info.st_dev, info.st_ino) != self._identity:
            raise WorkerError("artwork_workspace_identity_changed", "review_required")
        if self._marker_source is None or not matches(self._marker_source):
            raise WorkerError("artwork_workspace_marker_changed", "review_required")
        marker = self._marker_source.path
        expected = {marker, *(source.path for source in self.files)}
        if set(self.path.iterdir()) != expected:
            raise WorkerError("artwork_workspace_has_unrecorded_files", "review_required")
        for source in self.files:
            if not matches(source):
                raise WorkerError("artwork_workspace_changed", "review_required")
        for source in self.files:
            source.path.unlink()
        marker.unlink(missing_ok=True)
        self.path.rmdir()
        sync_directory(self.root)


def release_artwork_workspace(
    plan: OperationPlan, *, state_dir: Path, authorization: RunAuthorization,
) -> Decision | None:
    directories = {operation.artifact.source.path.parent for operation in plan.operations
                   if operation.artifact is not None and operation.artifact.source is not None
                   and operation.artifact.source.path.parent.name.startswith(_WORKSPACE_PREFIX)}
    if not directories or not authorization.apply:
        return None
    try:
        record = StateStore(state_dir).get_operation(plan.operation_id)
        if record is None or record.phase != "complete" or record.plan.as_dict() != plan.as_dict():
            return Decision("review_required", "artwork_group_not_settled", candidates=(plan.operation_id,))
        for directory in directories:
            permission = next(root for root in plan.roots if root.write and directory.is_relative_to(root.path))
            if directory.parent != permission.path:
                raise ValueError("artwork workspace root mismatch")
            checked_path(directory, permission.path, allow_missing=True)
            if not directory.exists():
                continue
            info = directory.lstat()
            marker = directory / _MARKER
            marker_source = fingerprint(marker, permission.path)
            if marker_source.size > _MARKER_MAX_BYTES:
                raise ValueError("artwork marker limit")
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                value = json.loads(stream.read(_MARKER_MAX_BYTES + 1))
            if (not isinstance(value, dict) or set(value) != {"format", "operation_id", "plan_sha256", "device", "inode", "files"}
                    or value["format"] != "kce-artwork-workspace-v1" or value["operation_id"] != plan.operation_id
                    or value["plan_sha256"] != sha256(json.dumps(plan.as_dict(), sort_keys=True).encode("utf-8")).hexdigest()
                    or (info.st_dev, info.st_ino) != (value["device"], value["inode"])
                    or info.st_uid != os.getuid() or not isinstance(value["files"], list)):
                raise ValueError("invalid artwork ownership")
            from .artwork_workflow import _decode_fingerprint
            sources = tuple(_decode_fingerprint(item) for item in value["files"])
            planned_sources = {operation.artifact.source for operation in plan.operations
                               if operation.artifact is not None and operation.artifact.source is not None
                               and operation.artifact.source.path.parent == directory}
            if not planned_sources <= set(sources) or any(
                source.root != permission.path or source.path.parent != directory
                or not source.path.name.startswith("image-") or not source.path.name.removeprefix("image-").isascii()
                or not source.path.name.removeprefix("image-").isdigit() or not matches(source) for source in sources
            ):
                raise ValueError("artwork identity changed")
            if set(directory.iterdir()) != {marker, *(source.path for source in sources)} or not matches(marker_source):
                raise ValueError("artwork workspace changed")
            for source in sources:
                source.path.unlink()
            marker.unlink()
            directory.rmdir()
            sync_directory(permission.path)
    except (OSError, StateStoreError, TypeError, ValueError, StopIteration):
        return Decision("review_required", "artwork_workspace_cleanup_requires_review", candidates=(plan.operation_id,))
    return None


def virtual_file_locations(files: Mapping[Path, FileFingerprint], plans: Sequence[OperationPlan]) -> dict[Path, Path]:
    locations = {source: source for source in files}
    book_publications = {operation.source.path for plan in plans for operation in plan.operations
                         if operation.kind == "publish" and operation.source is not None
                         and operation.destination.suffix.lower() in BOOK_SUFFIXES}
    for plan in plans:
        for operation in plan.operations:
            if operation.kind == "move" and operation.source is not None:
                locations[operation.source.path] = operation.destination
            elif operation.kind == "quarantine" and operation.source is not None and operation.source.path not in book_publications:
                locations.pop(operation.source.path, None)
    return locations


def plan_projected_artwork(
    selected: Sequence[ReleaseSnapshot], catalog: LibrarySnapshot, *, roots: tuple[LibraryRoot, ...],
    parsing: ParsingPolicy, cover_policy: CoverPolicy, series_policy: SeriesCoverPolicy,
    classification: ClassificationPolicy, files: Mapping[Path, FileFingerprint],
    directories: Sequence[Path], previous_plans: Sequence[OperationPlan], state_dir: Path,
    policy_version: str, workspace: ArtworkWorkspace, cancel: Event, convert_native: bool = False,
    before: Sequence[ReleaseSnapshot] = (),
) -> RunResult:
    """Use current paths for selection and retain pre-change identities for ownership."""
    known = {release_source(release).path: release for release in catalog.releases}
    known.update((release_source(release).path, release) for release in selected)
    locations = virtual_file_locations(files, previous_plans)
    store = StateStore(state_dir)
    original_books = {release_source(release).path: replace(
        release, observation=replace(release.observation, path=release_source(release).path),
    ) for release in (*before, *known.values())}
    images: dict[Path, ArtworkImageObservation] = {}
    image_sources: dict[Path, FileFingerprint] = {}
    source_images: dict[Path, ArtworkImageObservation | None] = {}
    releases = dict(known)
    operations: list[Operation] = []
    decisions: list[Decision] = []
    folders = {release.parent for release in selected}
    virtual_directories = set(directories)
    for plan in previous_plans:
        for operation in plan.operations:
            if operation.kind == "mkdir":
                virtual_directories.add(operation.destination)
            elif operation.kind == "rmdir":
                virtual_directories.discard(operation.destination)
            elif (operation.kind == "publish" and operation.artifact is not None
                  and operation.destination.suffix.lower() in IMAGE_SUFFIXES and operation.source is not None):
                source = operation.source.path
                owner = known.get(source)
                suffix = operation.destination.suffix.lower()
                media = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix)
                if media is not None:
                    images[operation.destination] = ArtworkImageObservation(
                        operation.destination, operation.artifact.digest, suffix, media, operation.artifact,
                        True, owner.path if owner is not None else None,
                    )
    for source, destination in locations.items():
        if destination.parent not in folders or destination.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        observed = files[source]
        root = next(root for root in roots if root.path == observed.root)
        decoded = _observe_image(source, root, original_books, store, cover_policy, cancel)
        owner = (known.get(decoded.image.owner_book) or original_books.get(decoded.image.owner_book)
                 if decoded.image.owner_book is not None else None)
        if destination not in images:
            images[destination] = replace(decoded.image, path=destination, owner_book=owner.path if owner is not None else None)
        image_sources[destination] = observed

    def load(release: ReleaseSnapshot, *, sidecar: bool) -> ArtworkImageObservation | None:
        source = release_source(release)
        if source.path in source_images:
            return source_images[source.path]
        if cancel.is_set():
            raise WorkerError("shutdown_requested", "cancelled")
        with open_book_bytes(release, previous_plans, policy=cover_policy, cancel=cancel, convert_native=convert_native) as byte_source:
            extracted = extract_cover(byte_source.path, root=byte_source.root, policy=cover_policy,
                                      output=FileOutputPolicy(), release_kind=release.kind, cancel=cancel)
            if not matches(byte_source):
                raise WorkerError("artwork_source_changed", "stale_plan")
        if extracted.status not in {"valid", "absent"}:
            raise WorkerError(extracted.reason, extracted.status)
        if extracted.metadata.status == "invalid":
            raise WorkerError(extracted.metadata.reason or "invalid_book_metadata", "invalid_input")
        if not matches(source):
            raise WorkerError("artwork_source_changed", "stale_plan")
        observation = replace(release.observation, metadata=extracted.metadata.values,
                              archive_pages=extracted.archive_pages)
        parsed = parse_release(observation, parsing)
        releases[source.path] = replace(parsed, provenance=tuple(dict.fromkeys((*parsed.provenance, "archive:observed"))))
        decisions.extend(Decision("observed", reason, release.path) for reason in extracted.diagnostics)
        if extracted.status == "absent":
            decisions.append(Decision("no_match", extracted.reason, release.path))
            source_images[source.path] = None
            return None
        assert extracted.artifact is not None and extracted.suffix is not None and extracted.media_type is not None
        destination = release.path.with_suffix(extracted.suffix)
        existing = tuple(image for image in images.values() if image.path.parent == release.parent and image.path.stem == release.stem)
        custom = tuple(image.path for image in existing if not image.generated and image.sha256 != extracted.artifact.digest)
        if custom:
            raise WorkerError("custom_book_artwork", "review_required")
        same = next((image for image in existing if image.sha256 == extracted.artifact.digest and image.suffix == extracted.suffix), None)
        if same is not None:
            image = same
        else:
            artifact = workspace.spill(extracted.artifact)
            image = ArtworkImageObservation(destination, artifact.digest, extracted.suffix, extracted.media_type,
                                            artifact, True, release.path)
            if sidecar:
                before = image_sources.get(destination)
                if before is not None and before.path != destination:
                    before = None
                operations.append(Operation(f"book-art-{len(operations)}", "publish", destination,
                                            source=source, destination_before=before, artifact=artifact,
                                            collision="replace" if before is not None else "absent", reason="book_artwork"))
                for old in existing:
                    if old.generated and old.sha256 != image.sha256 and old.path != destination:
                        before_old = image_sources.get(old.path)
                        if before_old is not None:
                            operations.append(Operation(f"retire-art-{len(operations)}", "quarantine", before_old.path,
                                                        source=before_old, depends_on=tuple(op.member_id for op in operations),
                                                        reason="superseded_owned_artwork"))
                            images.pop(old.path, None)
        images[image.path] = image
        source_images[source.path] = image
        return image

    selected_sources = {release_source(release).path for release in selected}
    for folder in sorted(folders):
        members = tuple(release for release in known.values() if release.parent == folder)
        collision = check_book_artwork_collisions(members)
        if collision.status == "conflict":
            decisions.append(Decision("conflict", collision.reason, folder, tuple(str(path) for path in collision.candidates)))
            continue
        if any(release.stem in series_policy.roles for release in members):
            decisions.append(Decision("conflict", "series_role_book_sidecar_overlap", folder))
            continue
        root = next(root for root in roots if root.contains(folder))
        for release in members:
            if release_source(release).path not in selected_sources:
                continue
            suitable = check_root_suitability((release,), root, classification, classification_files=members)
            if suitable.status != "suitable":
                decisions.append(Decision("no_match", suitable.reason, release.path))
                continue
            load(release, sidecar=True)
        book_images = tuple(BookArtworkObservation(release.path, tuple(image for image in images.values()
                            if image.path.parent == release.parent and image.path.stem == release.stem),
                            "classification:one_shot" in release.provenance) for release in members)
        existing = tuple(image for image in images.values() if image.path.parent == folder and image.path.stem in series_policy.roles)
        observation = SeriesArtworkObservation(folder, book_images, existing,
                                                tuple(path for path in virtual_directories if path.parent == folder and not path.name.startswith(".kce-")),
                                                folder == root.path)
        policy = replace(series_policy, observation=observation)
        strategy = plan_series_artwork(members, policy=policy)
        if strategy.reason == "volume_series_match_required":
            if not catalog.available:
                decisions.append(Decision("missing_dependency", catalog.reason or "library_unavailable", folder))
                continue
            probe = replace(members[0], kind="volume", number=ReleaseNumber.missing(), identifiers=())
            volumes = tuple(release for release in known.values() if release.kind == "volume"
                            and release.observation.root_role in {"library", "both"})
            matched = match_series(probe, LibrarySnapshot(volumes, roots), MatchingPolicy(
                required_similarity_score=1.0, required_matching_percentage=100.0,
                match_through_image_similarity=False, match_through_identifiers=False, classification=classification))
            if matched.status != "matched" or matched.selected is None:
                decisions.append(Decision("ambiguous" if matched.status == "ambiguous" else "no_match",
                                          "multiple_volume_series_matches" if matched.status == "ambiguous" else "volume_series_match_required",
                                          folder, tuple(str(candidate.series_path) for candidate in matched.candidates)))
                continue
            source_folder = matched.selected.series_path
            volume_releases = tuple(release for release in volumes if release.parent == source_folder)
            volume_images = tuple(BookArtworkObservation(release.path, ()) for release in volume_releases)
            volume = VolumeSeriesArtworkObservation(source_folder, volume_releases, volume_images, "matched",
                                                   any(path.parent == source_folder for path in virtual_directories))
            policy = replace(policy, observation=replace(observation, matched_volume_series=(volume,)))
            strategy = plan_series_artwork(members, policy=policy)
        if strategy.reason == "selected_book_artwork_absent" and strategy.selected_release is not None:
            chosen = strategy.selected_release
            source_root = next(root for root in roots if root.contains(chosen.path))
            source_members = tuple(release for release in known.values() if release.parent == chosen.parent)
            suitable = check_root_suitability((chosen,), source_root, classification, classification_files=source_members)
            if suitable.status != "suitable":
                decisions.append(Decision("no_match", suitable.reason, folder))
                continue
            image = load(chosen, sidecar=release_source(chosen).path in selected_sources)
            if image is not None:
                chosen_book = BookArtworkObservation(chosen.path, (image,), "classification:one_shot" in chosen.provenance)
                artwork_observation = policy.observation
                assert artwork_observation is not None
                if artwork_observation.matched_volume_series:
                    volume = artwork_observation.matched_volume_series[0]
                    volume = replace(volume, book_artwork=tuple(chosen_book if book.book == chosen.path else book for book in volume.book_artwork))
                    policy = replace(policy, observation=replace(artwork_observation, matched_volume_series=(volume,)))
                else:
                    policy = replace(policy, observation=replace(artwork_observation,
                                     book_artwork=tuple(chosen_book if book.book == chosen.path else book for book in artwork_observation.book_artwork)))
                strategy = plan_series_artwork(members, policy=policy)
        if strategy.status in {"ambiguous", "conflict", "invalid_input"}:
            decisions.append(Decision("review_required" if strategy.status == "ambiguous" else cast(Status, strategy.status),
                                      strategy.reason, folder, tuple(str(path) for path in strategy.candidates)))
            continue
        if strategy.status == "no_match":
            decisions.append(Decision("no_match", strategy.reason, folder))
        for association in strategy.associations:
            if association.action == "keep":
                continue
            assert strategy.selected_release is not None and association.source_image.artifact is not None
            destination_before = image_sources.get(association.destination)
            if destination_before is not None and destination_before.path != association.destination:
                destination_before = None
            operations.append(Operation(f"series-art-{len(operations)}", "publish", association.destination,
                                        source=release_source(strategy.selected_release), destination_before=destination_before,
                                        artifact=association.source_image.artifact,
                                        depends_on=tuple(op.member_id for op in operations),
                                        collision="replace" if destination_before is not None else "absent", reason="series_artwork_" + association.role))
            for old_path in association.retire_after_commit:
                old = image_sources.get(old_path)
                if old is not None:
                    operations.append(Operation(f"retire-series-{len(operations)}", "quarantine", old.path,
                                                source=old, depends_on=tuple(op.member_id for op in operations),
                                                reason="superseded_owned_artwork"))
    if any(decision.status not in {"observed", "skipped", "no_match"} for decision in decisions):
        return RunResult(tuple(decisions), releases=tuple(releases.values()))
    if not operations:
        return RunResult((*decisions, Decision("skipped", "artwork_unchanged")), skipped=1,
                         releases=tuple(releases.values()))
    extras = {operation.artifact.source.root for operation in operations
              if operation.artifact is not None and operation.artifact.source is not None
              and not any(root.contains(operation.artifact.source.path) for root in roots)}
    plan = OperationPlan(policy_version, "artwork-" + uuid4().hex, tuple(operations),
                         (*tuple(RootPermission(root.path) for root in roots),
                          *tuple(RootPermission(path, write=False) for path in sorted(extras))), "book_and_series_artwork")
    return RunResult(tuple(decisions), plans=(plan,), releases=tuple(releases.values()))

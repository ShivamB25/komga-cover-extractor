"""Ordered organization decisions over original bytes and a virtual path view."""
from __future__ import annotations

import os
import select
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import cast

import regex

from ..covers.selection import CoverPolicy
from ..covers.series_policy import (
    ArtworkImageObservation,
    BookArtworkObservation,
    SeriesArtworkObservation,
    SeriesCoverPolicy,
    plan_series_artwork,
)
from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.libraries import LibraryRoot, LibrarySnapshot, normalize_library_roots
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    OperationPlan,
    PlanReport,
)
from ..domain.releases import ReleaseNumberKind, ReleaseSnapshot
from ..infrastructure.filesystem import (
    BOOK_SUFFIXES,
    FilesystemError,
    checked_path,
    fingerprint,
)
from ..infrastructure.state import StateStore, StateStoreError
from ..infrastructure.transactions import TransactionExecutor
from ..infrastructure.workers import WorkerError
from ..matching.classification import (
    folder_name_is_rename_candidate,
    folder_series_agreement,
    route_releases_to_root,
)
from ..matching.ranking import RankingPolicy, decide_upgrade, rank_release
from ..matching.series import CoverSimilarity, MatchingPolicy, match_series
from ..parsing.filenames import ParsingPolicy, RenamePolicy, render_release
from ..parsing.normalization import (
    clean_str,
    remove_brackets,
    remove_dual_space,
    similar,
)
from .planning import build_file_plan, project_release

OPTIONS = (
    "delete_unacceptable_files_toggle", "delete_chapters_from_downloads_toggle",
    "rename_files_in_download_folders_toggle", "create_folders_for_items_in_download_folder_toggle",
    "check_for_duplicate_volumes_toggle", "check_for_existing_series_toggle",
    "rename_dirs_in_download_folder_toggle", "move_series_to_correct_library_toggle",
    "move_lone_files_to_similar_folder", "replace_series_name_in_file_name_with_similar_folder_name",
    "manual_rename", "manual_delete",
)
STAGES = ("all", "filters", "before_matching", "rename_and_duplicates", "after_matching")
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tbn"})
_IMAGE_DETAILS = {
    ".jpg": (".jpg", "image/jpeg"), ".jpeg": (".jpg", "image/jpeg"),
    ".tbn": (".jpg", "image/jpeg"), ".png": (".png", "image/png"), ".webp": (".webp", "image/webp"),
}


def _after_matching(command: str, stage: str) -> bool:
    return stage in {"all", "after_matching"} and command != "duplicates"


def _source_path(release: ReleaseSnapshot) -> Path:
    observed = release.observation.fingerprint
    return release.path if observed is None else observed.path


def _same_domain(left: ReleaseSnapshot, right: ReleaseSnapshot) -> bool:
    return (left.kind == right.kind and left.number.kind != "missing" and right.number.kind != "missing"
            and ("filename:extra_marker" in left.provenance) == ("filename:extra_marker" in right.provenance)
            and left.number.covers(right.number) and right.number.covers(left.number))


def _overlapping(left: ReleaseSnapshot, right: ReleaseSnapshot) -> bool:
    if (left.kind != right.kind or left.number.kind == "missing" or right.number.kind == "missing"
            or left.number.part != right.number.part
            or ("filename:extra_marker" in left.provenance) != ("filename:extra_marker" in right.provenance)):
        return False
    if left.number.kind == "range" and right.number.kind == "range":
        return max(left.number.values[0], right.number.values[0]) <= min(left.number.values[1], right.number.values[1])
    return any(left.number.contains(value) for value in right.number.values) or any(
        right.number.contains(value) for value in left.number.values
    )


def _quality(
    incoming: ReleaseSnapshot, existing: ReleaseSnapshot, ranking: RankingPolicy,
    artifacts: Mapping[Path, ArtifactSpec],
) -> tuple[str, str]:
    incoming_rank = rank_release(incoming, ranking.rules)
    existing_rank = rank_release(existing, ranking.rules)
    if incoming_rank.status != "ranked" or existing_rank.status != "ranked":
        return "invalid_input", "release_kind_unavailable"
    if incoming_rank.total_score > existing_rank.total_score:
        return "upgrade", "strictly_higher_rank"
    if incoming_rank.total_score < existing_rank.total_score:
        return "not_upgrade", "lower_rank"
    if _source_path(incoming) in artifacts or _source_path(existing) in artifacts:
        left_artifact, right_artifact = artifacts.get(_source_path(incoming)), artifacts.get(_source_path(existing))
        left_source, right_source = incoming.observation.fingerprint, existing.observation.fingerprint
        left_hash = left_artifact.digest if left_artifact is not None else left_source.sha256 if left_source is not None else None
        right_hash = right_artifact.digest if right_artifact is not None else right_source.sha256 if right_source is not None else None
        if left_hash is None or right_hash is None:
            return "review_required", "equal_rank_content_identity_unknown"
        return ("not_upgrade", "identical_content") if left_hash == right_hash else ("review_required", "equal_rank_different_content")
    # The projected name can affect ranking, but content identity is observed
    # at the original path. No fingerprint of a nonexistent output is invented.
    original_incoming = replace(incoming, observation=replace(incoming.observation, path=_source_path(incoming)))
    original_existing = replace(existing, observation=replace(existing.observation, path=_source_path(existing)))
    identity = decide_upgrade(original_incoming, original_existing, RankingPolicy())
    return identity.status, identity.reason


def _folder_name(name: str, parsing: ParsingPolicy) -> str:
    keywords = parsing.number_policy._compiled.volume_keywords_expression
    value = regex.sub(
        rf"(\b|\s)((\s|)-(\s|)|)(Part|)({keywords})([-_. ]|)([-_. ]|)([0-9]+)(\b|\s).*",
        "", name.replace("_extra", ".5"), flags=regex.IGNORECASE,
    ).strip()
    value = remove_brackets(value)
    value = regex.sub(r"[\[\](){}]", "", value)
    value = regex.sub(r"([A-Za-z])[_-]", r"\1 ", value)
    value = regex.sub(r"([A-Za-z]):", r"\1 -", value)
    return remove_dual_space(value.replace("?", "")).strip()


def _safe_component(name: str) -> bool:
    return bool(name.strip() and name not in {".", ".."} and not name.startswith(".kce-")
                and not any(character in name for character in "/\\\x00")
                and regex.match(r"^[A-Za-z]:", name) is None)


def plan_organization(
    releases: Sequence[ReleaseSnapshot], catalog: LibrarySnapshot, *,
    roots: tuple[LibraryRoot, ...], parsing: ParsingPolicy, renaming: RenamePolicy,
    matching: MatchingPolicy, ranking: RankingPolicy, options: Mapping[str, bool],
    files: Mapping[Path, FileFingerprint], directories: Sequence[Path], policy_version: str,
    command: str = "organize", stage: str = "all", unacceptable_keywords: tuple[str, ...] = (),
    ignored_folder_names: tuple[str, ...] = (), cover_similarity: CoverSimilarity | None = None,
    series_policy: SeriesCoverPolicy | None = None, series_art_owners: Mapping[Path, Path] | None = None,
    artwork: Mapping[Path, tuple[ArtifactSpec, str, str]] | None = None,
    choices: Mapping[tuple[str, Path], str] | None = None,
    previous_plans: Sequence[OperationPlan] = (), cancel: Event | None = None,
    scope_sources: frozenset[Path] | None = None,
) -> RunResult:
    """Plan organize/duplicates or either ordered organizer segment of ``run``.

    Earlier plans may be supplied to collapse a before-matching virtual view
    into one final group. Reads remain the caller's original observations.
    Dictionary decisions and header/conversion/artwork services belong to the
    pipeline, which inserts them at their legacy positions.
    """
    if command not in {"organize", "duplicates", "run"} or stage not in STAGES:
        return RunResult((Decision("invalid_input", "invalid_organizer_command_or_stage"),))
    if set(options) - set(OPTIONS) or any(type(value) is not bool for value in options.values()):
        return RunResult((Decision("invalid_input", "invalid_organizer_options"),))
    settings = {key: options.get(key, False) for key in OPTIONS}
    observed_files = dict(files)
    original: dict[Path, ReleaseSnapshot] = {}
    locations = {path: path for path in observed_files}
    for release in (*catalog.releases, *releases):
        source = _source_path(release)
        original[source] = release
        locations[source] = release.path
    selected = {_source_path(release) for release in releases}
    retired: dict[Path, str] = {}
    replacements: set[Path] = set()
    generated: dict[Path, tuple[FileFingerprint, ArtifactSpec]] = {}
    source_artifacts: dict[Path, ArtifactSpec] = {}
    cleanup_sources: dict[Path, str] = {}
    links: list[tuple[Path, ...]] = []
    operation_reasons: dict[Path, str] = {}
    generated_book_images: dict[Path, Path] = {}
    generated_series_images: dict[Path, Path] = {}
    directories_set = set(directories)
    owners = {} if series_art_owners is None else dict(series_art_owners)
    supplied_artwork = {} if artwork is None else dict(artwork)
    answers = {} if choices is None else dict(choices)
    decisions: list[Decision] = []
    blocked: set[Path] = set()
    for plan in previous_plans:
        transformed_sources = {operation.source.path for operation in plan.operations
                               if operation.kind == "publish" and operation.source is not None
                               and operation.destination.suffix.lower() in BOOK_SUFFIXES}
        by_member = {operation.member_id: operation for operation in plan.operations}
        for operation in plan.operations:
            if operation.kind == "move" and operation.source is not None:
                locations[operation.source.path] = operation.destination
                operation_reasons[operation.source.path] = operation.reason
            elif operation.kind == "quarantine" and operation.source is not None:
                if operation.source.path in transformed_sources:
                    cleanup_sources[operation.source.path] = operation.reason
                else:
                    retired[operation.source.path] = operation.reason
            elif operation.kind == "publish" and operation.source is not None and operation.artifact is not None:
                if operation.source.path in transformed_sources and operation.destination.suffix.lower() in BOOK_SUFFIXES:
                    locations[operation.source.path] = operation.destination
                    source_artifacts[operation.source.path] = operation.artifact
                else:
                    generated[operation.destination] = (operation.source, operation.artifact)
                    logical_book = locations.get(operation.source.path, operation.source.path)
                    if (operation.reason == "book_artwork"
                            or operation.destination.parent == logical_book.parent and operation.destination.stem == logical_book.stem):
                        generated_book_images[operation.destination] = operation.source.path
                    elif (operation.reason.startswith("series_artwork_")
                          and operation.destination.parent == logical_book.parent):
                        generated_series_images[operation.destination] = operation.source.path
            if operation.destination_before is not None:
                replacements.add(operation.destination)
            dependency_sources = tuple(dependency.path for member in operation.depends_on
                                       if (dependency := by_member[member].source) is not None)
            if operation.source is not None and dependency_sources:
                links.append((operation.source.path, *dependency_sources))
    for source, release in original.items():
        observed = release.observation.fingerprint
        if observed is None or observed_files.get(source) != observed:
            decisions.append(Decision("review_required", "release_source_unobserved_or_changed", source))
            blocked.add(source)
    def ignored_source(source: Path) -> bool:
        root = next((root for root in roots if root.contains(source)), None)
        if root is None:
            return True
        relative = source.relative_to(root.path)
        return (any(part.startswith(".") for part in relative.parts)
                or any(part in ignored_folder_names for part in relative.parent.parts))

    for source in original:
        if ignored_source(source):
            blocked.add(source)
        elif locations[source].suffix.lower() not in matching.classification.supported_extensions:
            blocked.add(source)
            if source in selected:
                decisions.append(Decision("no_match", "organization_requires_supported_container", source))
    sidecars: dict[Path, tuple[Path, ...]] = {}
    images_by_stem: dict[tuple[Path, str], list[Path]] = {}
    for path in observed_files:
        if path.suffix.lower() in IMAGE_SUFFIXES:
            images_by_stem.setdefault((path.parent, path.stem), []).append(path)
    stems: dict[tuple[Path, str], list[Path]] = {}
    for source in original:
        stems.setdefault((source.parent, source.stem), []).append(source)
    for source in original:
        associated = tuple(images_by_stem.get((source.parent, source.stem), ()))
        if len(stems[(source.parent, source.stem)]) > 1 and associated:
            shared_stem_candidates = tuple(str(path) for path in stems[(source.parent, source.stem)])
            decisions.append(Decision("conflict", "shared_book_sidecar_stem", source, shared_stem_candidates))
            blocked.update(stems[(source.parent, source.stem)])
        sidecars[source] = associated

    def active() -> dict[Path, ReleaseSnapshot]:
        return {
            source: project_release(release, locations[source], parsing=parsing, roots=roots)
            for source, release in original.items() if source not in retired
        }

    def download(source: Path) -> bool:
        return any("download" in root.roles and root.contains(locations[source]) for root in roots)

    def library(source: Path) -> bool:
        return any("library" in root.roles and root.contains(locations[source]) for root in roots)

    def approved(action: str, source: Path, target: Path | None = None) -> bool:
        manual = settings["manual_rename" if action == "rename" else "manual_delete"]
        if not manual:
            return True
        key = (f"{action}:{target}" if target is not None else action, source)
        answer = answers.get(key)
        expected = str(target) if target is not None else "yes"
        if answer == expected:
            return True
        if answer == "skip":
            decisions.append(Decision("skipped", f"manual_{action}_declined", source))
        else:
            decisions.append(Decision("review_required", f"manual_{action}_required", source,
                                      (str(target),) if target is not None else (str(source),)))
        return False

    def retire_group(source: Path, reason: str) -> None:
        retired[source] = reason
        for image in sidecars.get(source, ()):
            retired[image] = "associated_book_artwork_" + reason
        links.append((source, *sidecars.get(source, ())))

    def relocate(source: Path, target: Path, *, rename: bool = False, reason: str | None = None) -> bool:
        if source in blocked or source in retired or locations[source] == target:
            return False
        if rename and not approved("rename", source, target):
            return False
        prior = locations[source]
        locations[source] = target
        semantic_reason = reason or ("restructure_release" if rename and renaming.restructure_when_renaming
                                    else "rename_release" if rename else "move_release")
        operation_reasons[source] = semantic_reason
        for image in sidecars.get(source, ()):
            if image not in retired:
                locations[image] = target.parent / (target.stem + image.suffix)
                operation_reasons[image] = semantic_reason + "_sidecar"
        links.append((source, *sidecars.get(source, ())))
        decisions.append(Decision("planned", semantic_reason, source,
                                  (str(prior), str(target))))
        return True

    def rename_folder(folder: Path, target: Path, *, reason: str = "directory_rename") -> None:
        if folder == target or not _safe_component(target.name):
            return
        members = [source for source, path in locations.items() if source not in retired and path.is_relative_to(folder)]
        if any(source in blocked for source in members):
            decisions.append(Decision("review_required", "folder_contains_unresolved_release", folder))
            return
        opaque = [path for path in directories_set if path.is_relative_to(folder)
                  and (path.name.startswith(".kce-") or path.name in ignored_folder_names)]
        if opaque:
            decisions.append(Decision("review_required", "folder_contains_unobserved_directory", folder,
                                      tuple(str(path) for path in sorted(opaque))))
            return
        if settings["manual_rename"] and not approved("rename", folder, target):
            return
        for source in members:
            locations[source] = target / locations[source].relative_to(folder)
            operation_reasons[source] = reason
        links.append(tuple(members))
        updated_directories = {target / path.relative_to(folder) if path.is_relative_to(folder) else path
                               for path in directories_set}
        directories_set.clear()
        directories_set.update(updated_directories)
        decisions.append(Decision("planned", reason, folder, (str(target),)))

    def choose_duplicate(group: Sequence[Path], *, key: Path) -> Path | None:
        view = active()
        ranked = [(source, rank_release(view[source], ranking.rules).total_score) for source in group]
        highest = max(score for _, score in ranked)
        top = sorted(source for source, score in ranked if score == highest)
        digests = {source_artifacts[source].digest if source in source_artifacts else observed_files[source].sha256 for source in top}
        answer = answers.get(("duplicate", key))
        if answer == "skip":
            decisions.append(Decision("skipped", "duplicate_choice_declined", key))
            return None
        if answer is not None and Path(answer) in group:
            return Path(answer)
        if len(top) == 1 or len(digests) == 1:
            return top[0]
        decisions.append(Decision("review_required", "duplicate_choice_required", key,
                                  tuple(str(source) for source in sorted(group))))
        return None

    before = stage in {"all", "before_matching", "rename_and_duplicates"} and command != "duplicates"
    filters = stage in {"all", "before_matching", "filters"} and command != "duplicates"
    after = _after_matching(command, stage)
    if before or filters:
        patterns = tuple(regex.compile(pattern, regex.IGNORECASE) for pattern in unacceptable_keywords)
        if filters and settings["delete_unacceptable_files_toggle"] and patterns:
            for source in sorted(observed_files):
                if scope_sources is not None and source not in scope_sources:
                    continue
                if (not any("download" in root.roles and root.contains(source) for root in roots)
                        or ignored_source(source)):
                    continue
                if any(pattern.search(source.name) for pattern in patterns):
                    if source in blocked or not approved("retire", source):
                        continue
                    if source in original:
                        retire_group(source, "unacceptable_file")
                    else:
                        retired[source] = "unacceptable_file"
                    decisions.append(Decision("planned", "quarantine_unacceptable_file", source))
        if filters and settings["delete_chapters_from_downloads_toggle"]:
            exception_patterns = tuple(regex.compile(pattern, regex.IGNORECASE) for pattern in parsing.exception_keywords)
            for source, release in active().items():
                if source not in selected or source in blocked or not download(source):
                    continue
                if (release.kind == "chapter" and release.path.suffix.lower() in matching.classification.manga_extensions
                        and not parsing.number_policy._compiled.volume_marker.search(release.name)
                        and not any(pattern.search(release.name) for pattern in exception_patterns)
                        and approved("retire", source)):
                    retire_group(source, "chapter_filter")
                    decisions.append(Decision("planned", "quarantine_chapter_release", source))
        if before and settings["rename_files_in_download_folders_toggle"]:
            for source, release in active().items():
                if source in selected and source not in blocked and download(source):
                    try:
                        target = release.parent / render_release(release, renaming)
                    except ValueError:
                        decisions.append(Decision("invalid_input", "invalid_rendered_filename", source))
                        blocked.add(source)
                        continue
                    relocate(source, target, rename=True)
        if before and settings["create_folders_for_items_in_download_folder_toggle"]:
            for source, release in active().items():
                if source not in selected or source in blocked or not download(source):
                    continue
                root = next(root for root in roots if root.contains(release.path))
                if release.parent != root.path or not release.display_series:
                    continue
                directory = release.parent / release.display_series
                if settings["move_lone_files_to_similar_folder"]:
                    folder_candidates: tuple[Path, ...] = tuple(sorted(
                        path for path in directories_set
                        if path.parent == root.path and not path.name.startswith(".")
                        and (path.name.strip().lower() == release.display_series.strip().lower()
                             or similar(clean_str(path.name, skip_bracket=True),
                                        clean_str(release.display_series, skip_bracket=True)) >= matching.required_similarity_score)
                    ))
                    if len(folder_candidates) > 1:
                        decisions.append(Decision("ambiguous", "multiple_similar_download_folders", source,
                                                  tuple(str(path) for path in folder_candidates)))
                        blocked.add(source)
                        continue
                    if folder_candidates:
                        directory = folder_candidates[0]
                if not _safe_component(directory.name):
                    decisions.append(Decision("invalid_input", "unsafe_series_folder_name", source))
                    blocked.add(source)
                    continue
                target = directory / release.name
                rename = settings["replace_series_name_in_file_name_with_similar_folder_name"] and directory.name != release.display_series
                if rename:
                    new_name = regex.sub(regex.escape(release.display_series), lambda _: directory.name,
                                         release.name, flags=regex.IGNORECASE, count=1)
                    target = directory / new_name
                relocate(source, target, rename=rename, reason="group_lone_release")
                directories_set.add(directory)

    if command == "duplicates" or before and settings["check_for_duplicate_volumes_toggle"]:
        duplicate_groups: dict[
            tuple[Path, str, str, str, ReleaseNumberKind, tuple[Decimal, ...], int | None, bool],
            list[Path],
        ] = {}
        for source, release in active().items():
            if source in blocked or release.number.kind == "missing":
                continue
            if command != "duplicates" and not download(source):
                continue
            duplicate_key = (release.parent, release.path.suffix.lower(), release.kind, release.canonical_series_key,
                             release.number.kind, release.number.values, release.number.part,
                             "filename:extra_marker" in release.provenance)
            duplicate_groups.setdefault(duplicate_key, []).append(source)
        for group in duplicate_groups.values():
            if len(group) < 2 or not selected.intersection(group):
                continue
            choice_path = min(group)
            keeper = choose_duplicate(group, key=choice_path)
            if keeper is None:
                blocked.update(group)
                continue
            losers = [source for source in group if source != keeper]
            permissions = [approved("retire", source) for source in losers]
            if not all(permissions):
                blocked.update(group)
                continue
            links.append(tuple(group))
            for source in losers:
                retire_group(source, "ranked_or_identical_duplicate")
                decisions.append(Decision("planned", "quarantine_duplicate_release", source, (str(keeper),)))

    if after and settings["check_for_existing_series_toggle"]:
        if not catalog.available:
            decisions.append(Decision("missing_dependency", catalog.reason or "library_unavailable"))
        else:
            folder_sources: dict[Path, list[Path]] = {}
            for source in sorted(selected):
                if source not in retired:
                    folder_sources.setdefault(locations[source].parent, []).append(source)
            ordering: list[Path] = []
            for folder in sorted(folder_sources, key=lambda path: (-len(path.parts), str(path))):
                folder_members = folder_sources[folder]
                if any(original[source].number.kind == "missing" for source in folder_members):
                    ordering.extend(sorted(folder_members, key=lambda source: locations[source].name))
                else:
                    ordering.extend(sorted(folder_members, key=lambda source: (
                        min(original[source].number.values), original[source].number.part or 0,
                        locations[source].name,
                    )))
            for source in ordering:
                if cancel is not None and cancel.is_set():
                    return RunResult((Decision("cancelled", "shutdown_requested", source),), releases=tuple(original.values()))
                view = active()
                if source not in view or source in blocked or not download(source):
                    continue
                incoming = view[source]
                if not incoming.display_series or incoming.number.kind == "missing":
                    decisions.append(Decision("no_match", "series_or_release_number_missing", source))
                    continue
                library_candidates: tuple[ReleaseSnapshot, ...] = tuple(
                    release for other, release in view.items()
                    if other != source and other not in blocked and library(other)
                )
                library_roots = tuple(root for root in roots if "library" in root.roles)
                match = match_series(incoming, LibrarySnapshot(library_candidates, library_roots), matching,
                                     cover_similarity=cover_similarity)
                if match.status != "matched" or match.selected is None:
                    decisions.append(Decision(cast(Status, match.status), match.reason, source,
                                              tuple(str(candidate.series_path) for candidate in match.candidates)))
                    if match.status != "no_match":
                        blocked.add(source)
                    continue
                destination_folder = match.selected.series_path
                decisions.append(Decision("observed", "series_match", source, (
                    f"evidence={match.reason}", f"series={destination_folder}",
                    *(f"score={score}" for score in match.scores),
                    f"required_similarity_score={matching.required_similarity_score}",
                    f"required_image_similarity_score={matching.required_image_similarity_score}",
                    f"required_matching_percentage={matching.required_matching_percentage}",
                )))
                if settings["rename_files_in_download_folders_toggle"] and renaming.restructure_when_renaming:
                    renamed = replace(incoming, display_series=destination_folder.name,
                                      canonical_series_key=clean_str(destination_folder.name))
                    target_name = render_release(renamed, renaming)
                else:
                    target_name = incoming.name
                target = destination_folder / target_name
                overlapping = [other for other, release in view.items() if other != source and other not in blocked
                               and release.parent == destination_folder and _overlapping(incoming, release)]
                if overlapping:
                    uncertain_identity = [other for other in overlapping
                                          if view[other].canonical_series_key != incoming.canonical_series_key
                                          and similar(clean_str(view[other].display_series), clean_str(incoming.display_series))
                                          < matching.required_similarity_score]
                    choice = answers.get(("duplicate", source))
                    allowed_choices = {"skip", str(source), *(str(other) for other in overlapping)}
                    if uncertain_identity and choice not in allowed_choices:
                        decisions.append(Decision("review_required", "duplicate_choice_required", source,
                                                  tuple(str(other) for other in (source, *overlapping))))
                        blocked.add(source)
                        continue
                    if any(not incoming.number.covers(view[other].number) for other in overlapping):
                        covering = [other for other in overlapping if view[other].number.covers(incoming.number)]
                        if len(covering) == 1 and _quality(incoming, view[covering[0]], ranking, source_artifacts)[0] == "not_upgrade":
                            if approved("retire", source):
                                retire_group(source, "covered_lower_rank_release")
                                decisions.append(Decision("planned", "quarantine_covered_release", source, (str(covering[0]),)))
                            continue
                        decisions.append(Decision("review_required", "partial_collection_overlap", source,
                                                  tuple(str(other) for other in overlapping)))
                        blocked.add(source)
                        continue
                    comparisons = [_quality(incoming, view[other], ranking, source_artifacts) for other in overlapping]
                    chosen = answers.get(("duplicate", source))
                    if chosen == "skip":
                        decisions.append(Decision("skipped", "duplicate_choice_declined", source))
                        blocked.add(source)
                        continue
                    if chosen is not None and Path(chosen) == source:
                        comparisons = [("upgrade", "explicit_duplicate_choice") for _ in overlapping]
                    if ("identifier" in match.reason and any(reason != "identical_content" for _, reason in comparisons)
                            and chosen not in allowed_choices - {"skip"}):
                        decisions.append(Decision("review_required", "duplicate_choice_required", source,
                                                  tuple(str(other) for other in (source, *overlapping))))
                        blocked.add(source)
                        continue
                    if all(status == "upgrade" for status, _ in comparisons):
                        approvals = [approved("retire", other) for other in overlapping]
                        if target_name != incoming.name:
                            approvals.append(approved("rename", source, target))
                        if not all(approvals):
                            blocked.add(source)
                            continue
                        links.append((source, *overlapping))
                        for other in overlapping:
                            retire_group(other, "complete_collection_or_release_upgrade")
                            replacements.add(locations[other])
                            replacements.update(locations[image] for image in sidecars[other])
                        relocate(source, target, reason="complete_collection_or_release_upgrade")
                        decisions.append(Decision("planned", "complete_collection_or_release_upgrade", source,
                                                  tuple(str(other) for other in overlapping)))
                        continue
                    if (all(status == "not_upgrade" for status, _ in comparisons)
                            and (len(overlapping) == 1 and _same_domain(incoming, view[overlapping[0]])
                                 or any(view[other].number.covers(incoming.number) for other in overlapping))):
                        if approved("retire", source):
                            retire_group(source, "not_upgrade")
                            decisions.append(Decision("planned", "quarantine_nonupgrade", source,
                                                      tuple(str(other) for other in overlapping)))
                        continue
                    if chosen is not None and Path(chosen) in overlapping and len(overlapping) == 1:
                        if approved("retire", source):
                            retire_group(source, "explicit_duplicate_choice")
                        continue
                    decisions.append(Decision("review_required", "duplicate_choice_required", source,
                                              tuple(str(other) for other in (source, *overlapping))))
                    blocked.add(source)
                    continue
                relocate(source, target, rename=target_name != incoming.name, reason="existing_series_new_release")

    if after and settings["rename_dirs_in_download_folder_toggle"]:
        view = active()
        folders = sorted({release.parent for source, release in view.items() if source in selected and download(source)},
                         key=lambda path: (-len(path.parts), str(path)))
        for folder in folders:
            if any(folder == root.path for root in roots):
                continue
            folder_releases: list[ReleaseSnapshot] = sorted(
                (release for release in active().values() if release.parent == folder), key=lambda release: release.name
            )
            agreement = folder_series_agreement(folder_releases, matching.classification)
            name = ""
            if agreement.status == "matched" and folder_releases and folder_name_is_rename_candidate(
                folder_releases[0].display_series, folder.name, matching.classification
            ):
                name = folder_releases[0].display_series
            elif (agreement.status != "matched"
                  and any(root.path == folder.parent and "download" in root.roles for root in roots)):
                name = _folder_name(folder.name, parsing)
            if name:
                rename_folder(folder, folder.parent / name)
    if after and settings["move_series_to_correct_library_toggle"]:
        view = active()
        moved = {source for source in selected if source not in retired and source not in blocked and library(source)
                 and (command == "organize" or locations[source] != _source_path(original[source]))}
        for folder in sorted({view[source].parent for source in moved}):
            routing_releases: tuple[ReleaseSnapshot, ...] = tuple(
                release for release in active().values() if release.parent == folder
            )
            classification = matching.classification
            chapter_share = sum(release.kind == "chapter" for release in routing_releases) / len(routing_releases)
            volume_share = sum(release.kind == "volume" for release in routing_releases) / len(routing_releases)
            if chapter_share < classification.chapter_threshold and volume_share < classification.volume_threshold:
                decisions.append(Decision("no_match", "mixed_series_not_routable", folder))
                continue
            route = route_releases_to_root(routing_releases, tuple(root for root in roots if "library" in root.roles), classification)
            if route.status != "routed" or route.selected is None:
                decisions.append(Decision(cast(Status, route.status), route.reason, folder,
                                          tuple(str(root.path) for root in route.candidates)))
                continue
            if route.selected.contains(folder):
                continue
            rename_folder(folder, route.selected.path / folder.name, reason="directory_route")

    output_sources: dict[Path, list[Path]] = {}
    for source, target in locations.items():
        if source not in retired:
            output_sources.setdefault(target, []).append(source)
    for target, sources in output_sources.items():
        if len(sources) < 2 or len({source_artifacts[source].digest if source in source_artifacts
                                   else observed_files[source].sha256 for source in sources}) != 1:
            continue
        keeper = target if target in sources else min(sources)
        losers = [source for source in sources if source != keeper]
        art_keys = {
            tuple(sorted((image.suffix.lower(), observed_files[image].sha256) for image in sidecars[source]))
            for source in sources if source in original
        }
        if len(art_keys) > 1:
            decisions.append(Decision("review_required", "identical_book_different_artwork", target,
                                      tuple(str(source) for source in sources)))
            continue
        permissions = [approved("retire", source) for source in losers if source not in retired]
        if not all(permissions):
            continue
        for source in losers:
            if source in retired:
                continue
            if source in original:
                retire_group(source, "identical_destination_merge")
            else:
                retired[source] = "identical_destination_merge"
            decisions.append(Decision("planned", "quarantine_identical_destination", source, (str(keeper),)))

    rebound: dict[Path, tuple[FileFingerprint, ArtifactSpec]] = {}
    for old_target, value in generated.items():
        book_owner = generated_book_images.get(old_target)
        series_owner = generated_series_images.get(old_target)
        owner = book_owner if book_owner is not None else series_owner
        if owner is not None and owner in retired:
            continue
        target = (locations[book_owner].with_suffix(old_target.suffix) if book_owner is not None
                  else locations[series_owner].parent / old_target.name if series_owner is not None else old_target)
        if target in rebound and rebound[target] != value:
            return RunResult((Decision("conflict", "multiple_generated_artwork_outputs", target),), releases=tuple(original.values()))
        identical = False
        if book_owner is not None:
            for image in sidecars.get(book_owner, ()):
                if image not in retired and locations[image] == target:
                    if image in replacements:
                        retired[image] = "superseded_book_artwork"
                    elif observed_files[image].sha256 != value[1].digest:
                        return RunResult((Decision("review_required", "custom_book_artwork", image),), releases=tuple(original.values()))
                    else:
                        identical = True
            supplied_artwork[book_owner] = (value[1], _IMAGE_DETAILS[target.suffix.lower()][0],
                                            _IMAGE_DETAILS[target.suffix.lower()][1])
        if not identical:
            rebound[target] = value
    generated = rebound

    view = active()
    series_roles = ("cover", "poster") if series_policy is None else series_policy.roles
    changed_books = {source for source in original if source in retired
                     or (source in operation_reasons or source in source_artifacts)
                     and locations[source] != _source_path(original[source])}
    affected_folders = {view[source].parent for source in changed_books if source in view}
    affected_folders.update(locations[source].parent for source in changed_books if source in retired)
    artwork_errors: list[Decision] = []
    for folder in sorted(affected_folders):
        existing_paths = [source for source, path in locations.items() if source not in retired
                          and path.parent == folder and path.stem in series_roles and path.suffix.lower() in IMAGE_SUFFIXES]
        moving_owned_art = [image for image, owner in owners.items() if owner in view
                            and view[owner].parent == folder and locations[image].parent != folder
                            and owner in changed_books]
        if not existing_paths and not moving_owned_art:
            continue
        if existing_paths and not moving_owned_art:
            origin_folders = {source.parent for source in existing_paths}
            member_sources = [source for source, release in view.items() if release.parent == folder]
            retired_owners = any(source in retired and source.parent in origin_folders for source in original)
            if (not retired_owners and member_sources
                    and all(source.parent in origin_folders for source in member_sources)):
                continue
        members_by_source = {source: release for source, release in view.items() if release.parent == folder}
        if not members_by_source:
            for image in existing_paths:
                if owners.get(image) in retired:
                    retired[image] = "retired_owned_series_artwork"
            continue
        observation_images: list[BookArtworkObservation] = []
        for source, release in members_by_source.items():
            images: list[ArtworkImageObservation] = []
            if source in supplied_artwork:
                artifact, suffix, media_type = supplied_artwork[source]
                images.append(ArtworkImageObservation(release.path.with_suffix(suffix), artifact.digest, suffix,
                                                       media_type, artifact, True, release.path))
            else:
                paths = [image for image in sidecars[source] if image not in retired]
                if not paths:
                    paths = [image for image, owner in owners.items() if owner == source and image not in retired]
                for image in paths:
                    suffix, media_type = _IMAGE_DETAILS[image.suffix.lower()]
                    observed = observed_files[image]
                    images.append(ArtworkImageObservation(release.path.with_suffix(suffix), observed.sha256,
                                                           suffix, media_type, ArtifactSpec(source=observed, media_type=media_type),
                                                           owners.get(image) == source, release.path))
            observation_images.append(BookArtworkObservation(release.path, tuple(images),
                                                              "classification:one_shot" in release.provenance))
        existing = tuple(ArtworkImageObservation(
            locations[image], observed_files[image].sha256, _IMAGE_DETAILS[image.suffix.lower()][0],
            _IMAGE_DETAILS[image.suffix.lower()][1], ArtifactSpec(source=observed_files[image]),
            image in owners, view[owners[image]].path if image in owners and owners[image] in view else owners.get(image),
            observed_files[image].mtime_ns,
        ) for image in existing_paths)
        policy = series_policy or SeriesCoverPolicy()
        observation = SeriesArtworkObservation(folder, tuple(observation_images), existing,
                                                tuple(path for path in directories_set if path.parent == folder
                                                      and not path.name.startswith(".kce-")),
                                                any(folder == root.path for root in roots))
        strategy = plan_series_artwork(tuple(members_by_source.values()), policy=replace(
            policy, observation=observation, create_roles=tuple(role for role in policy.roles
                                                               if any(locations[image].stem == role for image in moving_owned_art)),
        ))
        if strategy.reason == "selected_book_artwork_absent" and strategy.selected_release is not None:
            missing_artwork_source = _source_path(strategy.selected_release)
            artwork_errors.append(Decision("review_required", "replacement_book_artwork_required", missing_artwork_source,
                                           (str(missing_artwork_source),)))
            continue
        if strategy.status in {"ambiguous", "conflict", "invalid_input"}:
            artwork_errors.append(Decision("review_required" if strategy.status == "ambiguous" else cast(Status, strategy.status),
                                           strategy.reason, folder, tuple(str(path) for path in strategy.candidates)))
            continue
        for association in strategy.associations:
            if association.action == "keep":
                continue
            association_source = _source_path(strategy.selected_release) if strategy.selected_release is not None else None
            assert association_source is not None
            association_artifact = association.source_image.artifact
            assert association_artifact is not None
            source_observation = observed_files[association_source]
            generated[association.destination] = (source_observation, association_artifact)
            for image in existing_paths:
                if locations[image] == association.destination:
                    replacements.add(image)
                    retired[image] = "replaced_owned_series_artwork"
                elif locations[image] in association.retire_after_commit:
                    retired[image] = "superseded_owned_series_artwork"
            for image in moving_owned_art:
                if owners[image] == association_source and locations[image].stem == association.role:
                    retired[image] = "moved_owned_series_artwork"
    if artwork_errors:
        return RunResult(tuple(decision for decision in decisions if decision.status != "planned") + tuple(artwork_errors),
                         releases=tuple(original.values()))

    destinations = {source: target for source, target in locations.items()
                    if source in operation_reasons or source in source_artifacts}
    compiled = build_file_plan(observed_files, destinations, retired, roots=roots, directories=directories,
                               replacement_paths=frozenset(replacements), publications=generated,
                               source_artifacts=source_artifacts, cleanup_sources=cleanup_sources,
                               links=links, reasons=operation_reasons,
                               policy_version=policy_version, reason="duplicate_cleanup" if command == "duplicates" else "organization")
    if not compiled.plans and compiled.exit_code:
        return replace(compiled, decisions=tuple(decision for decision in decisions if decision.status != "planned") + compiled.decisions,
                       releases=tuple(original.values()))
    skipped = len({decision.path for decision in decisions if decision.status in {"skipped", "no_match"}})
    compiler_decisions = compiled.decisions if compiled.plans or not decisions else ()
    return replace(compiled, decisions=(*decisions, *compiler_decisions), releases=tuple(view.values()),
                   skipped=skipped if decisions else compiled.skipped)


def _observation_error(error: OSError, path: Path) -> Decision:
    if isinstance(error, FilesystemError):
        status: Status = "conflict" if error.reason in {"symlink_traversal", "not_regular_file"} else "stale_plan" if error.reason == "source_changed" else "invalid_input"
        return Decision(status, error.reason, error.path)
    if isinstance(error, PermissionError):
        return Decision("permission_denied", "organizer_observation_permission_denied", path)
    if isinstance(error, FileNotFoundError):
        return Decision("stale_plan", "organizer_input_disappeared", path)
    return Decision("failed", "organizer_observation_failed", path)


def observe_organization_files(
    roots: tuple[LibraryRoot, ...], ignored: tuple[str, ...], cancel: Event, *,
    excluded_paths: tuple[Path, ...] = (),
) -> tuple[dict[Path, FileFingerprint], tuple[Path, ...]]:
    files: dict[Path, FileFingerprint] = {}
    directories: list[Path] = []
    for root in roots:
        pending = [root.path]
        while pending:
            if cancel.is_set():
                raise WorkerError("shutdown_requested", "cancelled")
            directory = checked_path(pending.pop(), root.path)
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise FilesystemError("root_not_directory", directory)
            directories.append(directory)
            for path in sorted(directory.iterdir()):
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise FilesystemError("symlink_traversal", path)
                if any(path == excluded or path.is_relative_to(excluded) for excluded in excluded_paths):
                    if stat.S_ISDIR(mode):
                        directories.append(path)
                    continue
                if stat.S_ISDIR(mode):
                    if path.name.startswith(".kce-") or path.name in ignored:
                        directories.append(path)
                    else:
                        pending.append(path)
                elif stat.S_ISREG(mode):
                    files[path] = fingerprint(path, root.path)
                else:
                    raise FilesystemError("not_regular_file", path)
    return files, tuple(directories)


def _manual_answers(result: RunResult, choices: dict[tuple[str, Path], str], cancel: Event) -> bool:
    descriptor = sys.stdin.fileno()
    pending = bytearray()
    changed = False
    for decision in result.decisions:
        if decision.path is None or decision.reason not in {"manual_rename_required", "manual_retire_required", "duplicate_choice_required"}:
            continue
        action = "rename" if decision.reason == "manual_rename_required" else "retire" if decision.reason == "manual_retire_required" else "duplicate"
        key = (f"rename:{decision.candidates[0]}" if action == "rename" else action, decision.path)
        if action == "duplicate":
            sys.stderr.write(f"Choose the retained release for {decision.path}.\n")
            for index, candidate in enumerate(decision.candidates, 1):
                sys.stderr.write(f"  {index}. {candidate}\n")
            sys.stderr.write("Enter a candidate number or 'skip'. ")
        else:
            target = decision.candidates[0] if decision.candidates else str(decision.path)
            sys.stderr.write(f"{'Rename' if action == 'rename' else 'Quarantine'} {decision.path}"
                             + (f" to {target}" if action == "rename" else "") + "? [yes/no] ")
        sys.stderr.flush()
        while True:
            if cancel.is_set():
                raise WorkerError("shutdown_requested", "cancelled")
            newline = pending.find(b"\n")
            if newline >= 0:
                try:
                    answer = pending[:newline].decode("utf-8").strip().lower()
                except UnicodeDecodeError:
                    raise WorkerError("invalid_interactive_input", "invalid_input") from None
                del pending[:newline + 1]
                break
            if len(pending) > 4096:
                raise WorkerError("interactive_input_byte_limit", "invalid_input")
            ready, _, _ = select.select((descriptor,), (), (), 0.2)
            if not ready:
                continue
            chunk = os.read(descriptor, min(4096, 4097 - len(pending)))
            if not chunk:
                raise WorkerError("interactive_input_closed", "cancelled")
            pending.extend(chunk)
        if action == "duplicate" and answer.isdecimal() and 1 <= int(answer) <= len(decision.candidates):
            choices[key] = decision.candidates[int(answer) - 1]
        elif action != "duplicate" and answer in {"yes", "y"}:
            choices[key] = decision.candidates[0] if action == "rename" else "yes"
        elif answer in {"no", "n", "skip"}:
            choices[key] = "skip"
        else:
            continue
        changed = True
    return changed


def prepare_organization_group(
    releases: Sequence[ReleaseSnapshot], catalog: LibrarySnapshot, *, roots: tuple[LibraryRoot, ...],
    parsing: ParsingPolicy, renaming: RenamePolicy, matching: MatchingPolicy, ranking: RankingPolicy,
    options: Mapping[str, bool], state_dir: Path, policy_version: str,
    cancel: Event, interactive: bool = False, command: str = "organize", stage: str = "all",
    unacceptable_keywords: tuple[str, ...] = (), ignored_folder_names: tuple[str, ...] = (),
    cover_similarity: CoverSimilarity | None = None, cover_policy: CoverPolicy | None = None,
    series_policy: SeriesCoverPolicy | None = None,
    previous_plans: Sequence[OperationPlan] = (),
    scope_sources: frozenset[Path] | None = None,
    files: Mapping[Path, FileFingerprint],
    directories: Sequence[Path],
    before: Sequence[ReleaseSnapshot] = (),
) -> RunResult:
    """Prepare one connected group, resolving owned artwork without publishing it."""
    roots = normalize_library_roots(roots)
    if cancel.is_set():
        return RunResult((Decision("cancelled", "shutdown_requested"),))
    if not roots:
        return RunResult((Decision("invalid_input", "no_selected_roots"),))
    try:
        files = dict(files)
        directories = tuple(directories)
        affected = set(scope_sources or (_source_path(release) for release in releases))
        if _after_matching(command, stage) and options.get("check_for_existing_series_toggle", False):
            for release in releases:
                if not any("download" in root.roles and root.contains(release.path) for root in roots):
                    continue
                match = match_series(release, catalog, matching, cover_similarity=cover_similarity)
                if match.status in {"matched", "ambiguous"}:
                    candidate_series_paths = {candidate.series_path for candidate in match.candidates}
                    affected.update(_source_path(candidate) for candidate in catalog.releases
                                    if candidate.parent in candidate_series_paths)
        affected_parents = {source.parent for source in affected}
        affected.update(path for path in files if path.parent in affected_parents
                        and path.suffix.lower() in IMAGE_SUFFIXES)
        for source in sorted(affected):
            if cancel.is_set():
                return RunResult((Decision("cancelled", "shutdown_requested", source),))
            before_source = files.get(source)
            if before_source is None:
                continue
            current_source = fingerprint(source, before_source.root)
            if current_source != before_source:
                return RunResult((Decision("stale_plan", "affected_source_fingerprint_changed", source),))
        snapshots: dict[Path, ReleaseSnapshot] = {}
        for release in (*catalog.releases, *releases):
            source = _source_path(release)
            current = files.get(source)
            if current is None:
                return RunResult((Decision("stale_plan", "release_source_missing", source),))
            source_before = release.observation.fingerprint
            if source_before is not None and source_before != current:
                return RunResult((Decision("stale_plan", "release_source_fingerprint_changed", source),))
            snapshots[source] = replace(release, observation=replace(release.observation, fingerprint=current))
        selected = tuple(snapshots[_source_path(release)] for release in releases)
        catalog = replace(catalog, releases=tuple(snapshots[_source_path(release)] for release in catalog.releases), roots=roots)
        provenance_books = dict(snapshots)
        for release in before:
            source = _source_path(release)
            observation = release.observation.fingerprint
            if observation is None or files.get(source) != observation:
                return RunResult((Decision("stale_plan", "provenance_source_fingerprint_changed", source),))
            provenance_books.setdefault(source, release)
        store = StateStore(state_dir)
        from .artwork_workflow import _image_provenance
        owners: dict[Path, Path] = {}
        roles = ("cover", "poster") if series_policy is None else series_policy.roles
        for path, observation in files.items():
            if path.suffix.lower() in IMAGE_SUFFIXES and path.stem in roles:
                owner, owner_source = _image_provenance(observation, provenance_books, store)
                if owner is not None and owner_source is not None and files.get(owner) == owner_source:
                    owners[path] = owner
        input_artifacts = {operation.source.path: operation.artifact
                           for plan in previous_plans for operation in plan.operations
                           if operation.kind == "publish" and operation.source is not None
                           and operation.artifact is not None and operation.destination.suffix.lower() in BOOK_SUFFIXES}
        if cover_similarity is None and cover_policy is not None:
            from .library_workflow import make_cover_similarity
            cover_similarity = make_cover_similarity(
                plans=previous_plans, policy=cover_policy, matching=matching, cancel=cancel,
            )
        artwork: dict[Path, tuple[ArtifactSpec, str, str]] = {}
        choices: dict[tuple[str, Path], str] = {}
        while True:
            planned = plan_organization(
                selected, catalog, roots=roots, parsing=parsing, renaming=renaming, matching=matching,
                ranking=ranking, options=options, files=files, directories=directories, policy_version=policy_version,
                command=command, stage=stage, unacceptable_keywords=unacceptable_keywords,
                ignored_folder_names=ignored_folder_names, cover_similarity=cover_similarity, series_policy=series_policy,
                series_art_owners=owners, artwork=artwork, choices=choices, cancel=cancel,
                previous_plans=previous_plans, scope_sources=scope_sources,
            )
            required = tuple(decision.path for decision in planned.decisions
                             if decision.reason == "replacement_book_artwork_required" and decision.path not in artwork)
            if required and cover_policy is not None:
                from ..infrastructure.images import FileOutputPolicy
                from .extract_covers import extract_cover
                for artwork_source in required:
                    assert artwork_source is not None
                    observation = files[artwork_source]
                    artifact = input_artifacts.get(artwork_source)
                    if artifact is not None and artifact.source is not None:
                        observation = artifact.source
                    release = snapshots[artwork_source]
                    extracted = extract_cover(observation.path, root=observation.root, policy=cover_policy,
                                              output=FileOutputPolicy(), release_kind=release.kind, cancel=cancel)
                    if extracted.status != "valid":
                        return RunResult((Decision(cast(Status, extracted.status if extracted.status != "absent" else "review_required"),
                                                   extracted.reason, artwork_source),), releases=tuple(snapshots.values()))
                    assert extracted.artifact is not None and extracted.suffix is not None and extracted.media_type is not None
                    artwork[artwork_source] = (extracted.artifact, extracted.suffix, extracted.media_type)
                continue
            if interactive and sys.stdin.isatty() and sys.stderr.isatty() and _manual_answers(planned, choices, cancel):
                continue
            break
        if not planned.plans:
            return planned
        if cover_policy is not None:
            from ..infrastructure.workers import run_worker
            for plan in planned.plans:
                for operation in plan.operations:
                    artifact = operation.artifact
                    if (operation.kind == "publish" and operation.reason == "owned_series_artwork"
                            and artifact is not None and artifact.source is not None):
                        observed = artifact.source
                        response = run_worker("inspect", {"artwork_image": True, "fingerprint": observed.as_dict(),
                                                      "max_bytes": cover_policy.archive_policy.cover_member_max_bytes,
                                                      "max_pixels": cover_policy.archive_policy.image_max_pixels},
                                                      timeout=cover_policy.archive_policy.conversion_timeout_seconds,
                                                      max_output_bytes=64 * 1024, cancel=cancel)
                        if response.get("suffix") != operation.destination.suffix.lower():
                            return RunResult((Decision("invalid_input", "artwork_suffix_mismatch", observed.path),))
        elif any(operation.kind == "publish" and operation.reason == "owned_series_artwork"
                 and operation.artifact is not None and operation.artifact.source is not None
                 for plan in planned.plans for operation in plan.operations):
            return RunResult((Decision("missing_dependency", "cover_policy_required_for_artwork_validation"),), releases=planned.releases)
        return planned
    except WorkerError as error:
        status = cast(Status, error.status)
        return RunResult((Decision(status, error.reason),), failed=int(status in {"failed", "permission_denied", "publication_failed"}))
    except StateStoreError:
        return RunResult((Decision("review_required", "organizer_artwork_provenance_unavailable"),))
    except OSError as error:
        decision = _observation_error(error, roots[0].path)
        return RunResult((decision,), failed=int(decision.status in {"failed", "permission_denied"}))
    except (TypeError, ValueError):
        return RunResult((Decision("invalid_input", "invalid_organizer_observation_or_policy"),))


def apply_organization(
    prepared: RunResult, *, roots: tuple[LibraryRoot, ...], state_dir: Path,
    authorization: RunAuthorization, cancel: Event, max_stage_bytes: int = 8_589_934_592,
) -> RunResult:
    """Settle prepared groups; the pipeline refreshes committed observations."""
    if not prepared.plans:
        return prepared
    results: list[RunResult] = []
    for plan in prepared.plans:
        executor_roots = tuple(dict.fromkeys((*tuple(root.path for root in roots),
                                             *tuple(permission.path for permission in plan.roots if not permission.write))))
        result = TransactionExecutor(state_dir, executor_roots, cancel=cancel, max_stage_bytes=max_stage_bytes).apply(
            plan, authorization=authorization,
        )
        results.append(result)
        if result.exit_code:
            break
    return RunResult(
        (*prepared.decisions, *(decision for result in results for decision in result.decisions)),
        committed=sum(result.committed for result in results),
        skipped=prepared.skipped + sum(result.skipped for result in results),
        failed=sum(result.failed for result in results), releases=prepared.releases,
        plan_reports=tuple(PlanReport(plan) for plan in prepared.plans),
    )


def organize_group(
    releases: Sequence[ReleaseSnapshot], catalog: LibrarySnapshot, *, roots: tuple[LibraryRoot, ...],
    parsing: ParsingPolicy, renaming: RenamePolicy, matching: MatchingPolicy, ranking: RankingPolicy,
    options: Mapping[str, bool], state_dir: Path, policy_version: str, authorization: RunAuthorization,
    cancel: Event, files: Mapping[Path, FileFingerprint], directories: Sequence[Path],
    interactive: bool = False, command: str = "organize", stage: str = "all",
    unacceptable_keywords: tuple[str, ...] = (), ignored_folder_names: tuple[str, ...] = (),
    cover_similarity: CoverSimilarity | None = None, cover_policy: CoverPolicy | None = None,
    series_policy: SeriesCoverPolicy | None = None, previous_plans: Sequence[OperationPlan] = (),
    scope_sources: frozenset[Path] | None = None,
) -> RunResult:
    prepared = prepare_organization_group(
        releases, catalog, roots=roots, parsing=parsing, renaming=renaming, matching=matching,
        ranking=ranking, options=options, state_dir=state_dir, policy_version=policy_version, cancel=cancel,
        files=files, directories=directories, interactive=interactive, command=command, stage=stage,
        unacceptable_keywords=unacceptable_keywords, ignored_folder_names=ignored_folder_names,
        cover_similarity=cover_similarity, cover_policy=cover_policy, series_policy=series_policy,
        previous_plans=previous_plans, scope_sources=scope_sources,
    )
    return apply_organization(prepared, roots=roots, state_dir=state_dir, authorization=authorization,
                              cancel=cancel, max_stage_bytes=cover_policy.archive_policy.conversion_scratch_max_bytes
                              if cover_policy is not None else 8_589_934_592)


__all__ = ["OPTIONS", "STAGES", "organize_group", "prepare_organization_group",
           "apply_organization", "plan_organization", "observe_organization_files", "organization_groups"]


def organization_groups(
    releases: Sequence[ReleaseSnapshot], catalog: LibrarySnapshot, *,
    roots: tuple[LibraryRoot, ...], matching: MatchingPolicy,
    cover_similarity: CoverSimilarity | None = None,
) -> tuple[tuple[ReleaseSnapshot, ...], ...]:
    """Keep one source series and shared destination evidence in one job."""
    values = {_source_path(release): release for release in releases}
    parent = {source: source for source in values}
    root_paths = {root.path for root in roots}

    def leader(source: Path) -> Path:
        while parent[source] != source:
            parent[source] = parent[parent[source]]
            source = parent[source]
        return source

    indexes: dict[tuple[object, ...], Path] = {}
    for source, release in sorted(values.items()):
        keys: list[tuple[object, ...]] = []
        if release.parent not in root_paths:
            keys.append(("source_directory", release.parent))
        if release.canonical_series_key:
            medium = "novel" if release.path.suffix.lower() in matching.classification.novel_extensions else "comic"
            keys.append(("series", release.canonical_series_key, release.kind, medium))
        if catalog.available and any("download" in root.roles and root.contains(release.path) for root in roots):
            candidates = tuple(item for item in catalog.releases if _source_path(item) != source
                               and any("library" in root.roles and root.contains(item.path) for root in roots))
            decision = match_series(release, LibrarySnapshot(candidates, roots), matching, cover_similarity=cover_similarity)
            if decision.status == "matched" and decision.selected is not None:
                keys.append(("source_directory", decision.selected.series_path))
        for key in keys:
            previous = indexes.setdefault(key, source)
            parent[leader(source)] = leader(previous)
    groups: dict[Path, list[ReleaseSnapshot]] = {}
    for source in sorted(values):
        groups.setdefault(leader(source), []).append(values[source])
    return tuple(tuple(group) for group in groups.values())



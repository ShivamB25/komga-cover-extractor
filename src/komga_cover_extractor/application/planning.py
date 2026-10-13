"""Compile a virtual file view into one recoverable publication group."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ..domain.decisions import Decision, RunResult
from ..domain.libraries import LibraryRoot
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationPlan,
    RootPermission,
)
from ..domain.releases import ReleaseSnapshot
from ..parsing.filenames import ParsingPolicy, parse_release


def project_release(
    release: ReleaseSnapshot, destination: Path, *, parsing: ParsingPolicy,
    roots: Sequence[LibraryRoot], siblings: tuple[Path, ...] | None = None,
) -> ReleaseSnapshot:
    """Project a name and root without inventing an observation of new bytes.

    The fingerprint still points to the observed source. Effect owners read
    that path until publication and take a fresh observation after a commit.
    """
    if destination == release.path and siblings is None:
        return release
    root = next((item for item in roots if item.contains(destination)), None)
    role = "unknown" if root is None else (
        "both" if {"library", "download"} <= set(root.roles)
        else "download" if "download" in root.roles else "library"
    )
    observation = replace(
        release.observation, path=destination,
        siblings=release.observation.siblings if siblings is None else siblings,
        root_role=role, at_configured_root=root is not None and destination.parent == root.path,
    )
    if destination.name == release.name:
        return replace(release, observation=observation,
                       provenance=tuple(dict.fromkeys((*release.provenance, "planned_path"))))
    parsed = parse_release(observation, parsing)
    return replace(parsed, provenance=tuple(dict.fromkeys((*release.provenance, *parsed.provenance, "planned_path"))))


def build_file_plan(
    files: Mapping[Path, FileFingerprint], destinations: Mapping[Path, Path],
    retirements: Mapping[Path, str], *, roots: Sequence[LibraryRoot],
    directories: Sequence[Path], replacement_paths: frozenset[Path] = frozenset(),
    publications: Mapping[Path, tuple[FileFingerprint, ArtifactSpec]] | None = None,
    source_artifacts: Mapping[Path, ArtifactSpec] | None = None,
    cleanup_sources: Mapping[Path, str] | None = None,
    links: Sequence[Sequence[Path]] = (), reasons: Mapping[Path, str] | None = None,
    policy_version: str, reason: str = "organization",
) -> RunResult:
    """Compile independent book/series groups, joining only shared obligations."""
    generated = {} if publications is None else dict(publications)
    transformed = {} if source_artifacts is None else dict(source_artifacts)
    cleanup = {} if cleanup_sources is None else dict(cleanup_sources)
    root_paths = {root.path for root in roots}
    changed = {source for source in files if destinations.get(source, source) != source}
    changed.update(retirements)
    changed.update(cleanup)
    changed.update(transformed)
    changed.update(source.path for source, _ in generated.values())
    outputs: dict[Path, list[Path]] = {}
    for source in files:
        if source not in retirements:
            outputs.setdefault(destinations.get(source, source), []).append(source)
    conflicts = tuple(Decision("conflict", "multiple_planned_outputs", target,
                               tuple(str(source) for source in sorted(sources)))
                      for target, sources in outputs.items() if len(sources) > 1)
    if conflicts:
        return RunResult(conflicts)
    if not changed:
        return RunResult((Decision("skipped", "organization_unchanged"),), skipped=1)
    parent = {source: source for source in changed}

    def leader(source: Path) -> Path:
        while parent[source] != source:
            parent[source] = parent[parent[source]]
            source = parent[source]
        return source

    def join(sources: Sequence[Path]) -> None:
        members = [source for source in sources if source in parent]
        if members:
            first = leader(members[0])
            for source in members[1:]:
                parent[leader(source)] = first

    for relationship in links:
        join(relationship)
    folders: dict[Path, list[Path]] = {}
    stems: dict[tuple[Path, str], list[Path]] = {}
    for source in changed:
        target = destinations.get(source, source)
        for path in (source, target):
            if path.parent not in root_paths:
                folders.setdefault(path.parent, []).append(source)
            stems.setdefault((path.parent, path.stem), []).append(source)
        if target in parent:
            join((source, target))
    for related_sources in (*folders.values(), *stems.values()):
        join(related_sources)
    for target, (publication_source, _) in generated.items():
        if target in parent:
            join((publication_source.path, target))
        if target.parent in folders:
            join((publication_source.path, *folders[target.parent]))
    groups: dict[Path, set[Path]] = {}
    for source in sorted(changed):
        groups.setdefault(leader(source), set()).add(source)
    plans: list[OperationPlan] = []
    decisions: list[Decision] = []
    current_files = dict(files)
    current_directories = set(directories)
    occupied: set[Path] = set()
    for members in groups.values():
        group_locations = {source: destinations[source] for source in members if source in destinations}
        group_retirements = {source: retirements[source] for source in members if source in retirements}
        group_publications = {target: value for target, value in generated.items() if value[0].path in members}
        result = _compile_file_group(
            current_files, group_locations, group_retirements, roots=roots,
            directories=tuple(current_directories), replacement_paths=replacement_paths,
            publications=group_publications,
            source_artifacts={source: artifact for source, artifact in transformed.items() if source in members},
            cleanup_sources={source: value for source, value in cleanup.items() if source in members},
            reasons=reasons, occupied_paths=frozenset(occupied), policy_version=policy_version, reason=reason,
        )
        if result.exit_code:
            return result
        plans.extend(result.plans)
        decisions.extend(result.decisions)
        for plan in result.plans:
            published_targets = {operation.destination for operation in plan.operations if operation.kind in {"move", "publish"}}
            for operation in plan.operations:
                if operation.kind == "mkdir":
                    current_directories.add(operation.destination)
                elif operation.kind == "rmdir":
                    current_directories.discard(operation.destination)
                elif operation.kind in {"move", "publish"}:
                    occupied.add(operation.destination)
            for source in members:
                current_files.pop(source, None)
                if source not in published_targets:
                    occupied.discard(source)
            # Original observations remain the only byte identities. Published
            # targets are represented as occupancy, never synthetic fingerprints.
            for source in members:
                if source in files and source not in group_retirements:
                    target = destinations.get(source, source)
                    if target == source:
                        current_files[source] = files[source]
    return RunResult(tuple(decisions), plans=tuple(plans))


def _compile_file_group(
    files: Mapping[Path, FileFingerprint], destinations: Mapping[Path, Path],
    retirements: Mapping[Path, str], *, roots: Sequence[LibraryRoot],
    directories: Sequence[Path], replacement_paths: frozenset[Path] = frozenset(),
    publications: Mapping[Path, tuple[FileFingerprint, ArtifactSpec]] | None = None,
    source_artifacts: Mapping[Path, ArtifactSpec] | None = None,
    cleanup_sources: Mapping[Path, str] | None = None,
    reasons: Mapping[Path, str] | None = None,
    occupied_paths: frozenset[Path] = frozenset(),
    policy_version: str, reason: str = "organization",
) -> RunResult:
    """Collapse renames and moves to original-source to final-destination edges.

    ``files`` and ``directories`` are complete observations of affected scopes.
    Retirements happen after every publication in this group. Cycles use the
    executor's staged source and observed replacement contract.
    """
    observed = dict(files)
    locations = {source: destinations.get(source, source) for source in observed}
    generated = {} if publications is None else dict(publications)
    transformed = {} if source_artifacts is None else {
        source: artifact for source, artifact in source_artifacts.items() if source not in retirements
    }
    cleanup = {} if cleanup_sources is None else dict(cleanup_sources)
    cleanup.update(retirements)
    for source in transformed:
        cleanup.setdefault(source, "converted_source")
    root_values = tuple(roots)
    directory_values = frozenset(directories)
    decisions: list[Decision] = []
    for source, target in locations.items():
        if source in retirements:
            continue
        if not any(root.contains(target) for root in root_values):
            decisions.append(Decision("permission_denied", "destination_outside_selected_roots", source, (str(target),)))
    owners: dict[Path, list[Path]] = {}
    for source, target in locations.items():
        if source not in retirements:
            owners.setdefault(target, []).append(source)
    for target, sources in owners.items():
        if len(sources) > 1:
            decisions.append(Decision("conflict", "multiple_planned_outputs", target,
                                      tuple(str(source) for source in sorted(sources))))
    for target in generated:
        if not any(root.contains(target) for root in root_values):
            decisions.append(Decision("permission_denied", "destination_outside_selected_roots", target))
        if target in owners and (owners[target] != [target] or target not in replacement_paths):
            decisions.append(Decision("conflict", "artwork_output_collision", target,
                                      tuple(str(source) for source in owners[target])))
    if decisions:
        return RunResult(tuple(decisions))

    moves = {source: target for source, target in locations.items()
             if source not in retirements and source not in transformed and source != target}
    transformed_targets = {locations[source]: (observed[source], artifact)
                           for source, artifact in transformed.items()}
    if not moves and not cleanup and not generated and not transformed_targets:
        return RunResult((Decision("skipped", "organization_unchanged"),), skipped=1)
    changed_targets = {*moves.values(), *generated, *transformed_targets}
    needed_directories: set[Path] = set()
    for target in changed_targets:
        parent = target.parent
        while parent not in directory_values:
            if not any(root.contains(parent) for root in root_values) or parent == parent.parent:
                return RunResult((Decision("invalid_input", "destination_parent_unobserved", target),))
            needed_directories.add(parent)
            parent = parent.parent
    if any(path in observed for path in needed_directories):
        return RunResult(tuple(Decision("conflict", "destination_parent_is_file", path)
                               for path in sorted(needed_directories) if path in observed))
    operations: list[Operation] = []
    mkdir_ids: dict[Path, str] = {}
    for index, path in enumerate(sorted(needed_directories, key=lambda item: (len(item.parts), str(item)))):
        member_id = f"mkdir-{index}"
        mkdir_dependencies = (mkdir_ids[path.parent],) if path.parent in mkdir_ids else ()
        operations.append(Operation(member_id, "mkdir", path, depends_on=mkdir_dependencies, reason="planned_parent_directory"))
        mkdir_ids[path] = member_id

    def parent_dependencies(path: Path) -> tuple[str, ...]:
        return tuple(mkdir_ids[parent] for parent in reversed(path.parents) if parent in mkdir_ids)

    covered_retirements: set[Path] = set()
    for index, (source, target) in enumerate(sorted(moves.items())):
        before = observed.get(target)
        if before is not None:
            displaced = target in moves or target in transformed or target in cleanup and target in replacement_paths
            if not displaced:
                return RunResult((Decision("conflict", "destination_contains_unretired_file", target, (str(source),)),))
            covered_retirements.add(target)
        operations.append(Operation(
            f"file-{index}", "move", target, source=observed[source], destination_before=before,
            collision="replace" if before is not None else "absent",
            depends_on=parent_dependencies(target), reason=reason if reasons is None else reasons.get(source, reason),
        ))
    for index, (target, (converted_source, artifact)) in enumerate(sorted(transformed_targets.items())):
        before = observed.get(target)
        if before is not None and target != converted_source.path and target not in moves and target not in replacement_paths:
            return RunResult((Decision("conflict", "converted_book_output_collision", target),))
        if before is not None:
            covered_retirements.add(target)
        operations.append(Operation(
            f"converted-book-{index}", "publish", target, source=converted_source,
            destination_before=before, artifact=artifact,
            collision="replace" if before is not None else "absent",
            depends_on=parent_dependencies(target), reason="verified_conversion_and_organization",
        ))
    publication_dependencies = tuple(operation.member_id for operation in operations if operation.kind in {"move", "publish"})
    for index, (target, (artwork_source, artifact)) in enumerate(sorted(generated.items())):
        before = observed.get(target)
        if before is not None and target not in replacement_paths:
            return RunResult((Decision("conflict", "artwork_replacement_not_authorized", target),))
        if before is not None:
            covered_retirements.add(target)
        operations.append(Operation(
            f"artwork-{index}", "publish", target, source=artwork_source, destination_before=before,
            artifact=artifact, collision="replace" if before is not None else "absent",
            depends_on=(*parent_dependencies(target), *publication_dependencies), reason="owned_series_artwork",
        ))
    publications_done = tuple(operation.member_id for operation in operations)
    for index, source in enumerate(sorted(cleanup)):
        if source in covered_retirements:
            continue
        if source not in observed:
            return RunResult((Decision("review_required", "retirement_source_unobserved", source),))
        if source in changed_targets:
            return RunResult((Decision("conflict", "retirement_is_new_output", source),))
        operations.append(Operation(f"retire-{index}", "quarantine", source,
                                    source=observed[source], depends_on=publications_done,
                                    reason=cleanup[source]))

    root_paths = {root.path for root in root_values}
    cleanup_candidates: set[Path] = set()
    for source in {*moves, *cleanup}:
        parent = source.parent
        while parent not in root_paths and any(root.contains(parent) for root in root_values):
            cleanup_candidates.add(parent)
            parent = parent.parent
    final_files = {*owners, *generated, *occupied_paths}
    removable: set[Path] = set()
    rmdir_ids: dict[Path, str] = {}
    for path in sorted(cleanup_candidates, key=lambda item: (-len(item.parts), str(item))):
        if path not in directory_values or path in changed_targets or path.name.startswith(".kce-"):
            continue
        if any(target.is_relative_to(path) for target in final_files):
            continue
        if any(child.parent == path and child not in removable for child in directory_values):
            continue
        cleanup_dependencies = tuple(operation.member_id for operation in operations)
        member_id = f"rmdir-{len(rmdir_ids)}"
        operations.append(Operation(member_id, "rmdir", path, depends_on=cleanup_dependencies,
                                    reason="observed_empty_after_group_cleanup"))
        rmdir_ids[path] = member_id
        removable.add(path)
    artifact_roots = sorted({
        artifact.source.root
        for artifact in (*transformed.values(), *(value[1] for value in generated.values()))
        if artifact.source is not None and not any(root.contains(artifact.source.path) for root in root_values)
    })
    plan = OperationPlan(policy_version, "organizer-" + uuid4().hex, tuple(operations),
                         (*tuple(RootPermission(root.path) for root in root_values),
                          *tuple(RootPermission(path, write=False) for path in artifact_roots)), reason)
    return RunResult((Decision("planned", reason),), plans=(plan,))


__all__ = ["project_release", "build_file_plan"]

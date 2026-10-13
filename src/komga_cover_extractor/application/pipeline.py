"""One application entry boundary, shared by CLI and the watch coordinator."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from types import MappingProxyType
from typing import cast
from uuid import uuid4

import regex

from ..covers.selection import CoverPolicy
from ..covers.series_policy import SeriesCoverPolicy
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
from ..domain.releases import ArchivePageObservation, FileObservation, ReleaseSnapshot
from ..infrastructure.filesystem import (
    BOOK_SUFFIXES,
    FilesystemError,
    fingerprint,
    matches,
)
from ..infrastructure.state import StateStore, StateStoreError
from ..infrastructure.transactions import TransactionExecutor
from ..infrastructure.workers import WorkerError, run_worker
from ..infrastructure.zip_archive import ArchivePolicy
from ..matching.classification import ClassificationPolicy
from ..matching.ranking import RankingPolicy
from ..matching.series import MatchingPolicy
from ..parsing.filenames import ParsingPolicy, RenamePolicy, parse_release
from .rename_lists import DictionaryPolicy, load_dictionaries, maintain_rename_lists


@dataclass(frozen=True, slots=True)
class RunRequest:
    command: str
    books: tuple[Path, ...] = ()
    roots: tuple[LibraryRoot, ...] = ()
    authorization: RunAuthorization = RunAuthorization()
    interactive: bool = False
    once: bool = False
    operation_id: str | None = None
    import_from: Path | None = None
    config_overrides: tuple[tuple[str, object], ...] = field(default=(), repr=False)
    config_path: Path | None = None
    watch: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "books", tuple(Path(book) for book in self.books))
        object.__setattr__(self, "roots", tuple(self.roots))
        object.__setattr__(self, "config_overrides", tuple(self.config_overrides))
        if not isinstance(self.authorization, RunAuthorization):
            raise TypeError("invocation authorization required")
        if type(self.watch) is not bool:
            raise TypeError("watch origin requires an explicit boolean")


def _merge(left: RunResult, right: RunResult, *, releases: tuple[ReleaseSnapshot, ...] | None = None) -> RunResult:
    return RunResult(
        decisions=(*left.decisions, *right.decisions),
        committed=left.committed + right.committed, skipped=left.skipped + right.skipped,
        failed=left.failed + right.failed,
        deferred_deliveries=tuple(dict.fromkeys((*left.deferred_deliveries, *right.deferred_deliveries))),
        releases=right.releases if releases is None else releases,
        plan_reports=(*left.plan_reports, *(PlanReport(plan) for plan in left.plans),
                      *right.plan_reports, *(PlanReport(plan) for plan in right.plans)),
        missing_releases=(*left.missing_releases, *right.missing_releases),
        planned_count=left.planned + right.planned,
    )


def _error_decision(error: Exception, path: Path | None = None) -> Decision:
    if isinstance(error, WorkerError):
        return Decision(cast(Status, error.status), error.reason, path)
    if isinstance(error, FilesystemError):
        status: Status = ("conflict" if error.reason in {"symlink_traversal", "not_regular_file"}
                          else "stale_plan" if error.reason == "source_changed" else "invalid_input")
        return Decision(status, error.reason, error.path)
    if isinstance(error, PermissionError):
        return Decision("permission_denied", "filesystem_observation_denied", path)
    if isinstance(error, FileNotFoundError):
        return Decision("stale_plan", "observed_input_disappeared", path)
    if isinstance(error, StateStoreError):
        return Decision("review_required", "workflow_state_unavailable", path)
    if isinstance(error, OSError):
        return Decision("failed", "filesystem_observation_failed", path)
    return Decision("invalid_input", "invalid_release_observation", path)


def _join_plans(plans: Sequence[OperationPlan], *, operation_id: str, policy_version: str) -> OperationPlan:
    operations: list[Operation] = []
    destinations: dict[Path, Operation] = {}
    permissions: dict[Path, RootPermission] = {}
    for index, plan in enumerate(plans):
        aliases: dict[str, str] = {}
        for root in plan.roots:
            prior = permissions.get(root.path)
            permissions[root.path] = RootPermission(root.path, root.write or prior is not None and prior.write)
        for operation in plan.operations:
            previous = destinations.get(operation.destination)
            if previous is not None:
                if previous.kind != operation.kind or operation.kind not in {"mkdir", "rmdir"}:
                    raise WorkerError("composed_plan_output_collision", "conflict")
                aliases[operation.member_id] = previous.member_id
                continue
            member_id = f"group-{index}-{operation.member_id}"
            aliases[operation.member_id] = member_id
            mapped = replace(operation, member_id=member_id,
                             depends_on=tuple(dict.fromkeys(aliases[member] for member in operation.depends_on)))
            operations.append(mapped)
            destinations[mapped.destination] = mapped
    return OperationPlan(policy_version, operation_id, tuple(operations), tuple(permissions.values()), "library_workflow")


class Pipeline:
    def __init__(self, *, roots: tuple[LibraryRoot, ...], state_dir: Path,
                 max_stage_bytes: int, parsing: ParsingPolicy, renaming: RenamePolicy,
                 archive: ArchivePolicy, covers: CoverPolicy, series_artwork: SeriesCoverPolicy,
                 policy_version: str, classification: ClassificationPolicy,
                 dictionary_policy: DictionaryPolicy, dictionary_version: str,
                 dictionary_diagnostics: tuple[Decision, ...], matching: MatchingPolicy,
                 ranking: RankingPolicy, options: Mapping[str, bool],
                 unacceptable_keywords: tuple[str, ...], ignored_folder_names: tuple[str, ...],
                 after_commit: Callable[[RunResult, RunRequest, tuple[ReleaseSnapshot, ...]], RunResult] | None = None,
                 release_check: Callable[[tuple[ReleaseSnapshot, ...], RunAuthorization], RunResult] | None = None,
                 log_dir: Path | None = None):
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.max_stage_bytes = max_stage_bytes
        self.parsing = parsing
        self.renaming = renaming
        self.archive = archive
        self.covers = covers
        self.series_artwork = series_artwork
        self._base_policy_version = policy_version
        self.policy_version = hashlib.sha256((policy_version + dictionary_version).encode("utf-8")).hexdigest()
        self.dictionary_policy = dictionary_policy
        self.dictionary_diagnostics = dictionary_diagnostics
        self.classification = classification
        self.matching = matching
        self.ranking = ranking
        self.options = MappingProxyType(dict(options))
        self.unacceptable_keywords = unacceptable_keywords
        self.ignored_folder_names = ignored_folder_names
        self.after_commit = after_commit
        self.release_check = release_check
        self.cancel = Event()
        self.roots = normalize_library_roots(roots)

    def run(self, request: RunRequest) -> RunResult:
        if request.command == "recover":
            return self._recover(request)
        if any(decision.status != "observed" for decision in self.dictionary_diagnostics):
            return RunResult(self.dictionary_diagnostics)
        if request.command == "rename-lists" and request.import_from is not None:
            return self._maintain_dictionaries(request, ())
        if request.command not in {"inspect", "extract", "convert", "organize", "duplicates", "run",
                                   "missing-releases", "rename-lists", "releases"}:
            return RunResult((Decision("unsupported", "unknown_command"),))
        try:
            roots = normalize_library_roots(request.roots or self.roots)
            if not roots and request.books:
                roots = normalize_library_roots(tuple(LibraryRoot(path.absolute().parent) for path in request.books))
        except (ValueError, TypeError):
            return RunResult((Decision("invalid_input", "invalid_selected_roots"),))
        if not roots:
            return RunResult((Decision("invalid_input", "no_selected_roots"),))
        files, directories, decisions, unavailable = self._inventory(roots)
        requested = frozenset(path.absolute() for path in request.books)
        for path in requested:
            if not any(root.contains(path) for root in roots):
                decisions.append(Decision("invalid_input", "outside_selected_roots", path))
            elif path not in files:
                decisions.append(Decision("invalid_input", "selected_input_unavailable", path))
            elif not self._is_book(path, roots) and request.command != "run":
                decisions.append(Decision("invalid_input", "selected_input_not_supported_book", path))
        snapshots, observation_decisions, bad_roots = self._observe_inventory(
            files, roots, archives=request.command not in {"run", "convert", "missing-releases", "rename-lists"},
            archive_paths=requested if requested and request.command in {"inspect", "extract", "convert", "rename-lists"} else None,
        )
        decisions.extend(observation_decisions)
        unavailable.update(bad_roots)
        if (request.watch and self.options.get("auto_classify_watchdog_paths", False)
                and self.options.get("check_for_existing_series_toggle", False)):
            roots, classification_decisions = self._classify_watch_roots(roots, snapshots)
            decisions.extend(classification_decisions)
        selected = tuple(release for release in snapshots if not requested or release.path in requested)
        result = RunResult(tuple(decisions), releases=selected)
        library_roots = tuple(root for root in roots if "library" in root.roles)
        catalog = LibrarySnapshot(
            snapshots, roots, bool(library_roots) and not any(root.path in unavailable for root in library_roots),
            "library_observation_unavailable" if any(root.path in unavailable for root in library_roots)
            else "no_configured_library_roots" if not library_roots else None,
        )
        if request.command == "inspect":
            return result
        if request.command == "rename-lists":
            return _merge(result, self._maintain_dictionaries(request, selected), releases=selected)
        if request.command == "missing-releases":
            from .missing_releases import report_missing_releases
            if not catalog.available:
                return replace(result, decisions=(*result.decisions, Decision("missing_dependency", catalog.reason or "library_unavailable")))
            return _merge(result, report_missing_releases(catalog.releases), releases=selected)
        if request.command == "releases":
            if not catalog.available:
                return replace(result, decisions=(*result.decisions, Decision("missing_dependency", catalog.reason or "library_unavailable")))
            library = tuple(release for release in selected if release.observation.root_role in {"library", "both"})
            return _merge(result, self._check_releases(library, request), releases=selected)
        if request.command in {"organize", "duplicates", "run"}:
            observed_before = {release.path: release for release in snapshots}
            outcome = self._run_library(request, roots, files, directories, selected, catalog, observed_before)
            return self._finish(_merge(result, outcome, releases=outcome.releases), request, tuple(observed_before.values()))
        if request.command == "extract":
            from .artwork_workflow import extract_artwork
            outcome = extract_artwork(
                selected, catalog, roots=roots, parsing=self.parsing, cover_policy=self.covers,
                series_policy=self.series_artwork, state_dir=self.state_dir, policy_version=self.policy_version,
                authorization=request.authorization, cancel=self.cancel, interactive=request.interactive,
                classification=self.classification,
            )
            updated = {release.path: release for release in selected}
            updated.update((release.path, release) for release in outcome.releases)
            return self._finish(_merge(result, outcome, releases=tuple(updated.values())), request, snapshots)
        from .conversion_workflow import convert_releases
        outcome = convert_releases(
            selected, roots=roots, archive_policy=self.archive, state_dir=self.state_dir,
            policy_version=self.policy_version, authorization=request.authorization,
            cancel=self.cancel, interactive=request.interactive,
        )
        visible = selected
        if request.authorization.apply and outcome.committed:
            observed_files, _, root_errors, _ = self._inventory(roots)
            current, more, _ = self._observe_inventory(observed_files, roots, archives=True)
            outcome = replace(outcome, decisions=(*outcome.decisions, *root_errors, *more))
            visible = current
        return self._finish(_merge(result, outcome, releases=visible), request, snapshots)

    def _inventory(
        self, roots: tuple[LibraryRoot, ...],
    ) -> tuple[dict[Path, FileFingerprint], tuple[Path, ...], list[Decision], set[Path]]:
        from .organize import observe_organization_files
        files: dict[Path, FileFingerprint] = {}
        directories: list[Path] = []
        decisions: list[Decision] = []
        unavailable: set[Path] = set()
        excluded = (self.state_dir,) if self.log_dir is None else (self.state_dir, self.log_dir)
        for root in roots:
            try:
                observed, folders = observe_organization_files(
                    (root,), self.ignored_folder_names, self.cancel, excluded_paths=excluded,
                )
                files.update(observed)
                directories.extend(folders)
            except (WorkerError, OSError, ValueError, TypeError) as error:
                decisions.append(_error_decision(error, root.path))
                unavailable.add(root.path)
        return files, tuple(dict.fromkeys(directories)), decisions, unavailable

    def _is_book(self, path: Path, roots: tuple[LibraryRoot, ...]) -> bool:
        root = next((root for root in roots if root.contains(path)), None)
        if root is None or path.suffix.lower() not in BOOK_SUFFIXES:
            return False
        relative = path.relative_to(root.path)
        return not (any(part.startswith(".") for part in relative.parts)
                    or any(part in self.ignored_folder_names for part in relative.parent.parts)
                    or path.is_relative_to(self.state_dir)
                    or self.log_dir is not None and path.is_relative_to(self.log_dir))

    def _classify_watch_roots(
        self, roots: tuple[LibraryRoot, ...], releases: tuple[ReleaseSnapshot, ...],
    ) -> tuple[tuple[LibraryRoot, ...], tuple[Decision, ...]]:
        from ..matching.classification import classify_root
        inferred: list[LibraryRoot] = []
        decisions: list[Decision] = []
        for root in roots:
            if ("library" not in root.roles or "download" in root.roles
                    or root.formats or root.extensions or root.library_types):
                inferred.append(root)
                continue
            observed = tuple(release for release in releases if root.contains(release.path)
                             and release.path.suffix.lower() in self.classification.supported_extensions)
            decision = classify_root(observed, self.classification)
            if decision.status != "matched":
                decisions.append(Decision("invalid_input" if decision.status == "invalid_input" else "no_match",
                                          decision.reason, root.path))
                inferred.append(root)
                continue
            inferred.append(replace(root, formats=decision.formats, extensions=decision.extensions))
            decisions.append(Decision("observed", "watch_root_policy_inferred", root.path, (
                "formats=" + ",".join(decision.formats), "extensions=" + ",".join(decision.extensions),
                f"total_files={decision.total_files}", f"sample_size={decision.sample_size}",
            )))
        return tuple(inferred), tuple(decisions)

    def _observe_inventory(
        self, files: Mapping[Path, FileFingerprint], roots: tuple[LibraryRoot, ...], *, archives: bool,
        archive_paths: frozenset[Path] | None = None,
    ) -> tuple[tuple[ReleaseSnapshot, ...], tuple[Decision, ...], set[Path]]:
        siblings: dict[Path, list[Path]] = {}
        for path in files:
            siblings.setdefault(path.parent, []).append(path)
        sibling_values = {directory: tuple(sorted(paths)) for directory, paths in siblings.items()}
        releases: list[ReleaseSnapshot] = []
        decisions: list[Decision] = []
        unavailable: set[Path] = set()
        for path, observed in sorted(files.items()):
            if not self._is_book(path, roots):
                continue
            root = next(root for root in roots if root.path == observed.root)
            try:
                if self.cancel.is_set():
                    raise WorkerError("shutdown_requested", "cancelled")
                role = "both" if {"library", "download"} <= set(root.roles) else "download" if "download" in root.roles else "library"
                release = parse_release(FileObservation(
                    path, siblings=sibling_values[path.parent], root_role=role,
                    fingerprint=observed, at_configured_root=path.parent == root.path,
                ), self.parsing)
                if archives and (archive_paths is None or path in archive_paths):
                    release = self._inspect_release(release, observed)
                releases.append(release)
                decisions.append(Decision("observed", "sha256_verified", path, (observed.sha256,)))
            except (WorkerError, OSError, ValueError, TypeError) as error:
                decisions.append(_error_decision(error, path))
                unavailable.add(root.path)
        return tuple(releases), tuple(decisions), unavailable

    def library_snapshot(self) -> LibrarySnapshot:
        roots = tuple(root for root in self.roots if "library" in root.roles)
        if not roots:
            return LibrarySnapshot((), (), False, "no_configured_library_roots")
        invalid_dictionary = next((decision for decision in self.dictionary_diagnostics if decision.status != "observed"), None)
        if invalid_dictionary is not None:
            return LibrarySnapshot((), roots, False, invalid_dictionary.reason)
        files, _, decisions, unavailable = self._inventory(roots)
        releases, errors, bad = self._observe_inventory(files, roots, archives=True)
        available = not unavailable and not bad and not self.cancel.is_set()
        reason = next((decision.reason for decision in (*decisions, *errors)
                       if decision.status not in {"observed", "skipped"}), None)
        return LibrarySnapshot(releases, roots, available, reason if not available else None)

    def _inspect_release(self, release: ReleaseSnapshot, source: FileFingerprint, *, retain_pages: bool = False) -> ReleaseSnapshot:
        if not matches(source) or not matches(cast(FileFingerprint, release.observation.fingerprint)):
            raise WorkerError("source_changed", "stale_plan")
        response = run_worker(
            "inspect", {"book": str(source.path), "root": str(source.root), "archive_policy": self.archive.as_dict()},
            timeout=self.archive.conversion_timeout_seconds, cancel=self.cancel,
        )
        if response.get("metadata_status") == "invalid":
            reason = response.get("metadata_reason")
            raise WorkerError(reason if isinstance(reason, str) and reason else "invalid_book_metadata", "invalid_input")
        if response.get("metadata_status") not in {"valid", "absent"}:
            raise WorkerError("invalid_metadata_observation", "failed")
        values = response.get("metadata")
        if not isinstance(values, list) or any(
            not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(value, str) for value in pair)
            for pair in values
        ):
            raise WorkerError("invalid_metadata_observation", "failed")
        members = response.get("archive_pages")
        if not isinstance(members, list) or len(members) > self.archive.archive_max_entries or any(
            not isinstance(member, dict) or set(member) != {"name", "is_grayscale", "is_directory"}
            or not isinstance(member["name"], str)
            or member["is_grayscale"] is not None and type(member["is_grayscale"]) is not bool
            or type(member["is_directory"]) is not bool for member in members
        ):
            raise WorkerError("invalid_archive_page_observation", "failed")
        metadata = tuple((cast(str, pair[0]), cast(str, pair[1])) for pair in values)
        pages = tuple(ArchivePageObservation(cast(str, member["name"]), cast(bool | None, member["is_grayscale"]),
                                            cast(bool, member["is_directory"])) for member in members)
        if not matches(source) or not matches(cast(FileFingerprint, release.observation.fingerprint)):
            raise WorkerError("source_changed", "stale_plan")
        observed = replace(release.observation, metadata=metadata, archive_pages=pages)
        parsed = parse_release(observed, self.parsing)
        return replace(parsed, observation=replace(parsed.observation, archive_pages=pages if retain_pages else ()),
                       provenance=tuple(dict.fromkeys((*parsed.provenance, "archive:observed"))))

    def _prepare_group(self, selected: tuple[ReleaseSnapshot, ...], catalog: LibrarySnapshot, *,
                       roots: tuple[LibraryRoot, ...], files: Mapping[Path, FileFingerprint], directories: Sequence[Path],
                       plans: Sequence[OperationPlan], scope: frozenset[Path], request: RunRequest,
                       stage: str, before: tuple[ReleaseSnapshot, ...]) -> RunResult:
        from .library_workflow import make_cover_similarity
        from .organize import OPTIONS, prepare_organization_group
        options = {name: self.options.get(name, False) for name in OPTIONS}
        if request.command == "run" and not (
            any("library" in root.roles for root in roots) and any("download" in root.roles for root in roots)
        ):
            options["check_for_existing_series_toggle"] = False
        return prepare_organization_group(
            selected, catalog, roots=roots, parsing=self.parsing, renaming=self.renaming,
            matching=self.matching, ranking=self.ranking,
            options=options, state_dir=self.state_dir,
            policy_version=self.policy_version, cancel=self.cancel, files=files, directories=directories,
            interactive=request.interactive, command=request.command, stage=stage,
            unacceptable_keywords=self.unacceptable_keywords, ignored_folder_names=self.ignored_folder_names,
            cover_similarity=make_cover_similarity(
                plans=plans, policy=self.covers, matching=self.matching, cancel=self.cancel,
                convert_native=request.command == "run" and self.options.get("convert_to_cbz_toggle", False),
            ),
            cover_policy=self.covers, series_policy=self.series_artwork, previous_plans=plans, scope_sources=scope,
            before=before,
        )

    def _run_library(
        self, request: RunRequest, roots: tuple[LibraryRoot, ...], files: dict[Path, FileFingerprint],
        directories: tuple[Path, ...], selected: tuple[ReleaseSnapshot, ...], catalog: LibrarySnapshot,
        observed_before: dict[Path, ReleaseSnapshot],
    ) -> RunResult:
        from .library_workflow import release_source
        from .organize import organization_groups
        result = RunResult(releases=catalog.releases, planned_count=0)
        try:
            grouping_policy = replace(self.matching, match_through_image_similarity=False)
            groups = organization_groups(selected, catalog, roots=roots, matching=grouping_policy)
            jobs: list[tuple[tuple[ReleaseSnapshot, ...], frozenset[Path]]] = []
            covered: set[Path] = set()
            root_paths = {root.path for root in roots}
            for group in groups:
                sources = {release_source(release).path for release in group}
                folders = {path.parent for path in sources if path.parent not in root_paths}
                scope = frozenset(path for path in files if path in sources
                                  or any(path.is_relative_to(folder) for folder in folders)
                                  or any(path.parent == source.parent and path.stem == source.stem for source in sources))
                jobs.append((group, scope))
                covered.update(scope)
            if request.command != "duplicates" and self.options.get("delete_unacceptable_files_toggle", False):
                patterns = tuple(regex.compile(pattern, regex.IGNORECASE) for pattern in self.unacceptable_keywords)
                explicit = {path.absolute() for path in request.books}
                for path in sorted(files):
                    if (path in covered or explicit and path not in explicit or path.name.startswith(".")
                            or not any("download" in root.roles and root.contains(path) for root in roots)):
                        continue
                    if any(pattern.search(path.name) for pattern in patterns):
                        jobs.append(((), frozenset((path,))))
            if not jobs:
                jobs.append(((), frozenset()))
            current = {release_source(release).path: release for release in catalog.releases}
            result = RunResult(releases=tuple(current.values()), planned_count=0)
            available = catalog.available
            for group, scope in jobs:
                if self.cancel.is_set():
                    result = _merge(result, RunResult((Decision("cancelled", "shutdown_requested"),)), releases=tuple(current.values()))
                    break
                live = tuple(current[release_source(release).path] for release in group if release_source(release).path in current)
                if group and not live:
                    continue
                before = LibrarySnapshot(tuple(current.values()), roots, available, catalog.reason if not available else None)
                if request.command == "run":
                    outcome = self._run_group(live, before, roots=roots, files=files, directories=directories,
                                              scope=scope, request=request, observed_before=observed_before)
                else:
                    from .organize import OPTIONS, organize_group
                    outcome = organize_group(
                        live, before, roots=roots, parsing=self.parsing, renaming=self.renaming,
                        matching=self.matching, ranking=self.ranking, options={name: self.options.get(name, False) for name in OPTIONS},
                        state_dir=self.state_dir, policy_version=self.policy_version, authorization=request.authorization,
                        cancel=self.cancel, interactive=request.interactive, command=request.command,
                        unacceptable_keywords=self.unacceptable_keywords, ignored_folder_names=self.ignored_folder_names,
                        cover_policy=self.covers, series_policy=self.series_artwork,
                        files=files, directories=directories, scope_sources=scope,
                    )
                failed_without_view = outcome.exit_code and not outcome.releases and not outcome.planned and not outcome.committed
                if not request.authorization.apply and not failed_without_view:
                    current = {release_source(release).path: release for release in outcome.releases}
                elif request.authorization.apply and not outcome.committed:
                    for release in outcome.releases:
                        source = release_source(release)
                        if release.path == source.path and source.path in current:
                            prior = current[source.path].observation.fingerprint
                            if prior == source:
                                current[source.path] = replace(
                                    release, observation=replace(release.observation, archive_pages=()))
                if any(decision.status not in {"observed", "planned", "committed", "skipped", "no_match"}
                       and (decision.reason == "library_observation_unavailable"
                            or decision.path is not None and any("library" in root.roles and root.contains(decision.path) for root in roots))
                       for decision in outcome.decisions):
                    available = False
                result = _merge(result, outcome, releases=tuple(current.values()))
                if request.authorization.apply and outcome.committed:
                    files, directories, visible, errors = self._refresh_committed(
                        files, directories, before.releases, outcome, roots,
                    )
                    current = {release_source(release).path: release for release in visible}
                    result = replace(result, decisions=(*result.decisions, *errors), releases=visible)
                    if errors:
                        available = False
                if outcome.failed or request.authorization.apply and outcome.committed and outcome.exit_code:
                    break
            if request.command == "run" and self.options.get("check_for_missing_volumes_toggle", False):
                from .missing_releases import report_missing_releases
                if available:
                    result = _merge(result, report_missing_releases(tuple(current.values())), releases=tuple(current.values()))
                else:
                    result = replace(result, decisions=(*result.decisions, Decision("missing_dependency", "library_observation_unavailable")))
            if (request.command == "run" and self.options.get("bookwalker_check", False)
                    and not request.watch and not self.cancel.is_set()):
                if available:
                    library = tuple(release for release in current.values() if release.observation.root_role in {"library", "both"})
                    result = _merge(result, self._check_releases(library, request), releases=tuple(current.values()))
                else:
                    result = replace(result, decisions=(*result.decisions, Decision("missing_dependency", "library_observation_unavailable")))
            return result
        except (WorkerError, OSError, ValueError, TypeError, StateStoreError) as error:
            decision = _error_decision(error)
            return replace(result, decisions=(*result.decisions, decision),
                           failed=result.failed + int(decision.status in {"failed", "permission_denied", "publication_failed"}))

    def _refresh_committed(
        self, files: Mapping[Path, FileFingerprint], directories: Sequence[Path],
        releases: tuple[ReleaseSnapshot, ...], result: RunResult, roots: tuple[LibraryRoot, ...],
    ) -> tuple[dict[Path, FileFingerprint], tuple[Path, ...], tuple[ReleaseSnapshot, ...], tuple[Decision, ...]]:
        from .planning import project_release
        observed = dict(files)
        folders = set(directories)
        proposed = {release.path: release for release in result.releases}
        visible = {release.path: proposed.get(release.path, release) for release in releases}
        errors: list[Decision] = []
        operations: list[Operation] = []
        store = StateStore(self.state_dir)
        ids = tuple(dict.fromkeys((*tuple(report.operation_id for report in result.plan_reports),
                                   *tuple(plan.operation_id for plan in result.plans))))
        for operation_id in ids:
            try:
                record = store.get_operation(operation_id)
                if record is not None and record.phase in {"committed", "cleanup_pending", "complete"}:
                    operations.extend(record.plan.operations)
            except (OSError, StateStoreError) as error:
                errors.append(_error_decision(error))
        for operation in operations:
            source = operation.source
            if source is None or operation.kind not in {"move", "quarantine"}:
                continue
            try:
                source.path.lstat()
            except FileNotFoundError:
                observed.pop(source.path, None)
                visible.pop(source.path, None)
            except OSError as error:
                errors.append(_error_decision(error, source.path))
        for operation in operations:
            destination = operation.destination
            root = next((root for root in roots if root.contains(destination)), None)
            if root is None:
                continue
            try:
                if operation.kind in {"move", "publish"}:
                    current = fingerprint(destination, root.path)
                    observed[destination] = current
                    if destination.suffix.lower() in BOOK_SUFFIXES:
                        release = proposed.get(destination)
                        if release is None and operation.source is not None:
                            source_release = next((release for release in releases
                                                   if release.observation.fingerprint == operation.source), None)
                            if source_release is not None:
                                release = project_release(source_release, destination, parsing=self.parsing, roots=roots)
                        if release is None:
                            errors.append(Decision("review_required", "committed_release_metadata_unavailable", destination))
                        else:
                            visible[destination] = replace(release, observation=replace(
                                release.observation, fingerprint=current, archive_pages=()))
                elif operation.kind == "mkdir":
                    folders.add(destination)
                elif operation.kind == "rmdir":
                    folders.discard(destination)
            except (OSError, ValueError) as error:
                errors.append(_error_decision(error, destination))
        return observed, tuple(sorted(folders)), tuple(visible.values()), tuple(errors)

    def _run_group(
        self, selected: tuple[ReleaseSnapshot, ...], catalog: LibrarySnapshot, *,
        roots: tuple[LibraryRoot, ...], files: Mapping[Path, FileFingerprint], directories: Sequence[Path],
        scope: frozenset[Path], request: RunRequest, observed_before: dict[Path, ReleaseSnapshot],
    ) -> RunResult:
        from .artwork_workflow import _ArtworkError
        from .conversion_workflow import (
            PreparedConversion,
            prepare_conversion,
            release_conversion_artifacts,
        )
        from .convert import observe_header
        from .header_correction import correct_headers
        from .library_workflow import (
            ArtworkWorkspace,
            plan_projected_artwork,
            release_artwork_workspace,
            release_source,
            resolve_book_bytes,
        )
        from .planning import project_release
        original = catalog.releases
        decisions: list[Decision] = []
        previous: list[OperationPlan] = []
        dictionary_result = RunResult(planned_count=0)
        operation_id = "run-" + uuid4().hex
        current = {release_source(release).path: release for release in catalog.releases}
        selected_sources = {release_source(release).path for release in selected}
        conversions: list[PreparedConversion] = []
        completed_parts: set[Path] = set()
        outcome: RunResult | None = None
        final: OperationPlan | None = None
        try:
            with ExitStack() as lifetime:
                workspace_root = next((root.path for root in roots if any(root.contains(path) for path in scope)), roots[0].path)
                workspace = lifetime.enter_context(ArtworkWorkspace(
                    root=workspace_root, operation_id=operation_id, authorization=request.authorization,
                    max_bytes=self.max_stage_bytes,
                ))
                if self.options.get("correct_file_extensions_toggle", False):
                    corrected = correct_headers(
                        selected, roots=roots, parsing=self.parsing, archive_policy=self.archive, state_dir=self.state_dir,
                        policy_version=self.policy_version, authorization=RunAuthorization(), cancel=self.cancel,
                        manual_rename=self.options.get("manual_rename", False), interactive=request.interactive,
                        rename_zip_to_cbz=self.options.get("rename_zip_to_cbz", True),
                    )
                    decisions.extend(corrected.decisions)
                    if corrected.exit_code:
                        return RunResult(tuple(decisions), releases=original)
                    previous.extend(corrected.plans)
                    current.update((release_source(release).path, release) for release in corrected.releases)
                if self.options.get("convert_to_cbz_toggle", False):
                    total_scratch = 0
                    for source in sorted(selected_sources):
                        if source in completed_parts:
                            continue
                        release = current[source]
                        observed = release_source(release)
                        header = observe_header(observed.path, root=observed.root, policy=self.archive, cancel=self.cancel)
                        if header.source != observed:
                            raise WorkerError("conversion_source_changed", "stale_plan")
                        if header.container not in {"rar", "7z"}:
                            continue
                        prepared = lifetime.enter_context(prepare_conversion(
                            header, roots=roots, archive_policy=self.archive, authorization=request.authorization,
                            cancel=self.cancel, operation_id=operation_id,
                        ))
                        conversion = prepared.result
                        if conversion.status != "valid":
                            raise WorkerError(conversion.reason, conversion.status)
                        assert conversion.artifact is not None
                        total_scratch += conversion.artifact.size
                        if total_scratch > self.max_stage_bytes:
                            raise WorkerError("conversion_group_scratch_limit", "review_required")
                        destination = release.path.with_suffix(".cbz")
                        existing = files.get(destination)
                        if existing is not None and destination != source:
                            raise WorkerError("conversion_destination_exists", "conflict")
                        operations = [Operation(
                            "book", "publish", destination, source=observed,
                            destination_before=existing, collision="replace" if existing is not None else "absent",
                            artifact=ArtifactSpec(source=conversion.artifact, media_type="application/zip"),
                            reason="verified_native_conversion",
                        )]
                        operations.extend(Operation(
                            f"source-{index}", "quarantine", part.path, source=part, depends_on=("book",),
                            reason="retain_original_archive_part_after_book_commit",
                        ) for index, part in enumerate(conversion.sources) if part.path != destination)
                        permissions = tuple(RootPermission(root.path) for root in roots)
                        if prepared.artifact_purpose == "informational":
                            permissions += (RootPermission(conversion.artifact.root, write=False),)
                        previous.append(OperationPlan(self.policy_version, operation_id, tuple(operations), permissions,
                                                      "archive_conversion_to_cbz"))
                        conversions.append(prepared)
                        completed_parts.update(part.path for part in conversion.sources)
                        for part in conversion.sources[1:]:
                            current.pop(part.path, None)
                            selected_sources.discard(part.path)
                        current[source] = project_release(release, destination, parsing=self.parsing, roots=roots)
                group_catalog = replace(catalog, releases=tuple(current.values()))
                chosen = tuple(current[source] for source in sorted(selected_sources) if source in current)
                filtered = self._prepare_group(chosen, group_catalog, roots=roots, files=files, directories=directories,
                                               plans=previous, scope=scope, request=request, stage="filters", before=original)
                decisions.extend(filtered.decisions)
                if filtered.exit_code:
                    return RunResult(tuple(decisions), releases=original)
                previous = workspace.spill_plans(filtered.plans)
                current = {release_source(release).path: release for release in filtered.releases}
                for source in sorted(selected_sources):
                    selected_release = current.get(source)
                    if selected_release is not None:
                        current[source] = self._inspect_release(selected_release, resolve_book_bytes(selected_release, previous), retain_pages=True)
                        original_observation = replace(current[source].observation, path=source)
                        observed_before[source] = parse_release(original_observation, self.parsing)
                # Library candidates are observed too; unavailable metadata must not become an empty successful match.
                available = catalog.available
                candidate_folders = {current[source].path.parent for source in selected_sources if source in current}
                cross_series_metadata = (
                    self.options.get("check_for_existing_series_toggle", False)
                    or self.series_artwork.copy_existing_volume_covers_toggle
                )
                for source, release in tuple(current.items()):
                    if (source in selected_sources or release.observation.root_role not in {"library", "both"}
                            or not cross_series_metadata and release.path.parent not in candidate_folders
                            or "archive:observed" in release.provenance):
                        continue
                    try:
                        current[source] = self._inspect_release(release, resolve_book_bytes(release, previous))
                        observed_before[source] = parse_release(replace(current[source].observation, path=source), self.parsing)
                    except (WorkerError, OSError, ValueError, TypeError) as error:
                        decisions.append(_error_decision(error, source))
                        available = False
                if self.dictionary_policy.generate_release_group_list_toggle:
                    candidates = tuple(release for source, release in current.items() if source in selected_sources
                                       and release.observation.root_role in {"library", "both"})
                    dictionary_result = self._maintain_dictionaries(request, candidates, roots=roots)
                    if dictionary_result.exit_code:
                        return _merge(RunResult(tuple(decisions), releases=original), dictionary_result, releases=original)
                    if dictionary_result.committed:
                        current = {source: parse_release(release.observation, self.parsing) for source, release in current.items()}
                        previous = [replace(plan, policy_version=self.policy_version) for plan in previous]
                group_catalog = LibrarySnapshot(tuple(current.values()), roots, available,
                                                "library_observation_unavailable" if not available else None)
                chosen = tuple(current[source] for source in sorted(selected_sources) if source in current)
                renamed = self._prepare_group(chosen, group_catalog, roots=roots, files=files, directories=directories,
                                              plans=previous, scope=scope, request=request, stage="rename_and_duplicates", before=original)
                decisions.extend(renamed.decisions)
                if renamed.exit_code:
                    return _merge(RunResult(tuple(decisions), releases=original), dictionary_result, releases=original)
                previous = workspace.spill_plans(renamed.plans)
                current = {release_source(release).path: release for release in renamed.releases}
                overlap = any({"library", "download"} <= set(root.roles) for root in roots)
                if self.options.get("extract_covers_toggle", False) and overlap:
                    art_selected = tuple(release for source, release in current.items() if source in selected_sources
                                         and release.observation.root_role in {"library", "both"})
                    if art_selected:
                        art = plan_projected_artwork(
                            art_selected, replace(group_catalog, releases=tuple(current.values())), roots=roots,
                            parsing=self.parsing, cover_policy=self.covers, series_policy=self.series_artwork,
                            classification=self.classification, files=files, directories=directories,
                            previous_plans=previous, state_dir=self.state_dir, policy_version=self.policy_version,
                            workspace=workspace, cancel=self.cancel,
                            convert_native=self.options.get("convert_to_cbz_toggle", False),
                            before=original,
                        )
                        decisions.extend(art.decisions)
                        if art.exit_code:
                            return _merge(RunResult(tuple(decisions), releases=original), dictionary_result, releases=original)
                        previous.extend(art.plans)
                        current = {release_source(release).path: release for release in art.releases}
                chosen = tuple(current[source] for source in sorted(selected_sources) if source in current)
                matched = self._prepare_group(chosen, replace(group_catalog, releases=tuple(current.values())), roots=roots,
                                              files=files, directories=directories, plans=previous,
                                              scope=scope, request=request, stage="after_matching", before=original)
                decisions.extend(matched.decisions)
                if matched.exit_code:
                    return _merge(RunResult(tuple(decisions), releases=original), dictionary_result, releases=original)
                previous = workspace.spill_plans(matched.plans)
                current = {release_source(release).path: release for release in matched.releases}
                if self.options.get("extract_covers_toggle", False) and not overlap:
                    art_selected = tuple(release for source, release in current.items() if source in selected_sources
                                         and release.observation.root_role in {"library", "both"})
                    if art_selected:
                        art = plan_projected_artwork(
                            art_selected, replace(group_catalog, releases=tuple(current.values())), roots=roots,
                            parsing=self.parsing, cover_policy=self.covers, series_policy=self.series_artwork,
                            classification=self.classification, files=files, directories=directories,
                            previous_plans=previous, state_dir=self.state_dir, policy_version=self.policy_version,
                            workspace=workspace, cancel=self.cancel,
                            convert_native=self.options.get("convert_to_cbz_toggle", False),
                            before=original,
                        )
                        decisions.extend(art.decisions)
                        if art.exit_code:
                            return _merge(RunResult(tuple(decisions), releases=original), dictionary_result, releases=original)
                        previous.extend(art.plans)
                        current = {release_source(release).path: release for release in art.releases}
                        chosen = tuple(current[source] for source in sorted(selected_sources) if source in current)
                        matched = self._prepare_group(chosen, replace(group_catalog, releases=tuple(current.values())), roots=roots,
                                                      files=files, directories=directories, plans=previous,
                                                      scope=scope, request=request, stage="after_matching", before=original)
                        decisions.extend(matched.decisions)
                        if matched.exit_code:
                            return _merge(RunResult(tuple(decisions), releases=original), dictionary_result, releases=original)
                        previous = workspace.spill_plans(matched.plans)
                        current = {release_source(release).path: release for release in matched.releases}
                if not previous:
                    return _merge(RunResult(tuple(decisions), skipped=matched.skipped,
                                            releases=tuple(current.values()), planned_count=0), dictionary_result,
                                  releases=tuple(current.values()))
                final = _join_plans(previous, operation_id=operation_id, policy_version=self.policy_version)
                workspace.record(final)
                if request.authorization.apply:
                    for prepared in conversions:
                        prepared.retain_for_recovery()
                executor_roots = tuple(dict.fromkeys((*tuple(root.path for root in roots),
                                                      *tuple(permission.path for permission in final.roots if not permission.write))))
                executor = TransactionExecutor(self.state_dir, executor_roots, cancel=self.cancel,
                                               max_stage_bytes=self.max_stage_bytes)
                if request.authorization.apply:
                    workspace.retained = True
                outcome = executor.apply(final, authorization=request.authorization)
                record = executor.state_store.get_operation(operation_id) if request.authorization.apply else None
                if request.authorization.apply:
                    workspace.retained = record is not None and record.phase != "complete"
                    for prepared in conversions:
                        if prepared._scratch is not None and record is None:
                            prepared._scratch.lifecycle = "temporary"
                    if record is not None and record.phase == "complete":
                        settlement = release_conversion_artifacts(final, roots=roots, state_dir=self.state_dir,
                                                                  authorization=request.authorization)
                        if settlement is not None:
                            decisions.append(settlement)
                        else:
                            for prepared in conversions:
                                if prepared._scratch is not None:
                                    prepared._scratch.lifecycle = "settled"
                        if workspace.path is not None:
                            workspace.retained = True
                            artwork_settlement = release_artwork_workspace(
                                final, state_dir=self.state_dir, authorization=request.authorization,
                            )
                            if artwork_settlement is not None:
                                decisions.append(artwork_settlement)
                visible = tuple(replace(release, observation=replace(release.observation, archive_pages=()))
                                for release in current.values()) if not request.authorization.apply or outcome.committed else original
                report: PlanReport = PlanReport(final)
                if len(conversions) == 1:
                    from .conversion_workflow import ConversionPlanReport
                    report = ConversionPlanReport(final, conversions[0])
                grouped = RunResult((*decisions, *outcome.decisions), committed=outcome.committed,
                                    skipped=outcome.skipped, failed=outcome.failed,
                                    deferred_deliveries=outcome.deferred_deliveries, releases=visible,
                                    plan_reports=(report,), planned_count=1)
                return _merge(grouped, dictionary_result, releases=visible)
        except _ArtworkError as error:
            decision = error.decision
        except (WorkerError, OSError, ValueError, TypeError, StateStoreError) as error:
            decision = _error_decision(error)
        failed = int(decision.status in {"failed", "permission_denied", "publication_failed"})
        visible = tuple(current.values()) if outcome is not None and outcome.committed else original
        partial = RunResult(
            (*decisions, *((outcome.decisions) if outcome is not None else ()), decision),
            committed=outcome.committed if outcome is not None else 0,
            skipped=outcome.skipped if outcome is not None else 0,
            failed=(outcome.failed if outcome is not None else 0) + failed,
            releases=visible, plan_reports=(PlanReport(final),) if final is not None else (),
        )
        return _merge(partial, dictionary_result, releases=visible)

    def _check_releases(self, releases: tuple[ReleaseSnapshot, ...], request: RunRequest) -> RunResult:
        if self.release_check is None:
            return RunResult((Decision("missing_dependency", "release_check_transport_unavailable"),))
        return self.release_check(releases, request.authorization)

    def _finish(self, result: RunResult, request: RunRequest, before: tuple[ReleaseSnapshot, ...]) -> RunResult:
        if self.after_commit is None:
            return result
        return self.after_commit(result, request, before)

    def _recover(self, request: RunRequest) -> RunResult:
        from .conversion_workflow import release_conversion_artifacts
        from .library_workflow import release_artwork_workspace
        from .recovery import recover_operation
        if request.operation_id is None:
            return RunResult((Decision("invalid_input", "operation_id_required"),))
        try:
            roots = normalize_library_roots(request.roots or self.roots)
        except (ValueError, TypeError):
            return RunResult((Decision("invalid_input", "invalid_selected_roots"),))
        if not roots and request.authorization.apply:
            return RunResult((Decision("invalid_input", "no_selected_roots"),))
        before_files, _, _, _ = self._inventory(roots)
        before, _, _ = self._observe_inventory(before_files, roots, archives=True)
        outcome = recover_operation(
            request.operation_id, state_dir=self.state_dir, roots=tuple(root.path for root in roots),
            authorization=request.authorization, max_stage_bytes=self.max_stage_bytes,
            cancel=self.cancel,
        )
        if request.authorization.apply:
            for plan in outcome.plans:
                for settlement in (
                    release_conversion_artifacts(plan, roots=roots, state_dir=self.state_dir, authorization=request.authorization),
                    release_artwork_workspace(plan, state_dir=self.state_dir, authorization=request.authorization),
                ):
                    if settlement is not None:
                        outcome = replace(outcome, decisions=(*outcome.decisions, settlement))
        if outcome.committed:
            files, _, root_errors, _ = self._inventory(roots)
            visible, observation_errors, _ = self._observe_inventory(files, roots, archives=True)
            errors = tuple(decision for decision in (*root_errors, *observation_errors)
                           if decision.status != "observed")
            outcome = replace(outcome, releases=visible, decisions=(*outcome.decisions, *errors))
        return self._finish(outcome, request, before)

    def _maintain_dictionaries(
        self, request: RunRequest, releases: tuple[ReleaseSnapshot, ...], *,
        roots: tuple[LibraryRoot, ...] | None = None,
    ) -> RunResult:
        current_roots = roots if roots is not None else request.roots or self.roots
        policy = replace(self.dictionary_policy, publisher_excluded_roots=tuple(dict.fromkeys((
            *self.dictionary_policy.publisher_excluded_roots,
            *(root.path for root in current_roots if root.formats == ("chapter",)),
        ))))
        result = maintain_rename_lists(
            releases, state_dir=self.state_dir, policy=policy,
            authorization=request.authorization, cancel=self.cancel,
            interactive=request.interactive, import_from=request.import_from,
        )
        if result.committed:
            dictionaries = load_dictionaries(self.state_dir)
            self.dictionary_diagnostics = dictionaries.diagnostics
            self.parsing = replace(
                self.parsing, release_groups=tuple(regex.escape(value) for value in dictionaries.release_groups),
                publishers=tuple(regex.escape(value) for value in dictionaries.publishers),
            )
            self.renaming = replace(self.renaming, publishers=dictionaries.publishers)
            self.policy_version = hashlib.sha256((self._base_policy_version + dictionaries.version).encode("utf-8")).hexdigest()
            return replace(result, decisions=(*result.decisions, *dictionaries.diagnostics))
        return result

    def close(self) -> None:
        self.cancel.set()

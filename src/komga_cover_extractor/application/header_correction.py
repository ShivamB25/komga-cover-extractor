from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast

from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.libraries import LibraryRoot, normalize_library_roots
from ..domain.operations import Operation, OperationPlan, RootPermission
from ..domain.releases import ReleaseSnapshot
from ..infrastructure.filesystem import (
    FilesystemError,
    checked_path,
    fingerprint,
    matches,
)
from ..infrastructure.transactions import TransactionExecutor
from ..infrastructure.workers import WorkerError
from ..infrastructure.zip_archive import ArchiveError, ArchivePolicy
from ..parsing.filenames import ParsingPolicy, parse_release
from .convert import HeaderObservation, observe_header
from .planning import read_interactive_line


def correct_headers(
    releases: Sequence[ReleaseSnapshot], *, roots: tuple[LibraryRoot, ...],
    parsing: ParsingPolicy, archive_policy: ArchivePolicy, state_dir: Path,
    policy_version: str, authorization: RunAuthorization, cancel: Event,
    manual_rename: bool, interactive: bool = False, rename_zip_to_cbz: bool = True,
    zip_only: bool = False, observations: dict[Path, HeaderObservation] | None = None,
) -> RunResult:
    roots = normalize_library_roots(roots)
    decisions: list[Decision] = []
    plans: list[OperationPlan] = []
    projected: list[ReleaseSnapshot] = []
    committed = skipped = failed = 0
    pending_input = bytearray()
    for release in releases:
        source = release.observation.fingerprint
        projected.append(release)
        if cancel.is_set():
            decisions.append(Decision("cancelled", "shutdown_requested", release.path))
            break
        if source is None:
            decisions.append(Decision("invalid_input", "header_source_observation_required", release.path))
            continue
        root = next((root for root in roots if root.path == source.root and root.contains(source.path)), None)
        if root is None:
            decisions.append(Decision("permission_denied", "outside_selected_roots", source.path))
            failed += 1
            continue
        try:
            header = observations.get(source.path) if observations is not None else None
            if header is None:
                header = observe_header(source.path, root=source.root, policy=archive_policy, cancel=cancel)
                if observations is not None:
                    observations[source.path] = header
            if header.source != source or not matches(source):
                decisions.append(Decision("stale_plan", "header_source_changed", source.path))
                continue
            if zip_only and (header.container != "zip" or source.path.suffix.lower() != ".zip"):
                skipped += 1
                decisions.append(Decision("skipped", "conversion_zip_rename_not_applicable", source.path))
                continue
            destination = release.path.with_suffix(header.target_suffix)
            if destination == release.path or (source.path.suffix.lower() == ".zip" and not rename_zip_to_cbz and header.container == "zip"):
                decisions.append(Decision("skipped", "header_suffix_unchanged", source.path))
                skipped += 1
                continue
            checked_path(destination, root.path, allow_missing=True)
            if destination.exists() or destination.is_symlink():
                decisions.append(Decision("conflict", "header_destination_exists", destination))
                continue
            if manual_rename:
                if not interactive or not sys.stdin.isatty() or not sys.stderr.isatty():
                    decisions.append(Decision("review_required", "header_rename_requires_choice", source.path, (str(destination),)))
                    continue
                sys.stderr.write(f"Rename {str(source.path)!r} to {str(destination)!r}? Type rename or keep: ")
                sys.stderr.flush()
                try:
                    choice = read_interactive_line(cancel, pending_input)
                except (EOFError, KeyboardInterrupt):
                    decisions.append(Decision("cancelled", "interactive_choice_cancelled", source.path))
                    continue
                if choice != "rename":
                    decisions.append(Decision("skipped" if choice == "keep" else "review_required", "header_rename_not_authorized", source.path))
                    skipped += int(choice == "keep")
                    continue
            plan = OperationPlan(
                policy_version, f"header-{uuid.uuid4().hex}",
                (Operation("book", "move", destination, source=source, reason="observed_container_extension"),),
                tuple(RootPermission(root.path) for root in roots), reason="header_correction",
            )
            plans.append(plan)
            result = TransactionExecutor(state_dir, tuple(root.path for root in roots), cancel=cancel).apply(
                plan, authorization=authorization,
            )
            decisions.extend(result.decisions)
            committed += result.committed
            skipped += result.skipped
            failed += result.failed
            if not authorization.apply or result.committed:
                projected_source = fingerprint(destination, root.path) if result.committed else source
                observation = replace(
                    release.observation, path=destination, fingerprint=projected_source,
                    siblings=tuple(destination if path == release.path else path for path in release.observation.siblings),
                )
                projected[-1] = parse_release(observation, parsing)
        except (ArchiveError, WorkerError) as error:
            decisions.append(Decision(cast(Status, error.status), error.reason, source.path))
            failed += int(error.status in {"failed", "permission_denied", "publication_failed"})
        except FilesystemError as error:
            decisions.append(Decision("invalid_input", error.reason, error.path))
        except PermissionError:
            decisions.append(Decision("permission_denied", "header_observation_denied", source.path))
            failed += 1
        except OSError:
            decisions.append(Decision("failed", "header_observation_failed", source.path))
            failed += 1
    return RunResult(tuple(decisions), tuple(plans), committed=committed, skipped=skipped,
                     failed=failed, releases=tuple(projected))

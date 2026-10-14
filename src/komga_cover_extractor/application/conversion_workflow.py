"""Prepare native archive artifacts and publish each verified book through the journal."""
from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal

from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.libraries import LibraryRoot, normalize_library_roots
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationPlan,
    PlanReport,
    RootPermission,
)
from ..domain.releases import ReleaseSnapshot
from ..infrastructure.filesystem import (
    FilesystemError,
    checked_path,
    fingerprint,
    sync_directory,
)
from ..infrastructure.state import StateStore, StateStoreError
from ..infrastructure.transactions import TransactionExecutor
from ..infrastructure.workers import WorkerError
from ..infrastructure.zip_archive import ArchivePolicy
from ..parsing.filenames import ParsingPolicy
from .convert import ConversionResult, HeaderObservation, convert_book, observe_header
from .planning import read_interactive_line

ArtifactPurpose = Literal["informational", "publication"]
_MARKER = ".owner.json"
_SCRATCH_PREFIX = ".kce-convert-"
_MARKER_MAX_BYTES = 16_384


class _WorkflowError(WorkerError):
    def __init__(self, status: Status, reason: str, path: Path | None = None) -> None:
        self.decision = Decision(status, reason, path)
        super().__init__(reason, status)


def _source_root(source: FileFingerprint, roots: tuple[LibraryRoot, ...]) -> LibraryRoot:
    root = next((root for root in roots if root.contains(source.path)), None)
    if root is None:
        raise _WorkflowError("permission_denied", "outside_selected_roots", source.path)
    if source.root != root.path:
        raise _WorkflowError("invalid_input", "source_fingerprint_root_mismatch", source.path)
    checked_path(root.path, root.path)
    if not stat.S_ISDIR(root.path.lstat().st_mode):
        raise _WorkflowError("invalid_input", "selected_root_not_directory", root.path)
    return root


def _assert_source(source: FileFingerprint) -> None:
    try:
        actual = fingerprint(source.path, source.root)
    except FilesystemError as error:
        status: Status = "stale_plan" if error.reason in {"missing_path", "source_changed"} else "invalid_input"
        raise _WorkflowError(status, error.reason, error.path) from error
    if actual != source:
        raise _WorkflowError("stale_plan", "source_fingerprint_changed", source.path)


def _marker_value(path: Path, operation_id: str, source: FileFingerprint) -> dict[str, object]:
    info = path.lstat()
    return {
        "format": "kce-conversion-scratch-v1", "operation_id": operation_id,
        "device": info.st_dev, "inode": info.st_ino, "source": source.as_dict(),
    }


@dataclass(slots=True)
class _ConversionScratch:
    path: Path
    root: Path
    operation_id: str
    source: FileFingerprint
    marker: FileFingerprint
    identity: tuple[int, int]
    artifact: FileFingerprint | None = None
    lifecycle: Literal["temporary", "journaled", "settled"] = "temporary"

    @classmethod
    def create(cls, root: Path, operation_id: str, source: FileFingerprint) -> _ConversionScratch:
        path = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=root))
        info = path.lstat()
        descriptor: int | None = None
        try:
            descriptor = os.open(path / _MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            payload = json.dumps(_marker_value(path, operation_id, source), ensure_ascii=False).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                if stream.write(payload) != len(payload):
                    raise OSError("conversion_marker_short_write")
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(path)
            sync_directory(root)
            return cls(path, root, operation_id, source, fingerprint(path / _MARKER, root),
                       (info.st_dev, info.st_ino))
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if (path.lstat().st_dev, path.lstat().st_ino) == (info.st_dev, info.st_ino):
                (path / _MARKER).unlink(missing_ok=True)
                path.rmdir()
            raise

    def discard(self) -> None:
        if self.lifecycle == "settled":
            return
        checked_path(self.path, self.root)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (
            info.st_dev, info.st_ino
        ) != self.identity:
            raise _WorkflowError("review_required", "conversion_scratch_identity_changed", self.path)
        if fingerprint(self.marker.path, self.root) != self.marker:
            raise _WorkflowError("review_required", "conversion_marker_changed", self.marker.path)
        expected = {self.marker.path}
        if self.artifact is not None:
            if fingerprint(self.artifact.path, self.root) != self.artifact:
                raise _WorkflowError("review_required", "conversion_artifact_changed", self.artifact.path)
            expected.add(self.artifact.path)
        if set(self.path.iterdir()) != expected:
            raise _WorkflowError("review_required", "conversion_scratch_has_unrecorded_files", self.path)
        if self.artifact is not None:
            self.artifact.path.unlink()
        self.marker.path.unlink()
        self.path.rmdir()
        sync_directory(self.root)
        self.lifecycle = "settled"


@dataclass(frozen=True, slots=True)
class PreparedConversion:
    """The native artifact lives until this context ends or its journal can recover it."""
    header: HeaderObservation
    result: ConversionResult
    operation_id: str
    artifact_purpose: ArtifactPurpose
    _scratch: _ConversionScratch | None = None

    def retain_for_recovery(self) -> None:
        """Call before executing a plan that references this publication artifact."""
        if self._scratch is None or self.result.artifact is None or self._scratch.lifecycle == "settled":
            raise ValueError("only a real publication artifact can be retained")
        self._scratch.lifecycle = "journaled"


@contextmanager
def prepare_conversion(
    header: HeaderObservation, *, roots: tuple[LibraryRoot, ...], archive_policy: ArchivePolicy,
    authorization: RunAuthorization, cancel: Event, operation_id: str | None = None,
) -> Iterator[PreparedConversion]:
    """Prepare only. Organizers may compose the artifact and sources into their own group."""
    roots = normalize_library_roots(roots)
    root = _source_root(header.source, roots)
    _assert_source(header.source)
    if cancel.is_set():
        raise _WorkflowError("cancelled", "shutdown_requested", header.source.path)
    if header.container not in {"rar", "7z"}:
        raise _WorkflowError("unsupported", "container_does_not_require_conversion", header.source.path)
    operation_id = operation_id or f"convert-{uuid.uuid4().hex}"
    if not operation_id or len(operation_id) > 128 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in operation_id
    ):
        raise ValueError("invalid conversion operation identifier")
    scratch: _ConversionScratch | None = None
    with ExitStack() as stack:
        if authorization.apply:
            _assert_source(header.source)
            scratch = _ConversionScratch.create(root.path, operation_id, header.source)
            directory = scratch.path
        else:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="kce-convert-preview-")))
        try:
            result = convert_book(header.source.path, root=root.path, scratch_dir=directory,
                                  policy=archive_policy, cancel=cancel)
            if scratch is not None:
                scratch.artifact = result.artifact
            if result.status == "valid":
                if result.artifact is None or result.artifact.path.parent != directory or result.artifact.root != (
                    root.path if authorization.apply else directory
                ) or not result.sources or result.sources[0] != header.source:
                    raise _WorkflowError("failed", "conversion_artifact_scope_mismatch", header.source.path)
                for source in result.sources:
                    _source_root(source, roots)
                    _assert_source(source)
                _assert_source(result.artifact)
                if cancel.is_set():
                    raise _WorkflowError("cancelled", "shutdown_requested", header.source.path)
            yield PreparedConversion(header, result, operation_id,
                                     "publication" if authorization.apply else "informational", scratch)
        finally:
            if scratch is not None and scratch.lifecycle == "temporary":
                scratch.discard()



def build_conversion_plan(
    prepared: PreparedConversion, *, policy_version: str,
    destination_before: FileFingerprint | None = None,
) -> OperationPlan:
    """Publish once and retire all observed archive parts only after the group commits."""
    result = prepared.result
    if result.status != "valid" or result.artifact is None or not result.sources:
        raise ValueError("a verified native conversion artifact is required")
    destination = prepared.header.source.path.with_suffix(".cbz")
    if destination in {source.path for source in result.sources}:
        raise ValueError("header correction must precede a conversion over its own source")
    operations = [Operation(
        "book", "publish", destination, source=prepared.header.source,
        destination_before=destination_before,
        artifact=ArtifactSpec(source=result.artifact, media_type="application/zip"),
        collision="replace" if destination_before is not None else "absent",
        reason="verified_native_conversion",
    )]
    operations.extend(Operation(
        f"source-{index}", "quarantine", source.path, source=source,
        depends_on=("book",), reason="retain_original_archive_part_after_book_commit",
    ) for index, source in enumerate(result.sources))
    permissions = [RootPermission(prepared.header.source.root)]
    if prepared.artifact_purpose == "informational":
        permissions.append(RootPermission(result.artifact.root, write=False))
    return OperationPlan(policy_version, prepared.operation_id, tuple(operations), tuple(permissions),
                         reason="archive_conversion_to_cbz")


@dataclass(frozen=True, slots=True, init=False)
class ConversionPlanReport(PlanReport):
    _conversion_json: str

    def __init__(self, plan: OperationPlan, prepared: PreparedConversion) -> None:
        PlanReport.__init__(self, plan)
        result = prepared.result
        object.__setattr__(self, "_conversion_json", json.dumps({
            "artifact_purpose": prepared.artifact_purpose, "container": result.container,
            "artifact": result.artifact.as_dict() if result.artifact is not None else None,
            "manifest": [list(pair) for pair in result.manifest],
            "comment_base64": base64.b64encode(result.comment).decode("ascii"),
            "sources": [source.as_dict() for source in result.sources],
        }, ensure_ascii=False))

    def as_dict(self) -> dict[str, object]:
        result = PlanReport.as_dict(self)
        result["conversion"] = json.loads(self._conversion_json)
        return result


def release_conversion_artifacts(
    plan: OperationPlan, *, roots: tuple[LibraryRoot, ...], state_dir: Path,
    authorization: RunAuthorization,
) -> Decision | None:
    """Settle a retained native stage after apply or recovery has verified every source cleanup."""
    native_members = tuple(operation for operation in plan.operations if operation.kind == "publish"
                           and operation.artifact is not None and operation.artifact.source is not None
                           and operation.artifact.source.path.parent.name.startswith(_SCRATCH_PREFIX))
    if not native_members:
        return None
    if not authorization.apply:
        return Decision("planned", "conversion_artifact_cleanup_requires_apply")
    try:
        roots = normalize_library_roots(roots)
        store = StateStore(state_dir)
        record = store.get_operation(plan.operation_id)
        if record is None or record.phase != "complete" or record.plan.as_dict() != plan.as_dict():
            return Decision("review_required", "conversion_group_not_settled")
        members = {member.member_id: member for member in store.list_members(plan.operation_id)}
        for operation in plan.operations:
            if operation.kind == "quarantine":
                member = members.get(operation.member_id)
                if member is None or member.status != "complete" or member.cleanup_fingerprint is None or operation.source is None:
                    return Decision("review_required", "conversion_source_cleanup_unverified", operation.destination)
                retained = member.cleanup_fingerprint
                _source_root(operation.source, roots)
                _assert_source(retained)
                if (retained.sha256, retained.size) != (operation.source.sha256, operation.source.size):
                    return Decision("review_required", "conversion_retained_source_mismatch", retained.path)
        for operation in native_members:
            assert operation.artifact is not None and operation.artifact.source is not None
            artifact = operation.artifact.source
            if operation.source is None:
                return Decision("review_required", "conversion_source_association_missing", operation.destination)
            root = _source_root(operation.source, roots)
            if artifact.root != root.path:
                return Decision("review_required", "conversion_artifact_scope_mismatch", artifact.path)
            destination_root = next((item for item in roots if item.contains(operation.destination)), None)
            if destination_root is None:
                return Decision("review_required", "conversion_destination_outside_selected_roots", operation.destination)
            actual = fingerprint(operation.destination, destination_root.path)
            if (actual.sha256, actual.size) != (artifact.sha256, artifact.size):
                return Decision("review_required", "conversion_published_artifact_changed", operation.destination)
            directory = artifact.path.parent
            checked_path(directory, root.path, allow_missing=True)
            if not directory.exists():
                continue
            if not directory.name.startswith(_SCRATCH_PREFIX):
                return Decision("review_required", "conversion_scratch_not_owned", directory)
            info = directory.lstat()
            marker_path = directory / _MARKER
            marker = fingerprint(marker_path, root.path)
            if marker.size > _MARKER_MAX_BYTES:
                return Decision("review_required", "conversion_marker_invalid", marker_path)
            descriptor = os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                encoded = stream.read(_MARKER_MAX_BYTES + 1)
            if json.loads(encoded) != _marker_value(directory, plan.operation_id, operation.source):
                return Decision("review_required", "conversion_marker_identity_mismatch", marker_path)
            _assert_source(marker)
            scratch = _ConversionScratch(directory, root.path, plan.operation_id, operation.source,
                                         marker, (info.st_dev, info.st_ino), artifact)
            scratch.discard()
    except _WorkflowError as error:
        return error.decision
    except (StateStoreError, ValueError, UnicodeError):
        return Decision("review_required", "conversion_state_or_marker_invalid")
    except FilesystemError as error:
        return Decision("review_required", error.reason, error.path)
    except OSError:
        return Decision("review_required", "conversion_scratch_cleanup_failed")
    return None


def _existing_destination(header: HeaderObservation) -> FileFingerprint | None:
    path = header.source.path.with_suffix(".cbz")
    checked_path(path, header.source.root, allow_missing=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise _WorkflowError("conflict", "conversion_destination_not_regular", path)
    return fingerprint(path, header.source.root)


def _choose_replacement(path: Path, *, interactive: bool, cancel: Event) -> bool:
    if not interactive or not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    sys.stderr.write(f"Replace {str(path)!r} and retain its previous bytes? Type replace to authorize, or keep: ")
    sys.stderr.flush()
    try:
        return read_interactive_line(cancel, bytearray()) == "replace"
    except KeyboardInterrupt:
        raise _WorkflowError("cancelled", "interactive_choice_cancelled", path) from None


def _error_decision(error: _WorkflowError | WorkerError | OSError | StateStoreError) -> Decision:
    if isinstance(error, _WorkflowError):
        return error.decision
    if isinstance(error, WorkerError):
        statuses: dict[str, Status] = {
            "invalid_input": "invalid_input", "unsupported": "unsupported",
            "missing_dependency": "missing_dependency", "stale_plan": "stale_plan",
            "permission_denied": "permission_denied", "failed": "failed", "cancelled": "cancelled",
        }
        return Decision(statuses.get(error.status, "failed"), error.reason)
    if isinstance(error, StateStoreError):
        return Decision("review_required", "conversion_state_invalid")
    if isinstance(error, FilesystemError):
        status: Status = "stale_plan" if error.reason == "source_changed" else "invalid_input"
        return Decision(status, error.reason, error.path)
    if isinstance(error, PermissionError):
        return Decision("permission_denied", "conversion_permission_denied")
    return Decision("failed", "conversion_filesystem_failed")


def convert_releases(
    selected: Sequence[ReleaseSnapshot], *, roots: tuple[LibraryRoot, ...], archive_policy: ArchivePolicy,
    state_dir: Path, policy_version: str, authorization: RunAuthorization, cancel: Event,
    interactive: bool = False, parsing: ParsingPolicy | None = None,
    manual_rename: bool = False, rename_zip_to_cbz: bool = True,
) -> RunResult:
    """The focused convert service. Preview artifacts are never executable saved plans."""
    decisions: list[Decision] = []
    reports: list[PlanReport] = []
    headers: list[HeaderObservation] = []
    completed_sources: set[Path] = set()
    committed = skipped = failed = 0
    try:
        roots = normalize_library_roots(roots)
    except (TypeError, ValueError):
        return RunResult((Decision("invalid_input", "invalid_selected_roots"),))
    if not roots:
        return RunResult((Decision("invalid_input", "no_selected_roots"),))
    seen: set[Path] = set()
    zip_renames: list[ReleaseSnapshot] = []
    observations: dict[Path, HeaderObservation] = {}
    for release in selected:
        if release.path in seen:
            continue
        seen.add(release.path)
        try:
            if cancel.is_set():
                raise _WorkflowError("cancelled", "shutdown_requested", release.path)
            source = release.observation.fingerprint
            if source is None or source.path != release.path:
                raise _WorkflowError("invalid_input", "source_fingerprint_required", release.path)
            root = _source_root(source, roots)
            _assert_source(source)
            header = observe_header(source.path, root=root.path, policy=archive_policy, cancel=cancel)
            if header.source != source:
                raise _WorkflowError("stale_plan", "source_fingerprint_changed", source.path)
            observations[source.path] = header
            if header.container == "zip" and source.path.suffix.lower() == ".zip" and rename_zip_to_cbz:
                zip_renames.append(release)
            elif header.container in {"zip", "epub"}:
                decisions.append(Decision("skipped", "already_zip_container" if header.container == "zip"
                                          else "epub_not_converted", source.path))
                skipped += 1
            elif source.path.suffix.lower() == ".cbz":
                decisions.append(Decision("review_required", "header_correction_required", source.path))
            else:
                headers.append(header)
        except (_WorkflowError, WorkerError, OSError) as error:
            decision = _error_decision(error)
            decisions.append(decision)
            failed += int(decision.status in {"failed", "permission_denied", "publication_failed"})
            if decision.status == "cancelled":
                break
    if zip_renames and not cancel.is_set():
        from .header_correction import correct_headers
        renamed = correct_headers(
            zip_renames, roots=roots, parsing=parsing or ParsingPolicy(), archive_policy=archive_policy,
            state_dir=state_dir, policy_version=policy_version, authorization=authorization,
            cancel=cancel, interactive=interactive, manual_rename=manual_rename, zip_only=True, observations=observations,
        )
        decisions.extend(renamed.decisions)
        reports.extend(PlanReport(plan) for plan in renamed.plans)
        committed += renamed.committed
        skipped += renamed.skipped
        failed += renamed.failed
    destinations: dict[Path, list[HeaderObservation]] = {}
    for header in headers:
        destinations.setdefault(header.source.path.with_suffix(".cbz"), []).append(header)
    executor = TransactionExecutor(state_dir, tuple(root.path for root in roots), cancel=cancel,
                                   max_stage_bytes=archive_policy.conversion_scratch_max_bytes) if authorization.apply else None
    for header in headers:
        source = header.source
        if source.path in completed_sources:
            decisions.append(Decision("skipped", "multipart_member_already_handled", source.path))
            continue
        destination = source.path.with_suffix(".cbz")
        if len(destinations[destination]) != 1:
            decisions.append(Decision("conflict", "multiple_archives_share_conversion_destination", source.path,
                                      tuple(str(item.source.path) for item in destinations[destination])))
            continue
        try:
            if cancel.is_set():
                raise _WorkflowError("cancelled", "shutdown_requested", source.path)
            before = _existing_destination(header)
            replace_existing = False
            if before is not None:
                with prepare_conversion(header, roots=roots, archive_policy=archive_policy,
                                        authorization=RunAuthorization(), cancel=cancel) as probe:
                    if probe.result.status != "valid":
                        decision = Decision(probe.result.status, probe.result.reason, source.path)
                        decisions.append(decision)
                        failed += int(decision.status in {"failed", "permission_denied", "publication_failed"})
                        continue
                    artifact = probe.result.artifact
                    assert artifact is not None
                    _assert_source(before)
                    if (before.sha256, before.size) == (artifact.sha256, artifact.size):
                        completed_sources.update(part.path for part in probe.result.sources)
                        decisions.append(Decision("skipped", "verified_identical_cbz_sources_retained", source.path,
                                                  (before.sha256,)))
                        skipped += 1
                        continue
                replace_existing = _choose_replacement(destination, interactive=interactive, cancel=cancel)
                if not replace_existing:
                    decisions.append(Decision("conflict", "conversion_destination_exists", source.path,
                                              (str(destination), before.sha256)))
                    continue
            if executor is not None:
                pending = executor.state_store.list_operations(phases=(
                    "planned", "staging", "staged", "publishing", "committed", "cleanup_pending", "needs_review", "failed",
                ))
                if pending:
                    decisions.append(Decision("review_required", "another_operation_requires_recovery", source.path,
                                              tuple(record.operation_id for record in pending)))
                    continue
            prepared: PreparedConversion
            with prepare_conversion(header, roots=roots, archive_policy=archive_policy,
                                    authorization=authorization, cancel=cancel) as prepared:
                result = prepared.result
                if result.status != "valid":
                    decision = Decision(result.status, result.reason, source.path)
                    decisions.append(decision)
                    failed += int(decision.status in {"failed", "permission_denied", "publication_failed"})
                    continue
                completed_sources.update(part.path for part in result.sources)
                plan = build_conversion_plan(prepared, policy_version=policy_version,
                                             destination_before=before if replace_existing else None)
                reports.append(ConversionPlanReport(plan, prepared))
                if executor is None:
                    decisions.append(Decision("planned", "verified_conversion_preview_requires_regeneration", source.path))
                    continue
                prepared.retain_for_recovery()
                outcome = executor.apply(plan, authorization=authorization)
                decisions.extend(outcome.decisions)
                committed += outcome.committed
                skipped += outcome.skipped
                failed += outcome.failed
                record = executor.state_store.get_operation(plan.operation_id)
                if record is None:
                    assert prepared._scratch is not None
                    prepared._scratch.lifecycle = "temporary"
                elif record.phase == "complete":
                    settlement = release_conversion_artifacts(
                        plan, roots=roots, state_dir=state_dir, authorization=authorization,
                    )
                    if settlement is None:
                        assert prepared._scratch is not None
                        prepared._scratch.lifecycle = "settled"
                    else:
                        decisions.append(settlement)
        except (_WorkflowError, WorkerError, OSError, StateStoreError) as error:
            decision = _error_decision(error)
            decisions.append(decision)
            failed += int(decision.status in {"failed", "permission_denied", "publication_failed"})
            if decision.status == "cancelled":
                break
    return RunResult(tuple(decisions), committed=committed, skipped=skipped, failed=failed,
                     plan_reports=tuple(reports))

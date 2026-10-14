"""Recoverable publication of authorized, immutable filesystem operation plans."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.operations import ArtifactSpec, FileFingerprint, Operation, OperationPlan
from .filesystem import (
    FilesystemError,
    checked_path,
    fingerprint,
    matches,
    sync_directory,
)
from .state import (
    LockOwner,
    MemberRecord,
    OperationRecord,
    StateStore,
    StateStoreError,
)

Checkpoint = Callable[[str, str], None]
FailureStatus = Literal[
    "unsupported", "invalid_input", "stale_plan", "conflict", "permission_denied",
    "publication_failed", "review_required", "cancelled", "failed",
]

_LOCK_FORMAT = "kce-lock-v1"
_LOCK_NAME = ".kce-transaction.lock"
_RESERVED_NAMES = frozenset({
    ".kce-transaction.lock", "apply.lock", "state.sqlite3", "state.sqlite3-journal",
    "state.sqlite3-wal", "state.sqlite3-shm",
})
_COPY_CHUNK = 1024 * 1024


class _Failure(Exception):
    def __init__(self, status: FailureStatus, reason: str, path: Path | None = None):
        self.status: FailureStatus = status
        self.reason = reason
        self.path = path
        super().__init__(reason)


class _BusyLock(_Failure):
    def __init__(self, path: Path):
        super().__init__("review_required", "cooperating_writer_lock_held", path)


class _Cancelled(_Failure):
    def __init__(self) -> None:
        super().__init__("cancelled", "operation_cancelled")


@dataclass(slots=True)
class _HeldLock:
    path: Path
    descriptor: int
    store_id: str
    owner: LockOwner | None


@dataclass(frozen=True, slots=True)
class _PlanAccess:
    permissions: dict[str, Path]
    source_roots: dict[str, Path]
    stage_parents: dict[str, Path]
    directory_identities: dict[str, tuple[int, int]]

class TransactionExecutor:
    """Apply and recover operation plans under explicit authorization.

    Cooperating writers serialize through the local state lock and one lock per
    configured physical root. Those locks do not exclude non-cooperating writers;
    observations are rechecked close to every effect to minimize that residual
    race without claiming an atomic transaction against external processes.
    """

    def __init__(
        self,
        state_dir: Path,
        roots: tuple[Path, ...],
        *,
        cancel: threading.Event | None = None,
        checkpoint: Checkpoint | None = None,
        max_stage_bytes: int = 8_589_934_592,
    ) -> None:
        if not state_dir.is_absolute() or ".." in state_dir.parts:
            raise ValueError("state directory must be an absolute canonical path")
        normalized_roots: list[Path] = []
        for root in roots:
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError("configured roots must be absolute canonical paths")
            absolute = Path(os.path.abspath(root))
            if absolute not in normalized_roots:
                normalized_roots.append(absolute)
        self.state_dir = Path(os.path.abspath(state_dir))
        self.roots = tuple(normalized_roots)
        self.cancel = cancel if cancel is not None else threading.Event()
        self.checkpoint = checkpoint
        if type(max_stage_bytes) is not int or max_stage_bytes <= 0:
            raise ValueError("staging budget must be a positive integer")
        self.max_stage_bytes = max_stage_bytes
        self.state_store = StateStore(self.state_dir)
        self._active_operation_id: str | None = None

    def prepare_state_root(self, *, authorization: RunAuthorization) -> Decision:
        if not authorization.apply:
            return Decision("planned", "state_root_preparation_requires_apply", self.state_dir)
        if self.cancel.is_set():
            return Decision("cancelled", "shutdown_requested", self.state_dir)
        held: list[_HeldLock] = []
        result = Decision("observed", "state_root_prepared", self.state_dir)
        try:
            if self.state_dir == Path(self.state_dir.anchor):
                raise _Failure("permission_denied", "filesystem_root_cannot_be_state", self.state_dir)
            held.append(self._acquire_state_before_database("state-root-preparation", recovering=False))
        except _Failure as error:
            result = Decision(error.status, error.reason, error.path)
        except (OSError, StateStoreError) as error:
            result = Decision(self._status_for_error(error), self._safe_error(error), self.state_dir)
        finally:
            release_error = self._release_locks(held, None)
            if release_error is not None and result.status == "observed":
                result = Decision("failed", "state_root_lock_release_failed", self.state_dir)
        return result

    def apply(self, plan: OperationPlan, *, authorization: RunAuthorization) -> RunResult:
        if not authorization.apply:
            try:
                self._validate_plan(plan, check_preconditions=True)
            except _Failure as error:
                return self._result(plan, error.status, error.reason, path=error.path)
            except (OSError, StateStoreError) as error:
                return self._result(plan, self._status_for_error(error), self._safe_error(error))
            return self._result(plan, "planned", "preview_only")
        if not plan.operations:
            return self._result(plan, "skipped", "empty_plan", skipped=1)

        try:
            access = self._validate_plan(plan, check_preconditions=True)
        except _Failure as error:
            return self._result(plan, error.status, error.reason, path=error.path)
        except (OSError, StateStoreError) as error:
            return self._result(plan, self._status_for_error(error), self._safe_error(error))

        held: list[_HeldLock] = []
        operation_recorded = False
        operation_owned = False
        owner = self._current_owner(plan.operation_id)
        result: RunResult | None = None
        store_id = ""
        try:
            state_lock = self._acquire_state_before_database(plan.operation_id, recovering=False)
            held.append(state_lock)
            store_id = state_lock.store_id
            existing = self.state_store.get_operation(plan.operation_id)
            if existing is not None:
                if existing.phase == "complete":
                    result = self._result(plan, "skipped", "operation_already_complete", skipped=1)
                else:
                    result = self._result(plan, "review_required", "operation_id_requires_recovery")
            else:
                pending = self.state_store.list_operations(
                    phases=("planned", "staging", "staged", "publishing", "committed", "cleanup_pending", "needs_review", "failed")
                )
                if pending:
                    raise _Failure("review_required", "another_operation_requires_recovery")

                for root in self._lock_roots_for_plan(plan):
                    root_lock = self._acquire_lock(
                        root / _LOCK_NAME, store_id, operation_id=plan.operation_id,
                        recovering=False, record=None,
                    )
                    held.append(root_lock)
                self.state_store.record_plan(plan, owner=owner)
                operation_recorded = True
                operation_owned = True
                self._active_operation_id = plan.operation_id
                self._write_lock(state_lock, owner)
                for root_lock in held[1:]:
                    self._write_lock(root_lock, owner)

                self._initialize_member_aux(plan, access)
                result = self._execute(plan, access)
        except _Failure as error:
            if operation_recorded:
                self._record_error(plan.operation_id, error.reason)
            result = self._result(plan, error.status, error.reason, path=error.path)
        except (OSError, StateStoreError) as error:
            if operation_recorded:
                self._record_error(plan.operation_id, self._safe_error(error))
            result = self._result(plan, self._status_for_error(error), self._safe_error(error))
        except Exception as error:
            if operation_recorded:
                self._record_error(plan.operation_id, f"executor_error:{type(error).__name__}")
            result = self._result(plan, "publication_failed", f"executor_error:{type(error).__name__}")
        finally:
            release_error = self._release_locks(held, plan.operation_id if operation_owned else None)
            self._active_operation_id = None
            if release_error is not None:
                if result is None:
                    result = self._result(plan, "publication_failed", "lock_release_failed")
                else:
                    result = replace(
                        result, failed=result.failed + 1,
                        decisions=(*result.decisions, Decision("publication_failed", "lock_release_failed")),
                    )
        if result is None:
            return self._result(plan, "failed", "operation_did_not_produce_result")
        return result

    def recover(self, operation_id: str, *, authorization: RunAuthorization) -> RunResult:
        if not operation_id or len(operation_id) > 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in operation_id
        ):
            return self._result(None, "invalid_input", "invalid_operation_id")
        record: OperationRecord | None = None
        held: list[_HeldLock] = []
        operation_owned = False
        result: RunResult | None = None
        try:
            self._validate_state_path()
            if authorization.apply:
                if not self.state_store.path.exists():
                    return self._result(None, "invalid_input", "operation_not_found")
                held.append(self._acquire_state_before_database(operation_id, recovering=True))
            record = self.state_store.get_operation(operation_id)
            if record is None:
                return self._result(None, "invalid_input", "operation_not_found")
            if not authorization.apply:
                if record.phase == "complete":
                    return self._result(record.plan, "skipped", "already_complete", skipped=1)
                if record.phase == "needs_review":
                    return self._result(record.plan, "review_required", record.error or "operation_needs_review")
                return self._result(record.plan, "planned", "recovery_requires_apply")
            state_lock = held[0]
            if state_lock.owner is not None and not self._known_owner(operation_id, state_lock.owner):
                raise _Failure("review_required", "lock_owner_not_recorded_in_journal", state_lock.path)
            access = self._validate_plan(record.plan, check_preconditions=False, recovery=True)
            for root in self._lock_roots_for_plan(record.plan):
                held.append(self._acquire_lock(
                    root / _LOCK_NAME, state_lock.store_id, operation_id=operation_id,
                    recovering=True, record=record,
                ))
            owner = self._current_owner(operation_id)
            self.state_store.update_owner(operation_id, owner)
            operation_owned = True
            self._active_operation_id = operation_id
            for lock in held:
                self._write_lock(lock, owner)
            if record.phase == "needs_review":
                result = self._result(record.plan, "review_required", record.error or "operation_needs_review")
            elif record.phase == "complete":
                members = {member.member_id: member for member in self.state_store.list_members(operation_id)}
                self._verify_committed_group(record.plan, members, access)
                result = self._result(record.plan, "skipped", "already_complete", skipped=1)
            else:
                result = self._execute(record.plan, access)
        except _Failure as error:
            if operation_owned:
                if error.status != "cancelled":
                    self._mark_needs_review(operation_id, error.reason)
                self._record_error(operation_id, error.reason)
            result = self._result(record.plan if record else None, error.status, error.reason, path=error.path)
        except (OSError, StateStoreError) as error:
            if operation_owned:
                self._record_error(operation_id, self._safe_error(error))
            result = self._result(record.plan if record else None, self._status_for_error(error), self._safe_error(error))
        except Exception as error:
            reason = f"executor_error:{type(error).__name__}"
            if operation_owned:
                self._record_error(operation_id, reason)
            result = self._result(record.plan if record else None, "publication_failed", reason)
        finally:
            release_error = self._release_locks(held, operation_id if operation_owned else None)
            self._active_operation_id = None
            if release_error is not None:
                if result is None:
                    result = self._result(record.plan if record else None, "publication_failed", "lock_release_failed")
                else:
                    result = replace(
                        result, failed=result.failed + 1,
                        decisions=(*result.decisions, Decision("publication_failed", "lock_release_failed")),
                    )
        return result if result is not None else self._result(None, "failed", "recovery_did_not_produce_result")

    def _lock_roots_for_plan(self, plan: OperationPlan) -> tuple[Path, ...]:
        affected: set[Path] = set()
        for operation in plan.operations:
            for path in (
                operation.destination,
                operation.source.path if operation.kind in {"move", "quarantine"} and operation.source is not None else None,
            ):
                if path is None:
                    continue
                configured = self._configured_root_for(path)
                permission = self._permission_for(plan, path, write=True)
                if configured is None or permission is None:
                    raise _Failure("permission_denied", "lock_path_not_authorized_for_write", path)
                root = configured if self._permission_for(plan, configured, write=True) is not None else permission
                affected.add(root)
        return self._ordered_roots(tuple(affected))

    def _execute(self, plan: OperationPlan, access: _PlanAccess) -> RunResult:
        record = self.state_store.get_operation(plan.operation_id)
        if record is None:
            raise StateStoreError("operation is not journaled")
        members = {member.member_id: member for member in self.state_store.list_members(plan.operation_id)}
        if record.phase in {"committed", "cleanup_pending"}:
            self._verify_committed_group(plan, members, access)
            return self._finish_cleanup(plan, members, access, committed=0)
        if record.phase == "complete":
            return self._result(plan, "skipped", "already_complete", skipped=1)
        if record.phase == "needs_review":
            return self._result(plan, "review_required", record.error or "operation_needs_review")

        if record.phase in {"planned", "staging", "failed"}:
            self.state_store.set_phase(plan.operation_id, "staging")
        try:
            for operation in plan.operations:
                member = members[operation.member_id]
                if operation.kind in {"publish", "move"}:
                    member = self._ensure_stage(operation, member, access)
                    members[operation.member_id] = member
                elif member.status == "planned":
                    staged = replace(member, status="staged", error=None)
                    self.state_store.save_member(staged)
                    members[operation.member_id] = staged
                    self._checkpoint("staged", operation.member_id)
            if record.phase != "publishing":
                self.state_store.set_phase(plan.operation_id, "staged")
            self.state_store.set_phase(plan.operation_id, "publishing")
        except _Failure as error:
            if error.status in {"stale_plan", "conflict", "permission_denied", "unsupported", "review_required"}:
                self._rollback_published(plan, members, access)
                self._mark_needs_review(plan.operation_id, error.reason)
                return self._result(plan, "unsupported" if error.status == "unsupported" else "review_required", error.reason, path=error.path)
            raise
        try:
            for operation in plan.operations:
                member = members[operation.member_id]
                if member.status in {"published", "committed", "complete", "cleanup_pending"}:
                    if operation.kind in {"publish", "move", "mkdir"}:
                        self._verify_member_publication(operation, member, access)
                    continue
                if operation.kind in {"publish", "move"}:
                    member = self._publish_file(operation, member, access, plan=plan, members=members)
                elif operation.kind == "mkdir":
                    member = self._publish_directory(operation, member, access)
                else:
                    # Source cleanup and rmdir happen only after the group commit.
                    member = replace(member, status="staged", error=None)
                    self.state_store.save_member(member)
                members[operation.member_id] = member
            for operation in plan.operations:
                self._revalidate_publication_source(operation, access, plan, members)
            payloads: tuple[tuple[str, str, dict[str, object]], ...] = tuple(
                (
                    operation.member_id,
                    "local_commit",
                    {
                        "member_id": operation.member_id,
                        "kind": operation.kind,
                        "destination": str(operation.destination),
                        "reason": operation.reason,
                    },
                )
                for operation in plan.operations
            )
            self.state_store.commit_group(plan.operation_id, event_payloads=payloads)
            self._checkpoint("group_committed", "")
            members = {member.member_id: member for member in self.state_store.list_members(plan.operation_id)}
            return self._finish_cleanup(plan, members, access, committed=1)
        except _Failure as error:
            current = self.state_store.get_operation(plan.operation_id)
            if current is not None and current.phase in {"committed", "cleanup_pending"}:
                self.state_store.update_error(plan.operation_id, error.reason)
                return self._result(plan, "review_required", "cleanup_pending", path=error.path, committed=1)
            if error.status in {"stale_plan", "conflict", "permission_denied", "unsupported", "review_required"}:
                self._rollback_published(plan, members, access)
                self._mark_needs_review(plan.operation_id, error.reason)
                return self._result(plan, "unsupported" if error.status == "unsupported" else "review_required", error.reason, path=error.path)
            raise
        except (OSError, StateStoreError) as error:
            current = self.state_store.get_operation(plan.operation_id)
            if current is not None and current.phase in {"committed", "cleanup_pending"}:
                reason = self._safe_error(error)
                self._record_error(plan.operation_id, reason)
                return self._result(plan, "review_required", "cleanup_pending", committed=1)
            raise
        except Exception as error:
            current = self.state_store.get_operation(plan.operation_id)
            if current is not None and current.phase in {"committed", "cleanup_pending"}:
                self._record_error(plan.operation_id, f"postcommit_error:{type(error).__name__}")
                return self._result(plan, "review_required", "cleanup_pending", committed=1)
            raise

    def _ensure_stage(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> MemberRecord:
        if self.cancel.is_set():
            raise _Cancelled()
        artifact = self._artifact(operation)
        if artifact is None:
            raise _Failure("invalid_input", "publication_artifact_missing", operation.destination)
        if member.status in {"publishing", "published", "committed", "complete"}:
            current_target = self._current_target(operation, member, access)
            if current_target is not None:
                self._sync_existing_file(operation.destination)
                reconciled = replace(member, status="published", published_fingerprint=current_target, error=None)
                self.state_store.save_member(reconciled)
                return reconciled
        if member.stage_path is not None and member.stage_path.exists():
            actual = self._safe_fingerprint(member.stage_path, access.permissions[operation.member_id])
            if member.stage_fingerprint is not None and actual != member.stage_fingerprint:
                raise _Failure("stale_plan", "staged_identity_changed", member.stage_path)
            if actual.size != artifact.size or actual.sha256 != artifact.digest:
                aux = member.aux or {}
                owns_partial = (
                    member.status == "staging"
                    and member.stage_fingerprint is None
                    and type(aux.get("stage_device")) is int
                    and type(aux.get("stage_inode")) is int
                    and (actual.device, actual.inode) == (aux["stage_device"], aux["stage_inode"])
                )
                if not owns_partial:
                    raise _Failure("review_required", "staged_bytes_do_not_match_plan", member.stage_path)
                os.unlink(member.stage_path)
                sync_directory(member.stage_path.parent)
                aux = dict(aux)
                aux.pop("stage_device", None)
                aux.pop("stage_inode", None)
                member = replace(member, stage_path=None, stage_fingerprint=None, aux=aux, status="staging")
                self.state_store.save_member(member)
            else:
                assert member.stage_path is not None
                self._sync_existing_file(member.stage_path)
                synced = self._safe_fingerprint(member.stage_path, access.permissions[operation.member_id])
                if synced != actual:
                    raise _Failure("review_required", "staged_identity_changed_during_sync", member.stage_path)
                actual = synced
                if member.stage_fingerprint is None:
                    aux = member.aux or {}
                    if (actual.device, actual.inode) != (aux.get("stage_device"), aux.get("stage_inode")):
                        raise _Failure("review_required", "staged_identity_not_journaled", member.stage_path)
                    member = replace(member, stage_fingerprint=actual, status="staged", error=None)
                    self.state_store.save_member(member)
                if member.status == "staging":
                    member = replace(member, stage_fingerprint=actual, status="staged", error=None)
                    self.state_store.save_member(member)
                self._checkpoint("staged", operation.member_id)
                return member
        if member.status in {"publishing", "published", "committed", "complete"}:
            raise _Failure("review_required", "published_member_identity_mismatch", operation.destination)

        stage_parent = Path(str((member.aux or {}).get("stage_parent", access.stage_parents[operation.member_id])))
        self._assert_existing_directory(stage_parent, access.permissions[operation.member_id])
        token = secrets.token_hex(16)
        stage_path = stage_parent / f".kce-stage-{self._member_token(operation)}-{token}"
        member = replace(
            member,
            stage_path=stage_path,
            stage_fingerprint=None,
            status="staging",
            error=None,
            aux={**(member.aux or {}), "stage_parent": str(stage_parent), "stage_token": token},
        )
        self.state_store.save_member(member)
        self._checkpoint("before_stage", operation.member_id)
        member = self._write_stage(stage_path, artifact, member, access)
        sync_directory(stage_parent)
        actual = self._safe_fingerprint(stage_path, access.permissions[operation.member_id])
        if actual.size != artifact.size or actual.sha256 != artifact.digest:
            raise _Failure("publication_failed", "staged_artifact_verification_failed", stage_path)
        member = replace(member, stage_fingerprint=actual, status="staged", error=None)
        self.state_store.save_member(member)
        self._checkpoint("staged", operation.member_id)
        return member

    def _write_stage(
        self,
        path: Path,
        artifact: ArtifactSpec,
        member: MemberRecord,
        access: _PlanAccess,
    ) -> MemberRecord:
        self._checked_internal_path(path, access.permissions[member.member_id], allow_missing=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise _Failure("review_required", "staging_path_already_exists", path) from None
        digest = hashlib.sha256()
        total = 0
        try:
            info = os.fstat(descriptor)
            member = replace(
                member,
                aux={
                    **(member.aux or {}),
                    "stage_device": info.st_dev,
                    "stage_inode": info.st_ino,
                },
            )
            self.state_store.save_member(member)
            if artifact.content is not None:
                content = artifact.content
                for start in range(0, len(content), _COPY_CHUNK):
                    if self.cancel.is_set():
                        raise _Cancelled()
                    chunk = content[start : start + _COPY_CHUNK]
                    self._write_all(descriptor, chunk)
                    digest.update(chunk)
                    total += len(chunk)
            else:
                source = artifact.source
                if source is None:
                    raise _Failure("invalid_input", "artifact_source_missing", path)
                self._copy_fingerprint_source(descriptor, source, digest.update, digest.hexdigest)
                total = source.size
            if total != artifact.size or digest.hexdigest() != artifact.digest:
                raise _Failure("stale_plan", "source_bytes_changed_during_staging", path)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return member

    def _copy_fingerprint_source(
        self,
        destination_fd: int,
        source: FileFingerprint,
        digest_update: Callable[[bytes], None],
        digest_value: Callable[[], str],
    ) -> None:
        self._checked_internal_path(source.path, source.root)
        expected = (source.device, source.inode, source.size, source.mtime_ns)
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        source_fd = os.open(source.path, flags)
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != expected:
                raise _Failure("stale_plan", "source_identity_changed", source.path)
            copied = 0
            while True:
                if self.cancel.is_set():
                    raise _Cancelled()
                chunk = os.read(source_fd, _COPY_CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > min(source.size, self.max_stage_bytes):
                    raise _Failure("invalid_input", "staging_byte_limit", source.path)
                self._write_all(destination_fd, chunk)
                digest_update(chunk)
            after = os.fstat(source_fd)
            current = source.path.lstat()
            identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity != expected or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                raise _Failure("stale_plan", "source_identity_changed_during_staging", source.path)
            if digest_value() != source.sha256:
                raise _Failure("stale_plan", "source_hash_changed_during_staging", source.path)
        finally:
            os.close(source_fd)


    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            offset += written

    def _publish_file(
        self, operation: Operation, member: MemberRecord, access: _PlanAccess, *,
        plan: OperationPlan, members: dict[str, MemberRecord],
    ) -> MemberRecord:
        operation_id = self._operation_id_for_member(operation)
        artifact = self._artifact(operation)
        if artifact is None or member.stage_path is None or member.stage_fingerprint is None:
            raise _Failure("publication_failed", "validated_stage_missing", operation.destination)
        self._prepare_backup(operation_id, operation, member, access)
        current = self._member(operation)
        if current is not None:
            member = current
        if member.stage_path is None or member.stage_fingerprint is None:
            raise _Failure("publication_failed", "validated_stage_missing", operation.destination)
        self._revalidate_publication_target(operation, member, access)
        if self.cancel.is_set():
            raise _Cancelled()
        member = replace(member, status="publishing", error=None)
        self.state_store.save_member(member)
        assert member.stage_path is not None and member.stage_fingerprint is not None
        self._checkpoint("before_publish", operation.member_id)
        self._revalidate_publication_source(operation, access, plan, members)
        for dependency in plan.operations:
            if dependency.kind == "quarantine" and operation.member_id in dependency.depends_on:
                self._revalidate_publication_source(dependency, access, plan, members)
        destination = operation.destination
        if operation.collision == "replace":
            if member.backup_path is None or member.backup_fingerprint is None:
                raise _Failure("publication_failed", "verified_replacement_backup_missing", destination)
            self._verify_backup(operation_id, operation, member, access)
            self._revalidate_observed_destination(operation, access)
            os.replace(member.stage_path, destination)
            if member.stage_path.parent != destination.parent:
                sync_directory(member.stage_path.parent)
        else:
            try:
                os.link(member.stage_path, destination, follow_symlinks=False)
            except FileExistsError:
                raise _Failure("conflict", "destination_appeared_before_publish", destination) from None
            except OSError as error:
                if error.errno in {errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EPERM, errno.ENOSYS}:
                    raise _Failure("unsupported", "durable_no_clobber_link_unsupported", destination) from error
                raise
        sync_directory(destination.parent)
        published = self._safe_fingerprint(destination, access.permissions[operation.member_id])
        if published.size != artifact.size or published.sha256 != artifact.digest:
            raise _Failure("publication_failed", "published_bytes_do_not_match_stage", destination)
        if not self._same_file_identity(published, member.stage_fingerprint):
            raise _Failure("review_required", "published_file_is_not_owned_stage", destination)
        self._checkpoint("published", operation.member_id)
        member = replace(member, status="published", published_fingerprint=published, error=None)
        self.state_store.save_member(member)
        self._checkpoint("member_committed", operation.member_id)
        return member

    def _revalidate_publication_source(
        self, operation: Operation, access: _PlanAccess, plan: OperationPlan,
        members: dict[str, MemberRecord],
    ) -> None:
        if operation.kind not in {"publish", "move", "quarantine"}:
            return
        artifact_source = operation.artifact.source if operation.artifact is not None else None
        sources = (operation.source,) if artifact_source == operation.source else (operation.source, artifact_source)
        for source in sources:
            if source is None:
                continue
            try:
                if matches(source):
                    continue
            except (OSError, FilesystemError):
                pass
            for producer in plan.operations:
                member = members[producer.member_id]
                if member.status not in {"published", "committed", "complete", "cleanup_pending"}:
                    continue
                if producer.destination_before == source and member.backup_fingerprint is not None:
                    self._verify_member_publication(producer, member, access)
                    self._verify_backup(plan.operation_id, producer, member, access)
                    break
            else:
                raise _Failure("stale_plan", "publication_source_fingerprint_changed", source.path)

    def _prepare_backup(self, operation_id: str, operation: Operation, member: MemberRecord, access: _PlanAccess) -> None:
        if operation.collision != "replace":
            return
        prior = operation.destination_before
        if prior is None:
            raise _Failure("invalid_input", "replacement_observation_missing", operation.destination)
        if member.backup_path is not None and member.backup_fingerprint is not None:
            self._verify_backup(operation_id, operation, member, access)
            return
        aux = dict(member.aux or {})
        directory = aux.get("backup_directory")
        token = aux.get("backup_token")
        if not isinstance(directory, str) or not isinstance(token, str):
            token = secrets.token_hex(16)
            directory = str(operation.destination.parent / f".kce-quarantine-{self._member_token(operation)}-{token}")
            aux["backup_directory"] = directory
            aux["backup_token"] = token
        backup_directory = Path(directory)
        backup_path = backup_directory / "backup"
        member = replace(member, backup_path=backup_path, aux=aux, status="publishing", error=None)
        self.state_store.save_member(member)
        self._ensure_owned_directory(
            operation_id,
            backup_directory,
            operation,
            access.permissions[operation.member_id],
            purpose="backup",
            token=token,
        )
        self._revalidate_observed_destination(operation, access)
        backup = self._retain_copy(
            operation.destination,
            backup_path,
            prior,
            access.permissions[operation.member_id],
        )
        backup = self._safe_fingerprint(backup_path, access.permissions[operation.member_id])
        if backup.size != prior.size or backup.sha256 != prior.sha256:
            raise _Failure("publication_failed", "replacement_backup_verification_failed", backup_path)
        member = replace(member, backup_fingerprint=backup, status="publishing", error=None)
        self.state_store.save_member(member)
        sync_directory(backup_directory)
        sync_directory(operation.destination.parent)
        self._checkpoint("backed_up", operation.member_id)

    def _copy_to_exclusive_backup(
        self,
        source_path: Path,
        backup_path: Path,
        expected: FileFingerprint,
        root: Path,
    ) -> None:
        self._checked_internal_path(backup_path, root, allow_missing=True)
        input_fd = os.open(source_path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        output_fd = -1
        try:
            before = os.fstat(input_fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                expected.device, expected.inode, expected.size, expected.mtime_ns
            ):
                raise _Failure("stale_plan", "replacement_target_changed_before_backup", source_path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                output_fd = os.open(backup_path, flags, 0o600)
            except FileExistsError:
                raise _Failure("review_required", "backup_path_already_exists", backup_path) from None
            digest = hashlib.sha256()
            count = 0
            while True:
                if self.cancel.is_set():
                    raise _Cancelled()
                chunk = os.read(input_fd, _COPY_CHUNK)
                if not chunk:
                    break
                if count + len(chunk) > min(expected.size, self.max_stage_bytes):
                    raise _Failure("invalid_input", "retention_byte_limit", source_path)
                self._write_all(output_fd, chunk)
                digest.update(chunk)
                count += len(chunk)
            after = os.fstat(input_fd)
            current = source_path.lstat()
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                expected.device, expected.inode, expected.size, expected.mtime_ns
            ) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                expected.device, expected.inode, expected.size, expected.mtime_ns
            ):
                raise _Failure("stale_plan", "replacement_target_changed_during_backup", source_path)
            if count != expected.size or digest.hexdigest() != expected.sha256:
                raise _Failure("stale_plan", "replacement_target_hash_changed_during_backup", source_path)
            os.fsync(output_fd)
        finally:
            os.close(input_fd)
            if output_fd >= 0:
                os.close(output_fd)
        sync_directory(backup_path.parent)
    def _retain_copy(
        self,
        source_path: Path,
        retained_path: Path,
        expected: FileFingerprint,
        authorized_root: Path,
    ) -> FileFingerprint:
        """Verify and durably retain an independent copy beneath an authorized root."""
        self._checked_internal_path(retained_path, authorized_root, allow_missing=True)
        try:
            retained_path.lstat()
        except FileNotFoundError:
            self._copy_to_exclusive_backup(source_path, retained_path, expected, authorized_root)
        self._sync_existing_file(retained_path)
        retained = self._safe_fingerprint(retained_path, authorized_root)
        if (
            retained.size != expected.size
            or retained.sha256 != expected.sha256
            or (retained.device, retained.inode) == (expected.device, expected.inode)
        ):
            raise _Failure("review_required", "retained_copy_identity_mismatch", retained_path)
        return retained


    def _publish_directory(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> MemberRecord:
        operation_id = self._operation_id_for_member(operation)
        destination = operation.destination
        token = (member.aux or {}).get("mkdir_token")
        if not isinstance(token, str):
            raise _Failure("invalid_input", "mkdir_owner_token_missing", destination)
        marker_value = (member.aux or {}).get("mkdir_marker")
        marker = Path(marker_value) if isinstance(marker_value, str) else destination / f".kce-owner-{self._member_token(operation)}"
        if destination.exists() or destination.is_symlink():
            if not self._is_owned_mkdir(operation_id, operation, member):
                raise _Failure("conflict", "directory_destination_appeared", destination)
            self._sync_directory_and_parent(destination)
            info = destination.lstat()
            aux = {**(member.aux or {}), "mkdir_device": info.st_dev, "mkdir_inode": info.st_ino, "mkdir_marker": str(marker)}
            member = replace(member, status="publishing", aux=aux)
            self.state_store.save_member(member)
            if marker.exists():
                if not self._marker_matches(marker, operation_id, operation, token, "mkdir"):
                    raise _Failure("review_required", "created_directory_marker_changed", marker)
                marker.unlink()
                sync_directory(destination)
            self._checkpoint("published", operation.member_id)
            member = replace(member, status="published", error=None)
            self.state_store.save_member(member)
            self._checkpoint("member_committed", operation.member_id)
            return member

        self._assert_parent_ready(destination.parent, access.permissions[operation.member_id])
        aux = {**(member.aux or {}), "mkdir_marker": str(marker)}
        member = replace(member, aux=aux, status="publishing")
        self.state_store.save_member(member)
        self._checkpoint("before_publish", operation.member_id)
        os.mkdir(destination, 0o755)
        self._write_owned_marker(marker, operation_id, operation, token, "mkdir")
        sync_directory(destination)
        sync_directory(destination.parent)
        info = destination.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise _Failure("review_required", "created_directory_identity_changed", destination)
        aux = {**aux, "mkdir_device": info.st_dev, "mkdir_inode": info.st_ino}
        member = replace(member, status="publishing", aux=aux)
        self.state_store.save_member(member)
        if not self._marker_matches(marker, operation_id, operation, token, "mkdir"):
            raise _Failure("review_required", "created_directory_marker_changed", marker)
        marker.unlink()
        sync_directory(destination)
        sync_directory(destination.parent)
        self._checkpoint("published", operation.member_id)
        member = replace(member, status="published", error=None)
        self.state_store.save_member(member)
        self._checkpoint("member_committed", operation.member_id)
        return member

    def _verify_member_publication(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> None:
        operation_id = self._operation_id_for_member(operation)
        if operation.kind in {"publish", "move"}:
            if not self._is_published(operation, member, access):
                raise _Failure("review_required", "published_member_identity_mismatch", operation.destination)
        elif operation.kind == "mkdir" and not self._is_owned_mkdir(operation_id, operation, member):
            raise _Failure("review_required", "published_directory_identity_mismatch", operation.destination)

    def _current_target(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> FileFingerprint | None:
        artifact = self._artifact(operation)
        if artifact is None or not operation.destination.exists() or operation.destination.is_symlink():
            return None
        try:
            current = self._safe_fingerprint(operation.destination, access.permissions[operation.member_id])
        except (OSError, FilesystemError, _Failure):
            return None
        if current.size != artifact.size or current.sha256 != artifact.digest:
            return None
        expected_identity = member.published_fingerprint or member.stage_fingerprint
        if expected_identity is None and member.stage_path is not None and member.stage_path.exists():
            try:
                expected_identity = self._safe_fingerprint(member.stage_path, access.permissions[operation.member_id])
            except (OSError, FilesystemError, _Failure):
                return None
        if expected_identity is None or not self._same_file_identity(current, expected_identity):
            return None
        if member.published_fingerprint is not None and current != member.published_fingerprint:
            return None
        return current

    def _is_published(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> bool:
        return self._current_target(operation, member, access) is not None

    def _is_owned_mkdir(self, operation_id: str, operation: Operation, member: MemberRecord) -> bool:
        destination = operation.destination
        if not destination.exists() or destination.is_symlink():
            return False
        aux = member.aux or {}
        try:
            info = destination.lstat()
        except OSError:
            return False
        device, inode = aux.get("mkdir_device"), aux.get("mkdir_inode")
        if type(device) is int and type(inode) is int:
            return info.st_dev == device and info.st_ino == inode and stat.S_ISDIR(info.st_mode)
        token = aux.get("mkdir_token")
        marker_value = aux.get("mkdir_marker")
        if not isinstance(token, str):
            return False
        marker = Path(marker_value) if isinstance(marker_value, str) else destination / f".kce-owner-{self._member_token(operation)}"
        return stat.S_ISDIR(info.st_mode) and self._marker_matches(marker, operation_id, operation, token, "mkdir")

    @staticmethod
    def _sync_existing_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _Failure("review_required", "published_target_not_regular_file", path)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        sync_directory(path.parent)

    def _sync_directory_and_parent(self, path: Path) -> None:
        sync_directory(path)
        sync_directory(path.parent)

    def _finish_cleanup(
        self, plan: OperationPlan, members: dict[str, MemberRecord], access: _PlanAccess, *,
        committed: Literal[0, 1],
    ) -> RunResult:
        record = self.state_store.get_operation(plan.operation_id)
        if record is None:
            raise StateStoreError("operation journal disappeared")
        for operation in plan.operations:
            member = members[operation.member_id]
            if member.stage_path is not None and member.stage_fingerprint is not None and member.stage_path.exists():
                self._verify_member_publication(operation, member, access)
                self._unlink_stage_if_linked(member.stage_path, operation.destination, member.stage_fingerprint)
        cleanups = [operation for operation in plan.operations if operation.kind in {"move", "quarantine", "rmdir"}]
        if not cleanups:
            self.state_store.set_phase(plan.operation_id, "complete")
            return self._result(plan, "committed", "local_commit_complete", committed=committed)
        if record.phase == "committed":
            self.state_store.set_phase(plan.operation_id, "cleanup_pending")
        for operation in cleanups:
            member = members[operation.member_id]
            if member.status == "complete":
                continue
            member = self._prepare_cleanup_intent(operation, member, plan, members, access)
            members[operation.member_id] = member
            if self.cancel.is_set():
                self.state_store.update_error(plan.operation_id, "cleanup_cancelled")
                return self._result(plan, "review_required", "cleanup_pending", committed=committed)
            self._checkpoint("before_cleanup", operation.member_id)
            if operation.kind in {"move", "quarantine"}:
                member = self._cleanup_source(operation, member, plan, members, access)
            else:
                member = self._cleanup_directory(operation, member, access)
            members[operation.member_id] = member
            self.state_store.save_member(member)
            self._checkpoint("cleanup_done", operation.member_id)
        updated = {member.member_id: member for member in self.state_store.list_members(plan.operation_id)}
        if all(updated[operation.member_id].status == "complete" for operation in cleanups):
            self.state_store.set_phase(plan.operation_id, "complete")
            return self._result(plan, "committed", "local_commit_complete", committed=committed)
        return self._result(plan, "review_required", "cleanup_pending", committed=committed)

    def _prepare_cleanup_intent(
        self,
        operation: Operation,
        member: MemberRecord,
        plan: OperationPlan,
        members: dict[str, MemberRecord],
        access: _PlanAccess,
    ) -> MemberRecord:
        if operation.kind == "rmdir":
            prepared = replace(
                member,
                status="cleanup_pending",
                cleanup_path=operation.destination,
                cleanup_fingerprint=None,
                error=None,
            )
            self.state_store.save_member(prepared)
            return prepared
        source = operation.source
        if source is None:
            raise _Failure("invalid_input", "cleanup_source_missing", operation.destination)
        reused_backup = self._backup_for_source(source, plan, members)
        if reused_backup is not None:
            backup_member, backup_path, backup_fingerprint = reused_backup
            if not self._backup_member_matches(backup_member, access):
                raise _Failure("review_required", "cycle_source_backup_mismatch", backup_path)
            prepared = replace(
                member,
                status="cleanup_pending",
                cleanup_path=backup_path,
                cleanup_fingerprint=backup_fingerprint,
                error=None,
            )
            self.state_store.save_member(prepared)
            return prepared

        source_root = access.source_roots[operation.member_id]
        aux = dict(member.aux or {})
        token = aux.get("cleanup_token")
        directory_value = aux.get("cleanup_directory")
        if not isinstance(token, str) or not isinstance(directory_value, str):
            token = secrets.token_hex(16)
            quarantine_parent = self._source_quarantine_parent(source, plan)
            self._assert_existing_directory(quarantine_parent, source_root)
            if source.path.exists() and quarantine_parent.lstat().st_dev != source.path.lstat().st_dev:
                raise _Failure("unsupported", "source_quarantine_parent_is_on_another_filesystem", quarantine_parent)
            directory_value = str(quarantine_parent / f".kce-quarantine-{self._member_token(operation)}-{token}")
            aux["cleanup_token"] = token
            aux["cleanup_directory"] = directory_value
        directory = Path(directory_value)
        cleanup_path = member.cleanup_path or directory / "source"
        prepared = replace(
            member,
            status="cleanup_pending",
            cleanup_path=cleanup_path,
            aux=aux,
            error=None,
        )
        self.state_store.save_member(prepared)
        return prepared

    def _cleanup_source(
        self,
        operation: Operation,
        member: MemberRecord,
        plan: OperationPlan,
        members: dict[str, MemberRecord],
        access: _PlanAccess,
    ) -> MemberRecord:
        operation_id = self._operation_id_for_member(operation)
        source = operation.source
        if source is None:
            raise _Failure("invalid_input", "cleanup_source_missing", operation.destination)
        source_root = access.source_roots[operation.member_id]
        reused_backup = self._backup_for_source(source, plan, members)
        if reused_backup is not None:
            backup_member, backup_path, backup_fingerprint = reused_backup
            if not self._backup_member_matches(backup_member, access):
                raise _Failure("review_required", "cycle_source_backup_mismatch", backup_path)
            return replace(
                member,
                status="complete",
                cleanup_path=backup_path,
                cleanup_fingerprint=backup_fingerprint,
                error=None,
            )

        cleanup_path = member.cleanup_path
        aux = dict(member.aux or {})
        token = aux.get("cleanup_token")
        directory_value = aux.get("cleanup_directory")
        if not isinstance(token, str) or not isinstance(directory_value, str):
            token = secrets.token_hex(16)
            quarantine_parent = self._source_quarantine_parent(source, plan)
            self._assert_existing_directory(quarantine_parent, source_root)
            if source.path.exists() and quarantine_parent.lstat().st_dev != source.path.lstat().st_dev:
                raise _Failure("unsupported", "source_quarantine_parent_is_on_another_filesystem", quarantine_parent)
            directory_value = str(quarantine_parent / f".kce-quarantine-{self._member_token(operation)}-{token}")
            aux["cleanup_token"] = token
            aux["cleanup_directory"] = directory_value
        directory = Path(directory_value)
        if cleanup_path is None:
            cleanup_path = directory / "source"
        member = replace(member, status="cleanup_pending", cleanup_path=cleanup_path, aux=aux, error=None)
        self.state_store.save_member(member)
        self._ensure_owned_directory(
            operation_id,
            directory,
            operation,
            source_root,
            purpose="source_cleanup",
            token=token,
        )

        if cleanup_path.exists() or cleanup_path.is_symlink():
            retained = self._retain_copy(source.path, cleanup_path, source, source_root)
            if source.path.exists():
                self._unlink_source_if_retained(source.path, cleanup_path, source, source_root)
                sync_directory(source.path.parent)
            return replace(member, status="complete", cleanup_fingerprint=retained, error=None)

        if not source.path.exists():
            backup = self._backup_for_source(source, plan, members)
            if backup is not None:
                backup_member, backup_path, backup_fp = backup
                if self._backup_member_matches(backup_member, access):
                    return replace(member, status="complete", cleanup_path=backup_path, cleanup_fingerprint=backup_fp, error=None)
            raise _Failure("review_required", "source_missing_without_verified_quarantine", source.path)
        if not matches(source):
            raise _Failure("review_required", "source_changed_before_cleanup", source.path)
        self._checked_internal_path(source.path, source.root)
        self._sync_existing_file(source.path)
        retained = self._retain_copy(source.path, cleanup_path, source, source_root)
        self._unlink_source_if_retained(source.path, cleanup_path, source, source_root)
        sync_directory(source.path.parent)
        return replace(member, status="complete", cleanup_fingerprint=retained, error=None)

    def _unlink_source_if_retained(
        self,
        source_path: Path,
        retained_path: Path,
        expected: FileFingerprint,
        authorized_root: Path,
    ) -> None:
        source_info = source_path.lstat()
        retained_info = retained_path.lstat()
        if (source_info.st_dev, source_info.st_ino) == (retained_info.st_dev, retained_info.st_ino):
            raise _Failure("review_required", "quarantine_is_not_an_independent_copy", source_path)
        if (source_info.st_dev, source_info.st_ino, source_info.st_size, source_info.st_mtime_ns) != (
            expected.device, expected.inode, expected.size, expected.mtime_ns
        ):
            raise _Failure("review_required", "source_changed_before_unlink", source_path)
        self._sync_existing_file(retained_path)
        retained = self._safe_fingerprint(retained_path, authorized_root)
        if retained.size != expected.size or retained.sha256 != expected.sha256:
            raise _Failure("review_required", "retained_source_hash_mismatch", retained_path)
        if not matches(expected):
            raise _Failure("review_required", "source_hash_changed_before_unlink", source_path)
        if self.cancel.is_set():
            raise _Cancelled()
        os.unlink(source_path)

    def _cleanup_directory(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> MemberRecord:
        path = operation.destination
        if not path.exists():
            return replace(member, status="complete", cleanup_path=path, cleanup_fingerprint=None, error=None)
        if path.is_symlink():
            raise _Failure("review_required", "directory_became_symlink", path)
        info = path.lstat()
        aux = member.aux or {}
        expected_device, expected_inode = aux.get("rmdir_device"), aux.get("rmdir_inode")
        if type(expected_device) is not int or type(expected_inode) is not int:
            raise _Failure("review_required", "directory_identity_not_journaled", path)
        if (info.st_dev, info.st_ino) != (expected_device, expected_inode) or not stat.S_ISDIR(info.st_mode):
            raise _Failure("review_required", "directory_identity_changed", path)
        with os.scandir(path) as entries:
            if next(entries, None) is not None:
                raise _Failure("review_required", "directory_not_empty", path)
        if self.cancel.is_set():
            raise _Cancelled()
        os.rmdir(path)
        sync_directory(path.parent)
        return replace(member, status="complete", cleanup_path=path, cleanup_fingerprint=None, error=None)

    def _verify_committed_group(self, plan: OperationPlan, members: dict[str, MemberRecord], access: _PlanAccess) -> None:
        record = self.state_store.get_operation(plan.operation_id)
        if record is None:
            raise StateStoreError("operation is not journaled")
        for operation in plan.operations:
            member = members[operation.member_id]
            if operation.collision == "replace":
                self._verify_backup(plan.operation_id, operation, member, access)
            if operation.kind in {"publish", "move"}:
                if not self._is_published(operation, member, access):
                    raise _Failure("review_required", "committed_target_identity_mismatch", operation.destination)
            elif operation.kind == "mkdir" and not self._is_owned_mkdir(plan.operation_id, operation, member):
                raise _Failure("review_required", "committed_directory_identity_mismatch", operation.destination)
            if operation.kind in {"move", "quarantine", "rmdir"}:
                if member.status == "complete":
                    self._verify_cleanup_record(plan, operation, member)
                elif record.phase == "complete":
                    raise _Failure("review_required", "completed_cleanup_member_not_settled", operation.destination)

    def _verify_cleanup_record(self, plan: OperationPlan, operation: Operation, member: MemberRecord) -> None:
        if operation.kind == "rmdir":
            if operation.destination.exists() or operation.destination.is_symlink():
                raise _Failure("review_required", "removed_directory_reappeared", operation.destination)
            return
        path = member.cleanup_path
        observed = member.cleanup_fingerprint
        if path is None or observed is None:
            raise _Failure("review_required", "cleanup_identity_missing", operation.destination)
        if self._configured_root_for(path) is None or self._permission_for(plan, path, write=False) is None:
            raise _Failure("review_required", "cleanup_path_outside_plan_roots", path)
        self._checked_internal_path(path, observed.root)
        actual = self._safe_fingerprint(path, observed.root)
        if actual != observed:
            raise _Failure("review_required", "retained_source_identity_mismatch", path)

    def _retain_uncommitted_output(
        self, plan: OperationPlan, operation: Operation, member: MemberRecord,
        access: _PlanAccess,
    ) -> None:
        published = member.published_fingerprint
        if published is None:
            return
        root = access.permissions[operation.member_id]
        aux = dict(member.aux or {})
        token = aux.get("rollback_token")
        directory = aux.get("rollback_directory")
        if not isinstance(token, str) or not isinstance(directory, str):
            token = secrets.token_hex(16)
            directory = str(operation.destination.parent / f".kce-quarantine-{self._member_token(operation)}-{token}")
        candidate_path = Path(directory) / "rollback-candidate"
        aux.update(rollback_token=token, rollback_directory=directory,
                   rollback_candidate_path=str(candidate_path))
        member = replace(member, aux=aux)
        self.state_store.save_member(member)
        self._ensure_owned_directory(plan.operation_id, Path(directory), operation, root,
                                     purpose="rollback", token=token)
        self._verify_member_publication(operation, member, access)
        if member.stage_path is None or member.stage_fingerprint is None:
            raise _Failure("review_required", "publication_generation_witness_missing", operation.destination)
        if self._safe_fingerprint(member.stage_path, root) != member.stage_fingerprint:
            raise _Failure("review_required", "publication_generation_witness_changed", member.stage_path)
        if not self._same_file_identity(published, member.stage_fingerprint):
            raise _Failure("review_required", "publication_generation_witness_mismatch", operation.destination)
        candidate = self._retain_copy(operation.destination, candidate_path, published, root)
        recorded = aux.get("rollback_candidate_fingerprint")
        if recorded is not None and recorded != candidate.as_dict():
            raise _Failure("review_required", "rollback_candidate_identity_changed", candidate_path)
        aux["rollback_candidate_fingerprint"] = candidate.as_dict()
        self.state_store.save_member(replace(member, aux=aux))
        if self._safe_fingerprint(operation.destination, root) != published:
            raise _Failure("review_required", "rollback_target_changed", operation.destination)
        os.unlink(operation.destination)
        if self._safe_fingerprint(member.stage_path, root) != member.stage_fingerprint:
            raise _Failure("review_required", "publication_generation_witness_changed", member.stage_path)
        os.unlink(member.stage_path)
        if member.stage_path.parent != operation.destination.parent:
            sync_directory(member.stage_path.parent)
        sync_directory(operation.destination.parent)

    def _rollback_published(
        self,
        plan: OperationPlan,
        members: dict[str, MemberRecord],
        access: _PlanAccess,
    ) -> None:
        """Retain uncommitted output before removing it or restoring prior content."""
        operation_id = plan.operation_id
        for operation in reversed(plan.operations):
            member = members.get(operation.member_id)
            if member is None:
                continue
            if operation.kind in {"publish", "move"} and self._is_published(operation, member, access):
                if operation.collision != "replace":
                    if operation.kind == "move":
                        continue
                    try:
                        self._retain_uncommitted_output(plan, operation, member, access)
                    except (OSError, FilesystemError, StateStoreError, _Failure):
                        continue
                    continue
                if member.backup_path is None or member.backup_fingerprint is None:
                    continue
                try:
                    self._verify_backup(operation_id, operation, member, access)
                    published = member.published_fingerprint
                    if published is None:
                        continue
                    authorized_root = access.permissions[operation.member_id]
                    candidate_path = member.backup_path.parent / "rollback-candidate"
                    aux = dict(member.aux or {})
                    recorded_path = aux.get("rollback_candidate_path")
                    if recorded_path is not None and recorded_path != str(candidate_path):
                        continue
                    aux["rollback_candidate_path"] = str(candidate_path)
                    member = replace(member, aux=aux)
                    self.state_store.save_member(member)
                    members[operation.member_id] = member

                    candidate = self._retain_copy(
                        operation.destination,
                        candidate_path,
                        published,
                        authorized_root,
                    )
                    recorded_fingerprint = (member.aux or {}).get("rollback_candidate_fingerprint")
                    if recorded_fingerprint is not None and recorded_fingerprint != candidate.as_dict():
                        continue
                    aux = {**(member.aux or {}), "rollback_candidate_fingerprint": candidate.as_dict()}
                    member = replace(member, aux=aux)
                    self.state_store.save_member(member)
                    members[operation.member_id] = member

                    current = self._safe_fingerprint(operation.destination, authorized_root)
                    if current != published:
                        continue
                    restore_temp = operation.destination.parent / (
                        f".kce-restore-{self._member_token(operation)}-{secrets.token_hex(8)}"
                    )
                    assert member.backup_path is not None and member.backup_fingerprint is not None
                    self._copy_to_exclusive_backup(
                        member.backup_path,
                        restore_temp,
                        member.backup_fingerprint,
                        authorized_root,
                    )
                    restored = self._safe_fingerprint(restore_temp, authorized_root)
                    prior = operation.destination_before
                    if (
                        prior is None
                        or restored.size != prior.size
                        or restored.sha256 != prior.sha256
                        or (restored.device, restored.inode)
                        == (member.backup_fingerprint.device, member.backup_fingerprint.inode)
                    ):
                        continue
                    current = self._safe_fingerprint(operation.destination, authorized_root)
                    if current != published:
                        continue
                    os.replace(restore_temp, operation.destination)
                    sync_directory(operation.destination.parent)
                except (OSError, FilesystemError, StateStoreError, _Failure):
                    # Ambiguity or a failed durability step keeps both retained candidates intact.
                    continue
            elif operation.kind == "mkdir" and self._is_owned_mkdir(operation_id, operation, member):
                with os.scandir(operation.destination) as entries:
                    if next(entries, None) is None:
                        os.rmdir(operation.destination)
                        sync_directory(operation.destination.parent)
    def _backup_for_source(
        self,
        source: FileFingerprint,
        plan: OperationPlan,
        members: dict[str, MemberRecord],
    ) -> tuple[MemberRecord, Path, FileFingerprint] | None:
        for operation in plan.operations:
            if operation.destination_before is None or not self._same_file_identity(operation.destination_before, source):
                continue
            exact_path = operation.destination == source.path
            case_only_path = (
                operation.kind == "move"
                and operation.destination.as_posix().casefold() == source.path.as_posix().casefold()
            )
            if not exact_path and not case_only_path:
                continue
            member = members.get(operation.member_id)
            if member is None or member.backup_path is None or member.backup_fingerprint is None:
                continue
            if case_only_path and not exact_path:
                if member.published_fingerprint is None or not source.path.exists():
                    continue
                try:
                    current_source = self._safe_fingerprint(source.path, source.root)
                except (OSError, FilesystemError, _Failure):
                    continue
                if not self._same_file_identity(current_source, member.published_fingerprint):
                    continue
            return member, member.backup_path, member.backup_fingerprint
        return None

    def _backup_member_matches(self, member: MemberRecord, access: _PlanAccess) -> bool:
        if member.backup_path is None or member.backup_fingerprint is None:
            return False
        try:
            return self._safe_fingerprint(member.backup_path, access.permissions[member.member_id]) == member.backup_fingerprint
        except (OSError, FilesystemError, _Failure):
            return False

    def _verify_backup(
        self,
        operation_id: str,
        operation: Operation,
        member: MemberRecord,
        access: _PlanAccess,
    ) -> None:
        prior = operation.destination_before
        if prior is None or member.backup_path is None or member.backup_fingerprint is None:
            raise _Failure("review_required", "replacement_backup_missing", operation.destination)
        directory = member.backup_path.parent
        token = str((member.aux or {}).get("backup_token", ""))
        if not self._owned_directory_matches(directory, operation_id, operation, token, "backup"):
            raise _Failure("review_required", "replacement_backup_directory_not_owned", directory)
        self._sync_existing_file(member.backup_path)
        actual = self._safe_fingerprint(member.backup_path, access.permissions[operation.member_id])
        if actual != member.backup_fingerprint or actual.size != prior.size or actual.sha256 != prior.sha256:
            raise _Failure("review_required", "replacement_backup_identity_mismatch", member.backup_path)

    def _revalidate_publication_target(self, operation: Operation, member: MemberRecord, access: _PlanAccess) -> None:
        self._assert_parent_ready(operation.destination.parent, access.permissions[operation.member_id])
        if operation.collision == "replace":
            if member.backup_path is None or member.backup_fingerprint is None:
                raise _Failure("publication_failed", "replacement_backup_not_durable", operation.destination)
            self._verify_backup(self._operation_id_for_member(operation), operation, member, access)
            self._revalidate_observed_destination(operation, access)
        else:
            if operation.destination.exists() or operation.destination.is_symlink():
                if self._is_published(operation, member, access):
                    return
                raise _Failure("conflict", "destination_no_longer_absent", operation.destination)

    def _revalidate_observed_destination(self, operation: Operation, access: _PlanAccess) -> None:
        expected = operation.destination_before
        if expected is None:
            raise _Failure("invalid_input", "replacement_observation_missing", operation.destination)
        if not operation.destination.exists() or operation.destination.is_symlink():
            raise _Failure("stale_plan", "replacement_target_missing_or_linked", operation.destination)
        actual = self._safe_fingerprint(operation.destination, access.permissions[operation.member_id])
        if actual != expected:
            raise _Failure("stale_plan", "replacement_target_changed", operation.destination)

    def _initialize_member_aux(self, plan: OperationPlan, access: _PlanAccess) -> None:
        for operation in plan.operations:
            member = self._member(operation)
            if member is None:
                raise StateStoreError("operation member is missing")
            aux = dict(member.aux or {})
            if operation.kind == "rmdir" and "rmdir_device" not in aux:
                identity = access.directory_identities.get(operation.member_id)
                if identity is None:
                    raise _Failure("stale_plan", "directory_observation_missing", operation.destination)
                aux["rmdir_device"], aux["rmdir_inode"] = identity
            if operation.kind == "mkdir" and "mkdir_token" not in aux:
                aux["mkdir_token"] = secrets.token_hex(16)
            if operation.kind in {"publish", "move"} and "stage_parent" not in aux:
                aux["stage_parent"] = str(access.stage_parents[operation.member_id])
            if aux != (member.aux or {}):
                self.state_store.save_member(replace(member, aux=aux))

    def _validate_plan(
        self,
        plan: OperationPlan,
        *,
        check_preconditions: bool,
        recovery: bool = False,
    ) -> _PlanAccess:
        if plan.schema_version != 1:
            raise _Failure("unsupported", "unsupported_operation_schema")
        if not self.roots:
            raise _Failure("invalid_input", "no_configured_roots")
        self._validate_configured_roots()
        self._validate_state_path()
        reserved_bytes = 0
        for operation in plan.operations:
            artifact = self._artifact(operation)
            reserved_bytes += artifact.size if artifact is not None else 0
            reserved_bytes += operation.destination_before.size if operation.destination_before is not None else 0
            if operation.kind in {"move", "quarantine"} and operation.source is not None:
                reserved_bytes += operation.source.size
            if reserved_bytes > self.max_stage_bytes:
                raise _Failure("invalid_input", "staging_byte_limit", operation.destination)

        for permission in plan.roots:
            self._clean_absolute(permission.path, "plan root")
            configured = self._configured_root_for(permission.path)
            if configured is None:
                raise _Failure("permission_denied", "plan_root_outside_configured_roots", permission.path)
            try:
                checked_path(permission.path, configured)
                info = permission.path.lstat()
            except OSError as error:
                raise self._from_os_error(error, permission.path) from error
            if not stat.S_ISDIR(info.st_mode):
                raise _Failure("invalid_input", "plan_root_not_directory", permission.path)

        if plan.operations and not plan.roots:
            raise _Failure("permission_denied", "operation_plan_has_no_roots")
        mkdir_by_path: dict[Path, str] = {
            operation.destination: operation.member_id for operation in plan.operations if operation.kind == "mkdir"
        }
        dependency_closure = self._dependency_closure(plan)
        permissions: dict[str, Path] = {}
        source_roots: dict[str, Path] = {}
        stage_parents: dict[str, Path] = {}
        directory_identities: dict[str, tuple[int, int]] = {}

        for operation in plan.operations:
            self._clean_absolute(operation.destination, "operation destination")
            destination_permission = self._permission_for(plan, operation.destination, write=True)
            if destination_permission is None:
                raise _Failure("permission_denied", "destination_not_authorized_for_write", operation.destination)
            permissions[operation.member_id] = destination_permission
            configured_destination_root = self._configured_root_for(operation.destination)
            if configured_destination_root is None:
                raise _Failure("permission_denied", "destination_outside_configured_roots", operation.destination)
            self._reject_reserved_destination(operation.destination)
            self._checked_internal_path(operation.destination, destination_permission, allow_missing=True)

            if operation.kind == "quarantine":
                if operation.source is None or operation.destination != operation.source.path:
                    raise _Failure("invalid_input", "quarantine_destination_must_be_source_path", operation.destination)
                if operation.artifact is not None or operation.destination_before is not None or operation.collision != "absent":
                    raise _Failure("invalid_input", "quarantine_has_incompatible_fields", operation.destination)
            elif operation.kind == "mkdir":
                if operation.source is not None or operation.artifact is not None or operation.destination_before is not None or operation.collision != "absent":
                    raise _Failure("invalid_input", "mkdir_has_incompatible_fields", operation.destination)
            elif operation.kind == "rmdir":
                if operation.source is not None or operation.artifact is not None or operation.destination_before is not None or operation.collision != "absent":
                    raise _Failure("invalid_input", "rmdir_has_incompatible_fields", operation.destination)
            elif operation.kind == "publish":
                if operation.artifact is None:
                    raise _Failure("invalid_input", "publication_artifact_missing", operation.destination)
            elif operation.kind == "move":
                if operation.source is None:
                    raise _Failure("invalid_input", "move_source_missing", operation.destination)
                if operation.artifact is not None and operation.artifact.source != operation.source:
                    raise _Failure("invalid_input", "move_artifact_conflicts_with_source", operation.destination)
                if operation.source.path == operation.destination:
                    raise _Failure("invalid_input", "move_source_equals_destination", operation.destination)

            if operation.kind in {"move", "quarantine"} and operation.source is not None:
                self._validate_fingerprint_scope(plan, operation.source, require_exists=check_preconditions)
                source_permission = self._permission_for(plan, operation.source.path, write=True)
                if source_permission is None:
                    raise _Failure("permission_denied", "source_not_authorized_for_cleanup", operation.source.path)
                source_roots[operation.member_id] = source_permission
                quarantine_parent = self._source_quarantine_parent(operation.source, plan)
                if not quarantine_parent.is_relative_to(source_permission):
                    raise _Failure("permission_denied", "source_quarantine_outside_plan_root", quarantine_parent)
                self._assert_existing_directory(quarantine_parent, source_permission)
                if not os.access(quarantine_parent, os.W_OK | os.X_OK):
                    raise _Failure("permission_denied", "source_quarantine_parent_not_writable", quarantine_parent)
                if check_preconditions and quarantine_parent.lstat().st_dev != operation.source.path.lstat().st_dev:
                    raise _Failure("unsupported", "source_quarantine_parent_is_on_another_filesystem", quarantine_parent)
                source_root = self._configured_root_for(operation.source.path)
                if source_root is None:
                    raise _Failure("permission_denied", "source_outside_configured_roots", operation.source.path)
                if check_preconditions and not matches(operation.source):
                    raise _Failure("stale_plan", "source_fingerprint_changed", operation.source.path)
            if operation.kind == "publish" and operation.source is not None:
                self._validate_fingerprint_scope(plan, operation.source, require_exists=check_preconditions)
                source_root = self._configured_root_for(operation.source.path)
                if source_root is None:
                    raise _Failure("permission_denied", "publication_source_outside_configured_roots", operation.source.path)
                if check_preconditions and not matches(operation.source):
                    raise _Failure("stale_plan", "publication_source_fingerprint_changed", operation.source.path)
            if operation.artifact is not None and operation.artifact.source is not None:
                self._validate_fingerprint_scope(plan, operation.artifact.source, require_exists=check_preconditions)
                source_root = self._configured_root_for(operation.artifact.source.path)
                if source_root is None:
                    raise _Failure("permission_denied", "artifact_source_outside_configured_roots", operation.artifact.source.path)
                if check_preconditions and not matches(operation.artifact.source):
                    raise _Failure("stale_plan", "artifact_source_fingerprint_changed", operation.artifact.source.path)

            if operation.kind == "quarantine":
                assert operation.source is not None
                if check_preconditions and not matches(operation.source):
                    raise _Failure("stale_plan", "quarantine_source_fingerprint_changed", operation.source.path)
                stage_parents[operation.member_id] = operation.destination.parent
                continue

            parent = operation.destination.parent
            stage_parent = self._nearest_existing_parent(parent, destination_permission, operation)
            stage_parents[operation.member_id] = stage_parent
            if operation.kind == "rmdir":
                if operation.destination == destination_permission or operation.destination in self.roots:
                    raise _Failure("permission_denied", "cannot_remove_authorized_root", operation.destination)
                try:
                    info = operation.destination.lstat()
                except FileNotFoundError:
                    if check_preconditions:
                        raise _Failure("stale_plan", "rmdir_target_missing", operation.destination) from None
                except OSError as error:
                    raise self._from_os_error(error, operation.destination) from error
                else:
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        raise _Failure("conflict", "rmdir_target_not_directory", operation.destination)
                    directory_identities[operation.member_id] = (info.st_dev, info.st_ino)
            elif operation.kind == "mkdir":
                if check_preconditions and (operation.destination.exists() or operation.destination.is_symlink()):
                    raise _Failure("conflict", "mkdir_target_already_exists", operation.destination)
            elif operation.kind in {"publish", "move"}:
                if operation.collision == "replace":
                    if operation.destination_before is None:
                        raise _Failure("invalid_input", "replacement_observation_missing", operation.destination)
                    self._validate_fingerprint_scope(plan, operation.destination_before, require_exists=check_preconditions)
                    if check_preconditions and not matches(operation.destination_before):
                        raise _Failure("stale_plan", "replacement_target_fingerprint_changed", operation.destination)
                elif operation.destination_before is not None:
                    raise _Failure("invalid_input", "absent_collision_has_destination_observation", operation.destination)
                if check_preconditions:
                    exists = operation.destination.exists() or operation.destination.is_symlink()
                    if operation.collision == "absent" and exists:
                        raise _Failure("conflict", "destination_expected_absent", operation.destination)
                    if operation.collision == "replace" and not exists:
                        raise _Failure("stale_plan", "replacement_target_missing", operation.destination)

        self._validate_missing_parent_dependencies(
            plan,
            mkdir_by_path,
            dependency_closure,
            recovery=recovery,
        )
        self._validate_rmdir_dependencies(plan, dependency_closure)
        return _PlanAccess(
            permissions,
            source_roots,
            stage_parents,
            directory_identities,
        )

    def _validate_missing_parent_dependencies(
        self,
        plan: OperationPlan,
        mkdir_by_path: dict[Path, str],
        dependencies: dict[str, frozenset[str]],
        *,
        recovery: bool = False,
    ) -> None:
        removed_directories = tuple(
            operation.destination for operation in plan.operations if operation.kind == "rmdir"
        )
        for operation in plan.operations:
            permission = self._permission_for(plan, operation.destination, write=True)
            if permission is None:
                continue
            current = operation.destination.parent
            while current != permission and current.is_relative_to(permission):
                try:
                    current.lstat()
                    break
                except FileNotFoundError:
                    if recovery and any(current.is_relative_to(path) for path in removed_directories):
                        current = current.parent
                        continue
                    maker = mkdir_by_path.get(current)
                    if maker is None or maker not in dependencies[operation.member_id]:
                        raise _Failure("conflict", "missing_parent_not_created_by_dependency", current)
                    current = current.parent
    def _validate_rmdir_dependencies(
        self,
        plan: OperationPlan,
        dependencies: dict[str, frozenset[str]],
    ) -> None:
        for directory in plan.operations:
            if directory.kind != "rmdir":
                continue
            for operation in plan.operations:
                if operation.member_id == directory.member_id:
                    continue
                if operation.destination.is_relative_to(directory.destination):
                    if operation.kind == "rmdir":
                        if operation.member_id not in dependencies[directory.member_id]:
                            raise _Failure("conflict", "nested_rmdir_missing_dependency", operation.destination)
                    elif operation.kind == "quarantine":
                        if operation.member_id not in dependencies[directory.member_id]:
                            raise _Failure("conflict", "rmdir_missing_source_cleanup_dependency", operation.destination)
                    else:
                        raise _Failure("conflict", "rmdir_contains_planned_output", operation.destination)
                if operation.kind in {"move", "quarantine"} and operation.source is not None:
                    if operation.source.path.is_relative_to(directory.destination):
                        if operation.member_id not in dependencies[directory.member_id]:
                            raise _Failure("conflict", "rmdir_missing_source_cleanup_dependency", operation.source.path)
    @staticmethod
    def _source_quarantine_parent(source: FileFingerprint, plan: OperationPlan) -> Path:
        containing = [
            operation.destination
            for operation in plan.operations
            if operation.kind == "rmdir" and source.path.is_relative_to(operation.destination)
        ]
        if not containing:
            return source.path.parent
        outermost = min(containing, key=lambda path: len(path.parts))
        return outermost.parent

    def _nearest_existing_parent(
        self,
        parent: Path,
        authorized_root: Path,
        operation: Operation,
    ) -> Path:
        current = parent
        while current.is_relative_to(authorized_root):
            try:
                info = current.lstat()
            except FileNotFoundError:
                current = current.parent
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _Failure("permission_denied", "destination_parent_not_safe_directory", current)
            if not os.access(current, os.W_OK | os.X_OK):
                raise _Failure("permission_denied", "destination_parent_not_writable", current)
            # A stage in an ancestor is on the future destination filesystem
            # unless a non-cooperating writer mounts over the path afterwards.
            return current
        raise _Failure("permission_denied", "no_existing_stage_parent", operation.destination)

    def _validate_fingerprint_scope(
        self,
        plan: OperationPlan,
        observation: FileFingerprint,
        *,
        require_exists: bool,
    ) -> None:
        self._clean_absolute(observation.root, "fingerprint root")
        configured = self._configured_root_for(observation.root)
        if configured is None:
            raise _Failure("permission_denied", "fingerprint_root_outside_configured_roots", observation.root)
        if not any(observation.root == root.path or observation.root == configured for root in plan.roots):
            if self._permission_for(plan, observation.path, write=False) is None:
                raise _Failure("permission_denied", "fingerprint_not_covered_by_plan_root", observation.path)
        if self._permission_for(plan, observation.path, write=False) is None:
            raise _Failure("permission_denied", "fingerprint_path_not_covered_by_plan_root", observation.path)
        self._checked_internal_path(observation.path, observation.root, allow_missing=not require_exists)

    def _validate_configured_roots(self) -> None:
        roots = sorted(set(self.roots), key=str)
        stats: dict[Path, tuple[int, int]] = {}
        for root in roots:
            self._clean_absolute(root, "configured root")
            try:
                checked_path(root, root)
                info = root.lstat()
            except OSError as error:
                raise self._from_os_error(error, root) from error
            if not stat.S_ISDIR(info.st_mode):
                raise _Failure("invalid_input", "configured_root_not_directory", root)
            if root == Path(root.anchor):
                raise _Failure("permission_denied", "filesystem_root_cannot_be_authorized", root)
            stats[root] = (info.st_dev, info.st_ino)
        for index, left in enumerate(roots):
            for right in roots[index + 1 :]:
                same_physical = stats[left] == stats[right]
                nested = left.is_relative_to(right) or right.is_relative_to(left)
                if nested and left != right and not same_physical:
                    raise _Failure("invalid_input", "nested_configured_roots_are_ambiguous", left)

    def _validate_state_path(self) -> None:
        current = Path(self.state_dir.anchor)
        for part in self.state_dir.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode):
                raise _Failure("permission_denied", "state_directory_symlink", current)
            if current == self.state_dir and not stat.S_ISDIR(info.st_mode):
                raise _Failure("invalid_input", "state_path_not_directory", current)
            if current != self.state_dir and not stat.S_ISDIR(info.st_mode):
                raise _Failure("invalid_input", "state_parent_not_directory", current)
        if self.state_dir == Path(self.state_dir.anchor):
            raise _Failure("permission_denied", "filesystem_root_cannot_be_state_directory", self.state_dir)

    def _configured_root_for(self, path: Path) -> Path | None:
        candidates = [root for root in self.roots if path.is_relative_to(root)]
        if not candidates:
            return None
        return max(candidates, key=lambda root: len(root.parts))

    @staticmethod
    def _permission_for(plan: OperationPlan, path: Path, *, write: bool) -> Path | None:
        permissions = [root for root in plan.roots if path.is_relative_to(root.path) and (root.write or not write)]
        if not permissions:
            return None
        return max(permissions, key=lambda root: len(root.path.parts)).path

    @staticmethod
    def _clean_absolute(path: Path, description: str) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise _Failure("invalid_input", f"{description}_must_be_absolute_canonical", path)

    def _checked_internal_path(self, path: Path, root: Path, *, allow_missing: bool = False) -> Path:
        self._clean_absolute(path, "filesystem path")
        self._clean_absolute(root, "filesystem root")
        try:
            return checked_path(path, root, allow_missing=allow_missing)
        except FilesystemError as error:
            raise self._from_os_error(error, error.path) from error

    def _reject_reserved_destination(self, path: Path) -> None:
        if any(part in _RESERVED_NAMES for part in path.parts):
            raise _Failure("permission_denied", "destination_uses_executor_state_name", path)
        if any(part.startswith((".kce-stage-", ".kce-quarantine-", ".kce-restore-")) for part in path.parts):
            raise _Failure("permission_denied", "destination_uses_executor_owned_name", path)

    def _dependency_closure(self, plan: OperationPlan) -> dict[str, frozenset[str]]:
        by_id = {operation.member_id: operation for operation in plan.operations}
        result: dict[str, frozenset[str]] = {}
        for operation in plan.operations:
            pending = list(operation.depends_on)
            closure: set[str] = set()
            while pending:
                member_id = pending.pop()
                if member_id in closure:
                    continue
                dependency = by_id.get(member_id)
                if dependency is None:
                    raise _Failure("invalid_input", "operation_dependency_missing")
                closure.add(member_id)
                pending.extend(dependency.depends_on)
            result[operation.member_id] = frozenset(closure)
        return result

    def _acquire_state_before_database(self, operation_id: str, *, recovering: bool) -> _HeldLock:
        """Hold the process lock before SQLite may repair a hot rollback journal."""
        self._validate_state_path()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        sync_directory(self.state_dir.parent)
        path = self.state_dir / "apply.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _Failure("permission_denied", "lock_path_not_regular_file", path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _BusyLock(path) from None
            owner, expected_store_id = self._read_lock(descriptor, path)
            if owner is not None:
                if owner.operation_id != operation_id or not recovering:
                    raise _Failure("review_required", "abandoned_operation_lock_requires_recovery", path)
                if self._owner_is_live(owner):
                    raise _BusyLock(path)
            if (self.state_dir / "store.id").exists():
                sidecar_id = self.state_store.store_identity()
                if expected_store_id is not None and sidecar_id != expected_store_id:
                    raise _Failure("review_required", "lock_belongs_to_different_state_store", path)
            store_id = self.state_store.initialize()
            if expected_store_id is not None and expected_store_id != store_id:
                raise _Failure("review_required", "lock_belongs_to_different_state_store", path)
            sync_directory(self.state_dir)
            return _HeldLock(path, descriptor, store_id, owner)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise

    def _acquire_lock(
        self,
        path: Path,
        store_id: str,
        *,
        operation_id: str,
        recovering: bool,
        record: OperationRecord | None,
    ) -> _HeldLock:
        path_missing = not path.exists()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise self._from_os_error(error, path) from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _Failure("permission_denied", "lock_path_not_regular_file", path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _BusyLock(path) from None
            owner, recorded_store_id = self._read_lock(descriptor, path)
            if recorded_store_id is not None and recorded_store_id != store_id:
                raise _Failure("review_required", "lock_belongs_to_different_state_store", path)
            if owner is not None:
                if owner.operation_id != operation_id:
                    raise _Failure("review_required", "lock_owned_by_another_pending_operation", path)
                if not recovering:
                    raise _Failure("review_required", "abandoned_operation_lock_requires_recovery", path)
                if record is None or not self._known_owner(record.operation_id, owner):
                    raise _Failure("review_required", "lock_owner_not_recorded_in_journal", path)
                if self._owner_is_live(owner):
                    raise _BusyLock(path)
            elif recovering and record is not None and record.owner_pid is not None and record.owner_start is not None:
                if self._owner_is_live(LockOwner(record.operation_id, record.owner_pid, record.owner_start)):
                    raise _BusyLock(path)
            if path_missing:
                sync_directory(path.parent)
            return _HeldLock(path, descriptor, store_id, owner)
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise

    def _read_lock(self, descriptor: int, path: Path) -> tuple[LockOwner | None, str | None]:
        info = os.fstat(descriptor)
        if info.st_size > 65536:
            raise _Failure("review_required", "lock_metadata_too_large", path)
        content = os.pread(descriptor, 65537, 0)
        if not content:
            return None, None
        try:
            value = json.loads(content.decode("utf-8"), object_pairs_hook=self._unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, _Failure) as error:
            raise _Failure("review_required", "lock_metadata_invalid", path) from error
        if not isinstance(value, dict) or set(value) != {"format", "store_id", "owner"}:
            raise _Failure("review_required", "lock_metadata_invalid", path)
        if value["format"] != _LOCK_FORMAT or not isinstance(value["store_id"], str):
            raise _Failure("review_required", "lock_metadata_invalid", path)
        raw_owner = value["owner"]
        if raw_owner is None:
            return None, value["store_id"]
        if not isinstance(raw_owner, dict) or set(raw_owner) != {"operation_id", "pid", "process_start"}:
            raise _Failure("review_required", "lock_metadata_owner_invalid", path)
        operation_id, pid, process_start = raw_owner["operation_id"], raw_owner["pid"], raw_owner["process_start"]
        if not isinstance(operation_id, str) or type(pid) is not int or not isinstance(process_start, str):
            raise _Failure("review_required", "lock_metadata_owner_invalid", path)
        return LockOwner(operation_id, pid, process_start), value["store_id"]

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate lock metadata key")
            result[key] = value
        return result

    def _write_lock(self, lock: _HeldLock, owner: LockOwner | None) -> None:
        value: dict[str, object] = {
            "format": _LOCK_FORMAT,
            "store_id": lock.store_id,
            "owner": None if owner is None else {
                "operation_id": owner.operation_id,
                "pid": owner.pid,
                "process_start": owner.process_start,
            },
        }
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        os.fchmod(lock.descriptor, 0o600)
        os.ftruncate(lock.descriptor, 0)
        offset = 0
        while offset < len(data):
            written = os.pwrite(lock.descriptor, data[offset:], offset)
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            offset += written
        os.fsync(lock.descriptor)
        lock.owner = owner

    def _known_owner(self, operation_id: str, owner: LockOwner) -> bool:
        return owner.operation_id == operation_id and owner in self.state_store.owner_history(operation_id)

    @staticmethod
    def _owner_is_live(owner: LockOwner) -> bool:
        try:
            os.kill(owner.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if owner.process_start == "unknown":
            return True
        observed = TransactionExecutor._process_start(owner.pid)
        if observed is None:
            return True
        return observed == owner.process_start

    @staticmethod
    def _process_start(pid: int) -> str | None:
        try:
            data = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            tail = data[data.rfind(")") + 1 :].split()
            return tail[19]
        except (OSError, IndexError, UnicodeDecodeError):
            return None

    @classmethod
    def _current_owner(cls, operation_id: str) -> LockOwner:
        pid = os.getpid()
        return LockOwner(operation_id, pid, cls._process_start(pid) or "unknown")

    def _ordered_roots(self, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        physical: dict[tuple[int, int], Path] = {}
        for root in roots:
            info = root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _Failure("permission_denied", "configured_root_changed_before_lock", root)
            identity = (info.st_dev, info.st_ino)
            selected = physical.get(identity)
            if selected is None or (str(root).casefold(), str(root)) < (str(selected).casefold(), str(selected)):
                physical[identity] = root
        return tuple(sorted(physical.values(), key=lambda path: (str(path).casefold(), str(path))))

    def _release_locks(self, locks: list[_HeldLock], operation_id: str | None) -> str | None:
        error_text: str | None = None
        if operation_id is not None:
            try:
                self.state_store.clear_owner(operation_id)
            except (OSError, StateStoreError) as error:
                error_text = self._safe_error(error)
        for lock in reversed(locks):
            try:
                if operation_id is not None or lock.owner is None:
                    self._write_lock(lock, None)
            except OSError as error:
                error_text = error_text or self._safe_error(error)
            try:
                fcntl.flock(lock.descriptor, fcntl.LOCK_UN)
            except OSError as error:
                error_text = error_text or self._safe_error(error)
            try:
                os.close(lock.descriptor)
            except OSError as error:
                error_text = error_text or self._safe_error(error)
        return error_text

    def _ensure_owned_directory(
        self,
        operation_id: str,
        directory: Path,
        operation: Operation,
        authorized_root: Path,
        *,
        purpose: str,
        token: str,
    ) -> None:
        self._checked_internal_path(directory, authorized_root, allow_missing=True)
        created = False
        try:
            directory.lstat()
        except FileNotFoundError:
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                raise _Failure("review_required", "quarantine_directory_appeared", directory) from None
            created = True
            sync_directory(directory.parent)
        if directory.is_symlink() or not directory.is_dir():
            raise _Failure("review_required", "quarantine_directory_not_owned", directory)
        marker = directory / "owner.json"
        if not marker.exists():
            if not created:
                raise _Failure("review_required", "quarantine_owner_marker_missing", marker)
            self._write_owned_marker(marker, operation_id, operation, token, purpose)
            sync_directory(directory)
            sync_directory(directory.parent)
        if not self._marker_matches(marker, operation_id, operation, token, purpose):
            raise _Failure("review_required", "quarantine_owner_marker_mismatch", marker)

    def _owned_directory_matches(
        self,
        directory: Path,
        operation_id: str,
        operation: Operation,
        token: str,
        purpose: str,
    ) -> bool:
        if directory.is_symlink() or not directory.is_dir():
            return False
        return self._marker_matches(directory / "owner.json", operation_id, operation, token, purpose)

    def _write_owned_marker(
        self,
        marker: Path,
        operation_id: str,
        operation: Operation,
        token: str,
        purpose: str,
    ) -> None:
        value = {
            "format": "kce-owned-v1",
            "store_id": self.state_store.store_identity(),
            "operation_id": operation_id,
            "member_id": operation.member_id,
            "purpose": purpose,
            "token": token,
        }
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(marker, flags, 0o600)
        except FileExistsError:
            raise _Failure("review_required", "owned_marker_already_exists", marker) from None
        try:
            self._write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _marker_matches(
        self,
        marker: Path,
        operation_id: str,
        operation: Operation,
        token: str,
        purpose: str,
    ) -> bool:
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
                return False
            value: object = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=self._unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False
        return value == {
            "format": "kce-owned-v1",
            "store_id": self.state_store.store_identity(),
            "operation_id": operation_id,
            "member_id": operation.member_id,
            "purpose": purpose,
            "token": token,
        }

    def _operation_id_for_member(self, operation: Operation) -> str:
        if self._active_operation_id is None:
            raise StateStoreError("transaction operation is not active")
        return self._active_operation_id

    def _safe_fingerprint(self, path: Path, root: Path) -> FileFingerprint:
        try:
            return fingerprint(path, root)
        except FilesystemError as error:
            raise self._from_os_error(error, error.path) from error

    def _assert_existing_directory(self, path: Path, root: Path) -> None:
        self._checked_internal_path(path, root)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise _Failure("permission_denied", "stage_parent_not_directory", path)

    def _assert_parent_ready(self, path: Path, root: Path) -> None:
        self._checked_internal_path(path, root)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _Failure("permission_denied", "destination_parent_not_directory", path)

    @staticmethod
    def _same_file_identity(first: FileFingerprint, second: FileFingerprint) -> bool:
        return (
            first.device == second.device
            and first.inode == second.inode
            and first.size == second.size
            and first.mtime_ns == second.mtime_ns
            and first.sha256 == second.sha256
        )

    @staticmethod
    def _artifact(operation: Operation) -> ArtifactSpec | None:
        if operation.artifact is not None:
            return operation.artifact
        if operation.kind == "move" and operation.source is not None:
            return ArtifactSpec(source=operation.source)
        return None

    @staticmethod
    def _member_token(operation: Operation) -> str:
        return hashlib.sha256(f"{operation.member_id}".encode()).hexdigest()[:24]

    def _member(self, operation: Operation) -> MemberRecord | None:
        operation_id = self._operation_id_for_member(operation)
        for member in self.state_store.list_members(operation_id):
            if member.member_id == operation.member_id:
                return member
        return None

    def _unlink_stage_if_linked(self, stage: Path, destination: Path, expected: FileFingerprint) -> None:
        try:
            stage_info = stage.lstat()
            target_info = destination.lstat()
        except FileNotFoundError:
            return
        if (stage_info.st_dev, stage_info.st_ino) != (target_info.st_dev, target_info.st_ino):
            raise _Failure("review_required", "stage_identity_changed_after_publish", stage)
        if (stage_info.st_dev, stage_info.st_ino) != (expected.device, expected.inode):
            raise _Failure("review_required", "stage_is_not_owned_artifact", stage)
        os.unlink(stage)
        sync_directory(stage.parent)

    def _checkpoint(self, phase: str, member_id: str) -> None:
        if self.checkpoint is not None:
            self.checkpoint(phase, member_id)

    def _record_error(self, operation_id: str, reason: str) -> None:
        try:
            if self.state_store.get_operation(operation_id) is not None:
                self.state_store.update_error(operation_id, reason)
        except (OSError, StateStoreError):
            return

    def _mark_needs_review(self, operation_id: str, reason: str) -> None:
        try:
            record = self.state_store.get_operation(operation_id)
            if record is not None and record.phase != "complete" and record.phase != "needs_review":
                self.state_store.set_phase(operation_id, "needs_review", error=reason)
        except (OSError, StateStoreError):
            self._record_error(operation_id, reason)

    @staticmethod
    def _from_os_error(error: OSError, path: Path | None = None) -> _Failure:
        if isinstance(error, FilesystemError):
            reason = error.reason
            if reason in {"outside_authorized_root", "symlink_traversal"}:
                return _Failure("permission_denied", reason, error.path)
            if reason == "noncanonical_path":
                return _Failure("invalid_input", reason, error.path)
            if reason in {"source_changed", "missing_path"}:
                return _Failure("stale_plan", reason, error.path)
            if reason == "not_regular_file":
                return _Failure("conflict", reason, error.path)
        if error.errno in {errno.EACCES, errno.EPERM}:
            return _Failure("permission_denied", "filesystem_permission_denied", path)
        if error.errno in {errno.ELOOP}:
            return _Failure("permission_denied", "symlink_traversal", path)
        if error.errno in {errno.ENOENT}:
            return _Failure("stale_plan", "filesystem_path_missing", path)
        return _Failure("publication_failed", f"filesystem_error:{error.errno or type(error).__name__}", path)

    @staticmethod
    def _status_for_error(error: OSError | StateStoreError) -> FailureStatus:
        if isinstance(error, FilesystemError):
            return TransactionExecutor._from_os_error(error).status
        if isinstance(error, StateStoreError):
            return "failed"
        if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP}:
            return "permission_denied"
        if error.errno == errno.ENOENT:
            return "stale_plan"
        return "publication_failed"

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        if isinstance(error, FilesystemError):
            return error.reason
        if isinstance(error, StateStoreError):
            return str(error)
        if isinstance(error, OSError):
            return f"filesystem_error:{error.errno or type(error).__name__}"
        return type(error).__name__

    def _result(
        self,
        plan: OperationPlan | None,
        status: str,
        reason: str,
        *,
        path: Path | None = None,
        committed: int = 0,
        skipped: int = 0,
        failed: int | None = None,
    ) -> RunResult:
        typed_status = cast(Status, status)
        if failed is None:
            failed = 1 if status in {"publication_failed", "permission_denied", "failed"} else 0
        decisions = (Decision(typed_status, reason, path),)
        plans = (plan,) if plan is not None else ()
        return RunResult(decisions, plans, committed, skipped, failed)

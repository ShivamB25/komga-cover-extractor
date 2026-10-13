"""Durable, local SQLite journal for recoverable filesystem operations."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from ..domain.decisions import RunAuthorization
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationKind,
    OperationPlan,
    RootPermission,
)

SCHEMA_VERSION = 1
OperationPhase = Literal[
    "planned", "staging", "staged", "publishing", "committed",
    "cleanup_pending", "complete", "needs_review", "failed",
]
MemberState = Literal[
    "planned", "staging", "staged", "publishing", "published", "committed",
    "cleanup_pending", "complete", "needs_review", "failed",
]
OutboxState = Literal["queued", "pending", "complete", "unknown", "failed"]
JobState = Literal["pending", "running", "complete", "failed", "needs_review"]

_PHASES = frozenset({
    "planned", "staging", "staged", "publishing", "committed",
    "cleanup_pending", "complete", "needs_review", "failed",
})
_MEMBER_STATES = frozenset({
    "planned", "staging", "staged", "publishing", "published", "committed",
    "cleanup_pending", "complete", "needs_review", "failed",
})
_OUTBOX_STATES = frozenset({"queued", "pending", "complete", "unknown", "failed"})
_JOB_STATES = frozenset({"pending", "running", "complete", "failed", "needs_review"})


class StateStoreError(RuntimeError):
    """The local journal is corrupt, unsupported, or cannot be safely updated."""


class _SQLiteStateError(StateStoreError):
    """A SQLite failure rendered as an actionable state-store error."""

    def __init__(self, error: sqlite3.Error) -> None:
        self.sqlite_errorcode = getattr(error, "sqlite_errorcode", None)
        self.sqlite_errorname = getattr(error, "sqlite_errorname", None)
        if (
            self.sqlite_errorname == "SQLITE_READONLY_ROLLBACK"
            or self.sqlite_errorcode
            == getattr(sqlite3, "SQLITE_READONLY_ROLLBACK", object())
        ):
            message = (
                "state database has a hot rollback journal; an authorized "
                "apply or recovery under the state-store lock is required to repair it"
            )
        else:
            message = f"SQLite state-store error: {error}"
        super().__init__(message)


class _StateConnection(sqlite3.Connection):
    """Normalize SQLite errors at the boundary where SQL is issued."""

    def execute(
        self,
        sql: str,
        parameters: Any = (),
        /,
    ) -> sqlite3.Cursor:
        try:
            return super().execute(sql, parameters)
        except sqlite3.Error as error:
            raise _SQLiteStateError(error) from error

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        try:
            return super().executescript(sql_script)
        except sqlite3.Error as error:
            raise _SQLiteStateError(error) from error

    def commit(self) -> None:
        try:
            super().commit()
        except sqlite3.Error as error:
            raise _SQLiteStateError(error) from error

    def rollback(self) -> None:
        try:
            super().rollback()
        except sqlite3.Error as error:
            raise _SQLiteStateError(error) from error


_SCHEMA_OBJECTS: dict[str, tuple[str, str]] = {
    "metadata": ("table", "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"),
    "operations": (
        "table",
        """CREATE TABLE operations (
                    operation_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('planned','staging','staged','publishing','committed','cleanup_pending','complete','needs_review','failed')),
                    plan_json TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    error TEXT,
                    owner_pid INTEGER,
                    owner_start TEXT,
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL
                )""",
    ),
    "lock_owners": (
        "table",
        """CREATE TABLE lock_owners (
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                    pid INTEGER NOT NULL,
                    process_start TEXT NOT NULL,
                    created_ns INTEGER NOT NULL,
                    PRIMARY KEY(operation_id, pid, process_start)
                )""",
    ),
    "members": (
        "table",
        """CREATE TABLE members (
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    member_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('publish','move','quarantine','mkdir','rmdir')),
                    status TEXT NOT NULL CHECK(status IN ('planned','staging','staged','publishing','published','committed','cleanup_pending','complete','needs_review','failed')),
                    source_json TEXT,
                    destination TEXT NOT NULL,
                    stage_path TEXT,
                    stage_fingerprint_json TEXT,
                    backup_path TEXT,
                    backup_fingerprint_json TEXT,
                    published_fingerprint_json TEXT,
                    cleanup_path TEXT,
                    cleanup_fingerprint_json TEXT,
                    error TEXT,
                    aux_json TEXT NOT NULL DEFAULT '{}',
                    artifact_data BLOB,
                    PRIMARY KEY(operation_id, member_id),
                    UNIQUE(operation_id, ordinal)
                )""",
    ),
    "jobs": (
        "table",
        """CREATE TABLE jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','running','complete','failed','needs_review')),
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL
                )""",
    ),
    "outbox": (
        "table",
        """CREATE TABLE outbox (
                    event_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                    member_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','pending','complete','unknown','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL,
                    UNIQUE(operation_id, member_id, event_type)
                )""",
    ),
    "jobs_state_idx": ("index", "CREATE INDEX jobs_state_idx ON jobs(state, created_ns)"),
    "outbox_state_idx": ("index", "CREATE INDEX outbox_state_idx ON outbox(state, created_ns)"),
}

_EXPECTED_INDEXES: dict[
    str, dict[str, tuple[bool, str, tuple[str, ...]]]
] = {
    "metadata": {
        "sqlite_autoindex_metadata_1": (True, "pk", ("key",)),
    },
    "operations": {
        "sqlite_autoindex_operations_1": (True, "pk", ("operation_id",)),
    },
    "lock_owners": {
        "sqlite_autoindex_lock_owners_1": (
            True,
            "pk",
            ("operation_id", "pid", "process_start"),
        ),
    },
    "members": {
        "sqlite_autoindex_members_1": (True, "pk", ("operation_id", "member_id")),
        "sqlite_autoindex_members_2": (True, "u", ("operation_id", "ordinal")),
    },
    "jobs": {
        "sqlite_autoindex_jobs_1": (True, "pk", ("job_id",)),
        "jobs_state_idx": (False, "c", ("state", "created_ns")),
    },
    "outbox": {
        "sqlite_autoindex_outbox_1": (True, "pk", ("event_id",)),
        "sqlite_autoindex_outbox_2": (
            True,
            "u",
            ("operation_id", "member_id", "event_type"),
        ),
        "outbox_state_idx": (False, "c", ("state", "created_ns")),
    },
}


def _normalise_schema_sql(value: str) -> str:
    return "".join(value.lower().split())


def _valid_store_identity(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    schema_version: int
    policy_version: str
    phase: OperationPhase
    plan: OperationPlan
    error: str | None
    owner_pid: int | None
    owner_start: str | None
    created_ns: int
    updated_ns: int


@dataclass(frozen=True, slots=True)
class MemberRecord:
    operation_id: str
    member_id: str
    ordinal: int
    kind: OperationKind
    status: MemberState
    source: FileFingerprint | None
    destination: Path
    stage_path: Path | None = None
    stage_fingerprint: FileFingerprint | None = None
    backup_path: Path | None = None
    backup_fingerprint: FileFingerprint | None = None
    published_fingerprint: FileFingerprint | None = None
    cleanup_path: Path | None = None
    cleanup_fingerprint: FileFingerprint | None = None
    error: str | None = None
    aux: dict[str, object] | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    event_id: str
    operation_id: str
    member_id: str
    event_type: str
    payload: dict[str, object]
    state: OutboxState
    attempts: int
    error: str | None
    created_ns: int
    updated_ns: int


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    kind: str
    state: JobState
    payload: dict[str, object]
    error: str | None
    created_ns: int
    updated_ns: int


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        str(row["job_id"]), str(row["kind"]), cast(JobState, _state(str(row["state"]), _JOB_STATES, "job state")),
        _object(_decode_json(str(row["payload_json"]), "job payload"), "job payload"),
        str(row["error"]) if row["error"] is not None else None,
        int(row["created_ns"]), int(row["updated_ns"]),
    )


@dataclass(frozen=True, slots=True)
class LockOwner:
    operation_id: str
    pid: int
    process_start: str



def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StateStoreError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(value: str, description: str) -> object:
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise StateStoreError(f"invalid {description} JSON") from error


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StateStoreError(f"{description} must be a JSON object")
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], keys: frozenset[str], description: str) -> None:
    if set(value) != keys:
        raise StateStoreError(f"invalid {description} fields")


def _required_str(value: object, description: str) -> str:
    if not isinstance(value, str):
        raise StateStoreError(f"{description} must be a string")
    return value


def _required_int(value: object, description: str) -> int:
    if type(value) is not int:
        raise StateStoreError(f"{description} must be an integer")
    return value


def _decode_fingerprint(value: object) -> FileFingerprint | None:
    if value is None:
        return None
    item = _object(value, "fingerprint")
    _exact_keys(item, frozenset({"root", "relative_path", "device", "inode", "size", "mtime_ns", "sha256"}), "fingerprint")
    try:
        return FileFingerprint(
            Path(_required_str(item["root"], "fingerprint root")),
            _required_str(item["relative_path"], "fingerprint relative path"),
            _required_int(item["device"], "fingerprint device"),
            _required_int(item["inode"], "fingerprint inode"),
            _required_int(item["size"], "fingerprint size"),
            _required_int(item["mtime_ns"], "fingerprint mtime"),
            _required_str(item["sha256"], "fingerprint hash"),
        )
    except (TypeError, ValueError) as error:
        raise StateStoreError("invalid fingerprint in operation plan") from error


def _decode_plan(value: object, artifact_data: dict[str, bytes | None]) -> OperationPlan:
    plan_data = _object(value, "operation plan")
    _exact_keys(
        plan_data,
        frozenset({"schema_version", "policy_version", "operation_id", "reason", "roots", "operations"}),
        "operation plan",
    )
    schema_version = _required_int(plan_data["schema_version"], "plan schema version")
    if schema_version != SCHEMA_VERSION:
        raise StateStoreError(f"unsupported operation schema version: {schema_version}")
    roots_value = plan_data["roots"]
    operations_value = plan_data["operations"]
    if not isinstance(roots_value, list) or not isinstance(operations_value, list):
        raise StateStoreError("operation plan roots and operations must be arrays")
    roots: list[RootPermission] = []
    for value_item in roots_value:
        root_data = _object(value_item, "root permission")
        _exact_keys(root_data, frozenset({"path", "write"}), "root permission")
        path = Path(_required_str(root_data["path"], "root path"))
        write = root_data["write"]
        if type(write) is not bool:
            raise StateStoreError("root permission write flag must be boolean")
        try:
            roots.append(RootPermission(path, write))
        except ValueError as error:
            raise StateStoreError("invalid root permission in operation plan") from error

    operations: list[Operation] = []
    for op_data_value in operations_value:
        op_data = _object(op_data_value, "operation")
        _exact_keys(
            op_data,
            frozenset({"member_id", "kind", "destination", "source", "destination_before", "artifact", "depends_on", "collision", "reason"}),
            "operation",
        )
        member_id = _required_str(op_data["member_id"], "member id")
        raw_kind = _required_str(op_data["kind"], "operation kind")
        if raw_kind not in {"publish", "move", "quarantine", "mkdir", "rmdir"}:
            raise StateStoreError(f"unsupported serialized operation kind: {raw_kind}")
        kind = cast(OperationKind, raw_kind)
        destination = Path(_required_str(op_data["destination"], "destination"))
        source = _decode_fingerprint(op_data["source"])
        destination_before = _decode_fingerprint(op_data["destination_before"])

        artifact_value = op_data["artifact"]
        artifact: ArtifactSpec | None = None
        if artifact_value is not None:
            artifact_data_value = _object(artifact_value, "artifact")
            _exact_keys(artifact_data_value, frozenset({"source", "sha256", "size", "media_type"}), "artifact")
            artifact_source = _decode_fingerprint(artifact_data_value["source"])
            digest = _required_str(artifact_data_value["sha256"], "artifact hash")
            size = _required_int(artifact_data_value["size"], "artifact size")
            media_type_value = artifact_data_value["media_type"]
            if media_type_value is not None and not isinstance(media_type_value, str):
                raise StateStoreError("artifact media type must be a string or null")
            content = artifact_data.get(member_id)
            if artifact_source is None:
                if content is None or len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                    raise StateStoreError("artifact bytes do not match the recorded digest")
                artifact = ArtifactSpec(content=content, media_type=media_type_value)
            else:
                if content is not None:
                    raise StateStoreError("source artifact unexpectedly contains inline bytes")
                artifact = ArtifactSpec(source=artifact_source, media_type=media_type_value)
                if artifact.size != size or artifact.digest != digest:
                    raise StateStoreError("source artifact metadata does not match its fingerprint")
        elif artifact_data.get(member_id) is not None:
            raise StateStoreError("unexpected artifact bytes for operation")

        dependencies_value = op_data["depends_on"]
        if not isinstance(dependencies_value, list) or any(not isinstance(item, str) for item in dependencies_value):
            raise StateStoreError("operation dependencies must be strings")
        raw_collision = _required_str(op_data["collision"], "collision decision")
        if raw_collision not in {"absent", "replace"}:
            raise StateStoreError("unsupported collision decision")
        try:
            operations.append(
                Operation(
                    member_id=member_id,
                    kind=kind,
                    destination=destination,
                    source=source,
                    destination_before=destination_before,
                    artifact=artifact,
                    depends_on=tuple(dependencies_value),
                    collision=cast(Literal["absent", "replace"], raw_collision),
                    reason=_required_str(op_data["reason"], "operation reason"),
                )
            )
        except (TypeError, ValueError) as error:
            raise StateStoreError("invalid serialized operation") from error
    try:
        return OperationPlan(
            policy_version=_required_str(plan_data["policy_version"], "policy version"),
            operation_id=_required_str(plan_data["operation_id"], "operation id"),
            operations=tuple(operations),
            roots=tuple(roots),
            reason=_required_str(plan_data["reason"], "plan reason"),
            schema_version=schema_version,
        )
    except (TypeError, ValueError) as error:
        raise StateStoreError("invalid serialized operation plan") from error


def _json_dumps(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise StateStoreError("journal payload is not JSON serializable") from error


def _state(value: str, allowed: frozenset[str], description: str) -> str:
    if value not in allowed:
        raise StateStoreError(f"invalid {description}: {value}")
    return value


@dataclass(frozen=True, slots=True)
class CommittedArtifact:
    published: FileFingerprint
    source: FileFingerprint | None
    kind: OperationKind
    created_ns: int
    plan_json: str


class StateStore:
    """One local SQLite journal; constructing it performs no filesystem I/O."""

    def __init__(self, state_dir: Path) -> None:
        if not state_dir.is_absolute() or ".." in state_dir.parts:
            raise ValueError("state directory must be absolute and canonical")
        self.state_dir = Path(os.path.abspath(state_dir))
        self.path = self.state_dir / "state.sqlite3"

    def initialize(self) -> str:
        """Create or validate schema 1 while the caller holds the state lock."""
        self._ensure_directory()
        if self.path.is_symlink():
            raise StateStoreError("state database must not be a symlink")
        if self.path.exists() and not self.path.is_file():
            raise StateStoreError("state database is not a regular file")
        connection = self._connect(write=True)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                objects = {
                    str(row["name"])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type IN ('table','index','view','trigger') "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if objects:
                    raise StateStoreError("unversioned state database is not empty")
                self._create_schema(connection)
            elif version != SCHEMA_VERSION:
                raise StateStoreError(f"unsupported state schema version: {version}")
            self._validate_schema(connection)
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='store_id'"
            ).fetchone()
            if row is None or not isinstance(row[0], str) or not _valid_store_identity(row[0]):
                raise StateStoreError("state database identity is missing or invalid")
            database_identity = str(row[0])
        finally:
            connection.close()
        self._ensure_store_identity(database_identity)
        return database_identity

    def _ensure_directory(self) -> None:
        current = Path(self.state_dir.anchor)
        for component in self.state_dir.parts[1:]:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                else:
                    self._sync_parent(current.parent)
                info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise StateStoreError(f"unsafe state directory component: {current}")

    @staticmethod
    def _sync_parent(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        self._check_existing_path()
        if not write and not self.path.exists():
            raise FileNotFoundError(self.path)
        if self.path.is_symlink():
            raise StateStoreError("state database must not be a symlink")
        database = str(self.path) if write else f"{self.path.as_uri()}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database,
                timeout=30.0,
                isolation_level=None,
                uri=not write,
                factory=_StateConnection,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if write:
                mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
                if mode != "delete":
                    raise StateStoreError("SQLite rollback journal mode is required")
                connection.execute("PRAGMA synchronous=FULL")
                synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
                if synchronous != 2:
                    raise StateStoreError("SQLite synchronous=FULL is required")
            else:
                connection.execute("PRAGMA query_only=ON")
            return connection
        except StateStoreError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise _SQLiteStateError(error) from error

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for _, statement in _SCHEMA_OBJECTS.values():
                connection.execute(statement)
            store_id = os.urandom(16).hex()
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('store_id',?)", (store_id,)
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        actual = {str(row["name"]): row for row in rows}
        if set(actual) != set(_SCHEMA_OBJECTS):
            raise StateStoreError("state database objects do not match schema 1")
        for name, (object_type, expected_sql) in _SCHEMA_OBJECTS.items():
            row = actual[name]
            sql = row["sql"]
            if (
                str(row["type"]) != object_type
                or not isinstance(sql, str)
                or _normalise_schema_sql(sql) != _normalise_schema_sql(expected_sql)
            ):
                raise StateStoreError(f"state schema object does not match schema 1: {name}")

        expected_columns: dict[str, frozenset[str]] = {
            "metadata": frozenset({"key", "value"}),
            "operations": frozenset({
                "operation_id", "schema_version", "policy_version", "phase", "plan_json",
                "plan_sha256", "error", "owner_pid", "owner_start", "created_ns", "updated_ns",
            }),
            "members": frozenset({
                "operation_id", "schema_version", "member_id", "ordinal", "kind", "status",
                "source_json", "destination", "stage_path", "stage_fingerprint_json", "backup_path",
                "backup_fingerprint_json", "published_fingerprint_json", "cleanup_path",
                "cleanup_fingerprint_json", "error", "aux_json", "artifact_data",
            }),
            "jobs": frozenset({
                "job_id", "kind", "state", "payload_json", "error", "created_ns", "updated_ns",
            }),
            "outbox": frozenset({
                "event_id", "operation_id", "member_id", "event_type", "payload_json",
                "state", "attempts", "error", "created_ns", "updated_ns",
            }),
            "lock_owners": frozenset({"operation_id", "pid", "process_start", "created_ns"}),
        }
        for table, columns in expected_columns.items():
            observed_indexes = frozenset(
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if observed_indexes != columns:
                raise StateStoreError(f"state table columns do not match schema 1: {table}")
            result = connection.execute(f"PRAGMA quick_check({table})").fetchone()
            if result is None or result[0] != "ok":
                raise StateStoreError(f"state table failed integrity check: {table}")

        for table, indexes in _EXPECTED_INDEXES.items():
            index_results: dict[str, tuple[bool, str, tuple[str, ...]]] = {}
            for row in connection.execute(f'PRAGMA index_list("{table}")'):
                index_name = str(row["name"])
                index_columns = tuple(
                    str(index_row["name"])
                    for index_row in connection.execute(
                        f'PRAGMA index_info("{index_name}")'
                    )
                    if index_row["name"] is not None
                )
                index_results[index_name] = (
                    bool(row["unique"]),
                    str(row["origin"]),
                    index_columns,
                )
            if index_results != indexes:
                raise StateStoreError(f"state table indexes do not match schema 1: {table}")

    def _read_connection(self) -> sqlite3.Connection:
        self._check_existing_path()
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        connection = self._connect(write=False)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StateStoreError(f"unsupported state schema version: {version}")
            self._validate_schema(connection)
            return connection
        except BaseException:
            connection.close()
            raise

    def _read_store_identity_sidecar(self) -> str | None:
        sidecar = self.state_dir / "store.id"
        try:
            initial = sidecar.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StateStoreError(f"cannot inspect state identity sidecar: {error}") from error
        if not stat.S_ISREG(initial.st_mode):
            raise StateStoreError("state identity sidecar must be a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(sidecar, flags)
        except OSError as error:
            raise StateStoreError(f"cannot read state identity sidecar: {error}") from error
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
            ):
                raise StateStoreError("state identity sidecar changed while opening")
            value = bytearray()
            while len(value) <= 33:
                chunk = os.read(fd, 34 - len(value))
                if not chunk:
                    break
                value.extend(chunk)
        except OSError as error:
            raise StateStoreError(f"cannot read state identity sidecar: {error}") from error
        finally:
            os.close(fd)
        if (
            len(value) != 33
            or value[-1:] != b"\n"
            or not _valid_store_identity(value[:32].decode("ascii", errors="ignore"))
        ):
            raise StateStoreError("state identity sidecar is malformed")
        return value[:32].decode("ascii")

    def _database_identity(self) -> str:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            raise StateStoreError("state store has not been initialized") from None
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='store_id'"
            ).fetchone()
            if row is None or not isinstance(row[0], str) or not _valid_store_identity(row[0]):
                raise StateStoreError("state database identity is missing or invalid")
            return str(row[0])
        finally:
            connection.close()

    def store_identity(self) -> str:
        """Return identity without opening a writable database connection."""
        self._check_existing_path()
        sidecar_identity = self._read_store_identity_sidecar()
        return sidecar_identity if sidecar_identity is not None else self._database_identity()

    def _ensure_store_identity(self, database_identity: str) -> None:
        if not _valid_store_identity(database_identity):
            raise StateStoreError("state database identity is missing or invalid")
        sidecar = self.state_dir / "store.id"
        existing = self._read_store_identity_sidecar()
        if existing is not None:
            if existing != database_identity:
                raise StateStoreError(
                    "state identity sidecar does not match the database; refusing to continue"
                )
            return

        temporary = self.state_dir / f".store.id.{os.urandom(8).hex()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        created_temporary = False
        try:
            fd = os.open(temporary, flags, 0o600)
            created_temporary = True
            try:
                payload = (database_identity + "\n").encode("ascii")
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        raise OSError("short write while persisting state identity")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temporary, sidecar, follow_symlinks=False)
            except FileExistsError:
                existing = self._read_store_identity_sidecar()
                if existing != database_identity:
                    raise StateStoreError(
                        "state identity sidecar does not match the database; refusing to continue"
                    )
                return
            self._sync_parent(self.state_dir)
        except OSError as error:
            raise StateStoreError(f"cannot persist state identity sidecar: {error}") from error
        finally:
            if created_temporary:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise StateStoreError(
                        f"cannot remove temporary state identity: {error}"
                    ) from error
                try:
                    self._sync_parent(self.state_dir)
                except OSError as error:
                    raise StateStoreError(
                        f"cannot sync state identity directory: {error}"
                    ) from error

    def _check_existing_path(self) -> None:
        current = Path(self.state_dir.anchor)
        for component in self.state_dir.parts[1:]:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode):
                raise StateStoreError(f"state path contains a symlink: {current}")
            if not stat.S_ISDIR(info.st_mode):
                raise StateStoreError(f"state path component is not a directory: {current}")
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise StateStoreError("state database is not a regular file")

    @staticmethod
    def _transaction(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def record_plan(self, plan: OperationPlan, *, owner: LockOwner) -> None:
        value = plan.as_dict(include_content=False)
        plan_json = _json_dumps(value)
        plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        now = time.time_ns()
        connection = self._connect(write=True)
        try:
            self._transaction(connection)
            try:
                connection.execute(
                    "INSERT INTO operations(operation_id,schema_version,policy_version,phase,plan_json,plan_sha256,error,owner_pid,owner_start,created_ns,updated_ns) VALUES(?,?,?,?,?,?,NULL,?,?,?,?)",
                    (plan.operation_id, plan.schema_version, plan.policy_version, "planned", plan_json, plan_hash, owner.pid, owner.process_start, now, now),
                )
                connection.execute(
                    "INSERT INTO lock_owners(operation_id,pid,process_start,created_ns) VALUES(?,?,?,?)",
                    (plan.operation_id, owner.pid, owner.process_start, now),
                )
                for ordinal, operation in enumerate(plan.operations):
                    source_json = _json_dumps(operation.source.as_dict()) if operation.source else None
                    content = operation.artifact.content if operation.artifact is not None else None
                    connection.execute(
                        "INSERT INTO members(operation_id,schema_version,member_id,ordinal,kind,status,source_json,destination,aux_json,artifact_data) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (plan.operation_id, SCHEMA_VERSION, operation.member_id, ordinal, operation.kind, "planned", source_json, str(operation.destination), "{}", content),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except _SQLiteStateError as error:
            if (error.sqlite_errorname or "").startswith("SQLITE_CONSTRAINT"):
                raise StateStoreError(
                    "operation identifier already exists or violates journal constraints"
                ) from error
            raise
        finally:
            connection.close()

    def update_owner(self, operation_id: str, owner: LockOwner) -> None:
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE operations SET owner_pid=?,owner_start=?,updated_ns=? WHERE operation_id=?",
                (owner.pid, owner.process_start, time.time_ns(), operation_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StateStoreError("operation is not journaled")
            connection.execute(
                "INSERT OR IGNORE INTO lock_owners(operation_id,pid,process_start,created_ns) VALUES(?,?,?,?)",
                (operation_id, owner.pid, owner.process_start, time.time_ns()),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def clear_owner(self, operation_id: str) -> None:
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE operations SET owner_pid=NULL,owner_start=NULL,updated_ns=? WHERE operation_id=?",
                (time.time_ns(), operation_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StateStoreError("operation is not journaled")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def owner_history(self, operation_id: str) -> tuple[LockOwner, ...]:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return ()
        try:
            rows = connection.execute(
                "SELECT pid,process_start FROM lock_owners WHERE operation_id=? ORDER BY created_ns,pid,process_start",
                (operation_id,),
            ).fetchall()
            return tuple(LockOwner(operation_id, int(row["pid"]), str(row["process_start"])) for row in rows)
        finally:
            connection.close()

    def set_phase(self, operation_id: str, phase: OperationPhase, *, error: str | None = None) -> None:
        _state(phase, _PHASES, "operation phase")
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT phase FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StateStoreError("operation is not journaled")
            old = str(row["phase"])
            if old not in _PHASES:
                connection.rollback()
                raise StateStoreError("journal contains an invalid operation phase")
            allowed: dict[str, frozenset[str]] = {
                "planned": frozenset({"planned", "staging", "publishing", "needs_review", "failed"}),
                "staging": frozenset({"staging", "staged", "publishing", "needs_review", "failed"}),
                "staged": frozenset({"staged", "publishing", "needs_review", "failed"}),
                "publishing": frozenset({"publishing", "committed", "needs_review", "failed"}),
                "committed": frozenset({"committed", "cleanup_pending", "complete", "needs_review"}),
                "cleanup_pending": frozenset({"cleanup_pending", "complete", "needs_review", "failed"}),
                "failed": frozenset({"failed", "staging", "staged", "publishing", "needs_review"}),
                "needs_review": frozenset({"needs_review"}),
                "complete": frozenset({"complete"}),
            }
            if phase not in allowed[old]:
                connection.rollback()
                raise StateStoreError(f"invalid operation phase transition: {old} -> {phase}")
            connection.execute(
                "UPDATE operations SET phase=?,error=?,updated_ns=? WHERE operation_id=?",
                (phase, error, time.time_ns(), operation_id),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def update_error(self, operation_id: str, error: str | None) -> None:
        connection = self._connect(write=True)
        try:
            cursor = connection.execute(
                "UPDATE operations SET error=?,updated_ns=? WHERE operation_id=?",
                (error, time.time_ns(), operation_id),
            )
            if cursor.rowcount != 1:
                raise StateStoreError("operation is not journaled")
        finally:
            connection.close()

    def save_member(self, member: MemberRecord) -> None:
        _state(member.status, _MEMBER_STATES, "member state")
        if member.schema_version != SCHEMA_VERSION:
            raise StateStoreError(f"unsupported member schema version: {member.schema_version}")
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status,schema_version FROM members WHERE operation_id=? AND member_id=?",
                (member.operation_id, member.member_id),
            ).fetchone()
            if current is None:
                connection.rollback()
                raise StateStoreError("member is not journaled")
            if int(current["schema_version"]) != SCHEMA_VERSION:
                connection.rollback()
                raise StateStoreError("unsupported member schema version in journal")
            old_status = _state(str(current["status"]), _MEMBER_STATES, "member state")
            transitions: dict[str, frozenset[str]] = {
                "planned": frozenset({"planned", "staging", "staged", "publishing", "failed", "needs_review"}),
                "staging": frozenset({"staging", "staged", "failed", "needs_review"}),
                "staged": frozenset({"staged", "publishing", "cleanup_pending", "complete", "failed", "needs_review"}),
                "publishing": frozenset({"publishing", "staged", "published", "failed", "needs_review"}),
                "published": frozenset({"published", "committed", "failed", "needs_review"}),
                "committed": frozenset({"committed", "cleanup_pending", "complete", "needs_review"}),
                "cleanup_pending": frozenset({"cleanup_pending", "complete", "failed", "needs_review"}),
                "failed": frozenset({"failed", "staging", "staged", "needs_review"}),
                "needs_review": frozenset({"needs_review"}),
                "complete": frozenset({"complete"}),
            }
            if member.status not in transitions[old_status]:
                connection.rollback()
                raise StateStoreError(f"invalid member state transition: {old_status} -> {member.status}")
            cursor = connection.execute(
                "UPDATE members SET schema_version=?,status=?,stage_path=?,stage_fingerprint_json=?,backup_path=?,backup_fingerprint_json=?,published_fingerprint_json=?,cleanup_path=?,cleanup_fingerprint_json=?,error=?,aux_json=? WHERE operation_id=? AND member_id=?",
                (
                    member.schema_version,
                    member.status,
                    str(member.stage_path) if member.stage_path else None,
                    _json_dumps(member.stage_fingerprint.as_dict()) if member.stage_fingerprint else None,
                    str(member.backup_path) if member.backup_path else None,
                    _json_dumps(member.backup_fingerprint.as_dict()) if member.backup_fingerprint else None,
                    _json_dumps(member.published_fingerprint.as_dict()) if member.published_fingerprint else None,
                    str(member.cleanup_path) if member.cleanup_path else None,
                    _json_dumps(member.cleanup_fingerprint.as_dict()) if member.cleanup_fingerprint else None,
                    member.error,
                    _json_dumps(member.aux or {}),
                    member.operation_id,
                    member.member_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StateStoreError("member is not journaled")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def commit_group(
        self,
        operation_id: str,
        *,
        event_payloads: tuple[tuple[str, str, dict[str, object]], ...],
    ) -> None:
        """Atomically commit verified members and insert unique local events."""
        now = time.time_ns()
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT phase FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StateStoreError("operation is not journaled")
            phase = str(row["phase"])
            if phase not in {"publishing", "committed", "cleanup_pending", "complete"}:
                connection.rollback()
                raise StateStoreError("operation cannot commit from its current phase")
            member_rows = connection.execute(
                "SELECT member_id,kind,status FROM members WHERE operation_id=? ORDER BY ordinal",
                (operation_id,),
            ).fetchall()
            expected_events: set[str] = set()
            for member_row in member_rows:
                member_id = str(member_row["member_id"])
                kind = str(member_row["kind"])
                status = _state(str(member_row["status"]), _MEMBER_STATES, "member state")
                required = "published" if kind in {"publish", "move", "mkdir"} else "staged"
                if phase == "publishing" and status != required:
                    connection.rollback()
                    raise StateStoreError(f"member {member_id} is not ready for group commit")
                expected_events.add(member_id)
            received_events: set[str] = set()
            for member_id, event_type, payload in event_payloads:
                if event_type != "local_commit" or member_id in received_events:
                    connection.rollback()
                    raise StateStoreError("invalid or duplicate local commit event")
                received_events.add(member_id)
                _json_dumps(payload)
            if received_events != expected_events:
                connection.rollback()
                raise StateStoreError("local commit events do not match operation members")
            if phase == "publishing":
                connection.execute(
                    "UPDATE members SET status='committed',error=NULL WHERE operation_id=? AND status='published'",
                    (operation_id,),
                )
                connection.execute(
                    "UPDATE operations SET phase='committed',error=NULL,updated_ns=? WHERE operation_id=?",
                    (now, operation_id),
                )
            for member_id, event_type, payload in event_payloads:
                event_id = hashlib.sha256(f"{operation_id}\0{member_id}\0{event_type}".encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO outbox(event_id,operation_id,member_id,event_type,payload_json,state,attempts,error,created_ns,updated_ns) VALUES(?,?,?,?,?,'queued',0,NULL,?,?)",
                    (event_id, operation_id, member_id, event_type, _json_dumps(payload), now, now),
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def committed_artifact(self, path: Path, *, before_ns: int | None = None) -> CommittedArtifact | None:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return None
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT m.kind,m.source_json,m.published_fingerprint_json,o.created_ns,o.plan_json,o.plan_sha256 "
                "FROM members m JOIN operations o USING(operation_id) "
                "WHERE m.destination=? AND m.kind IN ('publish','move') "
                "AND m.status IN ('committed','cleanup_pending','complete') "
                "AND o.phase IN ('committed','cleanup_pending','complete') "
                "AND o.created_ns<? ORDER BY o.created_ns DESC,m.ordinal DESC LIMIT 1",
                (str(path), before_ns if before_ns is not None else 9_223_372_036_854_775_807),
            ).fetchone()
            if row is None or row["published_fingerprint_json"] is None:
                return None
            published = _decode_fingerprint(_decode_json(str(row["published_fingerprint_json"]), "published artifact"))
            if published is None or published.path != path:
                raise StateStoreError("published artifact path does not match journal")
            source = None if row["source_json"] is None else _decode_fingerprint(
                _decode_json(str(row["source_json"]), "artifact source")
            )
            encoded = str(row["plan_json"])
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["plan_sha256"]:
                raise StateStoreError("operation plan digest mismatch")
            return CommittedArtifact(published, source, cast(OperationKind, str(row["kind"])), int(row["created_ns"]), encoded)
        finally:
            connection.close()

    def artifact_assessment_matches(
        self, published: FileFingerprint, source: FileFingerprint, generation: str,
    ) -> bool:
        key = "artifact-assessment:" + hashlib.sha256(str(published.path).encode("utf-8")).hexdigest()
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return False
        try:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if row is None:
                return False
            value = _decode_json(str(row["value"]), "artifact assessment")
            return value == {"published": published.as_dict(), "source": source.as_dict(), "generation": generation}
        finally:
            connection.close()

    def record_artifact_assessment(
        self, published: FileFingerprint, source: FileFingerprint, generation: str, *,
        authorization: RunAuthorization,
    ) -> None:
        if not authorization.apply:
            raise StateStoreError("artifact assessment requires apply authorization")
        committed = self.committed_artifact(published.path)
        if committed is None or committed.published != published:
            return
        from .filesystem import matches
        if not matches(published) or not matches(source):
            raise StateStoreError("artifact assessment observations changed")
        key = "artifact-assessment:" + hashlib.sha256(str(published.path).encode("utf-8")).hexdigest()
        value = _json_dumps({"published": published.as_dict(), "source": source.as_dict(), "generation": generation})
        connection = self._connect(write=True)
        try:
            self._transaction(connection)
            connection.execute("INSERT INTO metadata(key,value) VALUES(?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            connection.commit()
        finally:
            connection.close()

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return None
        try:
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            schema_version = int(row["schema_version"])
            if schema_version != SCHEMA_VERSION:
                raise StateStoreError(f"unsupported operation schema version: {schema_version}")
            plan_raw = _decode_json(str(row["plan_json"]), "operation plan")
            calculated = hashlib.sha256(str(row["plan_json"]).encode("utf-8")).hexdigest()
            if calculated != row["plan_sha256"]:
                raise StateStoreError("operation plan digest mismatch")
            content_rows = connection.execute(
                "SELECT member_id,artifact_data FROM members WHERE operation_id=?",
                (operation_id,),
            ).fetchall()
            artifact_data: dict[str, bytes | None] = {
                str(content_row["member_id"]): bytes(content_row["artifact_data"]) if content_row["artifact_data"] is not None else None
                for content_row in content_rows
            }
            plan = _decode_plan(plan_raw, artifact_data)
            if plan.operation_id != operation_id or plan.policy_version != row["policy_version"]:
                raise StateStoreError("operation plan metadata does not match journal row")
            phase = _state(str(row["phase"]), _PHASES, "operation phase")
            owner_pid = int(row["owner_pid"]) if row["owner_pid"] is not None else None
            owner_start = str(row["owner_start"]) if row["owner_start"] is not None else None
            return OperationRecord(
                str(row["operation_id"]), schema_version, str(row["policy_version"]),
                cast(OperationPhase, phase), plan, str(row["error"]) if row["error"] is not None else None,
                owner_pid, owner_start, int(row["created_ns"]), int(row["updated_ns"]),
            )
        finally:
            connection.close()

    def list_members(self, operation_id: str) -> tuple[MemberRecord, ...]:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return ()
        try:
            connection.execute("BEGIN")
            operation_row = connection.execute(
                "SELECT plan_json,plan_sha256 FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation_row is None:
                return ()
            plan_json = str(operation_row["plan_json"])
            if hashlib.sha256(plan_json.encode("utf-8")).hexdigest() != operation_row["plan_sha256"]:
                raise StateStoreError("operation plan digest mismatch")
            content_rows = connection.execute(
                "SELECT member_id,artifact_data FROM members WHERE operation_id=?",
                (operation_id,),
            ).fetchall()
            content = {
                str(row["member_id"]): bytes(row["artifact_data"]) if row["artifact_data"] is not None else None
                for row in content_rows
            }
            raw_plan = _decode_json(plan_json, "operation plan")
            plan = _decode_plan(raw_plan, content)
            operations = {operation.member_id: operation for operation in plan.operations}
            ordinals = {operation.member_id: ordinal for ordinal, operation in enumerate(plan.operations)}
            rows = connection.execute(
                "SELECT * FROM members WHERE operation_id=? ORDER BY ordinal",
                (operation_id,),
            ).fetchall()
            members: list[MemberRecord] = []
            for row in rows:
                schema_version = int(row["schema_version"])
                if schema_version != SCHEMA_VERSION:
                    raise StateStoreError(f"unsupported member schema version: {schema_version}")
                member_id = str(row["member_id"])
                operation = operations.get(member_id)
                if operation is None:
                    raise StateStoreError("journal member is absent from its operation plan")
                if (
                    int(row["ordinal"]) != ordinals[member_id]
                    or str(row["kind"]) != operation.kind
                    or str(row["destination"]) != str(operation.destination)
                ):
                    raise StateStoreError("journal member metadata does not match its operation plan")
                source_value = _decode_json(str(row["source_json"]), "member source") if row["source_json"] is not None else None
                if _decode_fingerprint(source_value) != operation.source:
                    raise StateStoreError("journal member source does not match its operation plan")
                aux = _object(_decode_json(str(row["aux_json"]), "member auxiliary"), "member auxiliary")
                status = _state(str(row["status"]), _MEMBER_STATES, "member state")
                members.append(
                    MemberRecord(
                        operation_id=operation_id,
                        member_id=member_id,
                        ordinal=int(row["ordinal"]),
                        kind=operation.kind,
                        status=cast(MemberState, status),
                        source=operation.source,
                        destination=operation.destination,
                        stage_path=Path(str(row["stage_path"])) if row["stage_path"] is not None else None,
                        stage_fingerprint=self._decode_optional_fingerprint(row["stage_fingerprint_json"]),
                        backup_path=Path(str(row["backup_path"])) if row["backup_path"] is not None else None,
                        backup_fingerprint=self._decode_optional_fingerprint(row["backup_fingerprint_json"]),
                        published_fingerprint=self._decode_optional_fingerprint(row["published_fingerprint_json"]),
                        cleanup_path=Path(str(row["cleanup_path"])) if row["cleanup_path"] is not None else None,
                        cleanup_fingerprint=self._decode_optional_fingerprint(row["cleanup_fingerprint_json"]),
                        error=str(row["error"]) if row["error"] is not None else None,
                        aux=aux,
                        schema_version=schema_version,
                    )
                )
            return tuple(members)
        finally:
            connection.close()

    @staticmethod
    def _decode_optional_fingerprint(value: object) -> FileFingerprint | None:
        if value is None:
            return None
        return _decode_fingerprint(_decode_json(str(value), "fingerprint"))

    def list_operations(self, phases: tuple[OperationPhase, ...] = ()) -> tuple[OperationRecord, ...]:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return ()
        try:
            if phases:
                for phase in phases:
                    _state(phase, _PHASES, "operation phase")
                placeholders = ",".join("?" for _ in phases)
                rows = connection.execute(
                    f"SELECT operation_id FROM operations WHERE phase IN ({placeholders}) ORDER BY created_ns",
                    phases,
                ).fetchall()
            else:
                rows = connection.execute("SELECT operation_id FROM operations ORDER BY created_ns").fetchall()
        finally:
            connection.close()
        records: list[OperationRecord] = []
        for row in rows:
            operation = self.get_operation(str(row["operation_id"]))
            if operation is not None:
                records.append(operation)
        return tuple(records)

    def outbox_state(self, event_id: str) -> OutboxState | None:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return None
        try:
            row = connection.execute("SELECT state FROM outbox WHERE event_id=?", (event_id,)).fetchone()
            return cast(OutboxState, _state(str(row["state"]), _OUTBOX_STATES, "outbox state")) if row is not None else None
        finally:
            connection.close()

    def list_outbox(
        self, *, operation_id: str | None = None, states: tuple[OutboxState, ...] = (),
        event_type: str | None = None, after_event_id: str | None = None, limit: int | None = None,
    ) -> tuple[OutboxRecord, ...]:
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ValueError("outbox query limit must be between one and 10000")
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return ()
        try:
            clauses: list[str] = []
            values: list[object] = []
            if operation_id is not None:
                clauses.append("operation_id=?")
                values.append(operation_id)
            if states:
                for state in states:
                    _state(state, _OUTBOX_STATES, "outbox state")
                clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
                values.extend(states)
            if event_type is not None:
                clauses.append("event_type=?")
                values.append(event_type)
            if after_event_id is not None:
                clauses.append("event_id>?")
                values.append(after_event_id)
            query = "SELECT * FROM outbox"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY event_id" if after_event_id is not None or limit is not None else " ORDER BY created_ns,event_id"
            if limit is not None:
                query += " LIMIT ?"
                values.append(limit)
            rows = connection.execute(query, values)
            result: list[OutboxRecord] = []
            for row in rows:
                payload = _object(_decode_json(str(row["payload_json"]), "outbox payload"), "outbox payload")
                record_state = _state(str(row["state"]), _OUTBOX_STATES, "outbox state")
                result.append(
                    OutboxRecord(
                        str(row["event_id"]), str(row["operation_id"]), str(row["member_id"]),
                        str(row["event_type"]), payload, cast(OutboxState, record_state), int(row["attempts"]),
                        str(row["error"]) if row["error"] is not None else None,
                        int(row["created_ns"]), int(row["updated_ns"]),
                    )
                )
            return tuple(result)
        finally:
            connection.close()

    def enqueue_event(self, operation_id: str, member_id: str, event_type: str, payload: dict[str, object]) -> str:
        if not event_type or len(event_type) > 100:
            raise ValueError("invalid event type")
        event_id = hashlib.sha256(f"{operation_id}\0{member_id}\0{event_type}".encode()).hexdigest()
        now = time.time_ns()
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT phase FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None or str(row["phase"]) not in {"committed", "cleanup_pending", "complete"}:
                connection.rollback()
                raise StateStoreError("outbox events require a committed local operation")
            member = connection.execute(
                "SELECT 1 FROM members WHERE operation_id=? AND member_id=?",
                (operation_id, member_id),
            ).fetchone()
            if member is None:
                connection.rollback()
                raise StateStoreError("outbox event member is not journaled")
            connection.execute(
                "INSERT OR IGNORE INTO outbox(event_id,operation_id,member_id,event_type,payload_json,state,attempts,error,created_ns,updated_ns) VALUES(?,?,?,?,?,'queued',0,NULL,?,?)",
                (event_id, operation_id, member_id, event_type, _json_dumps(payload), now, now),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return event_id

    def update_outbox(self, event_id: str, state: OutboxState, *, error: str | None = None, increment_attempts: bool = False) -> None:
        _state(state, _OUTBOX_STATES, "outbox state")
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE outbox SET state=?,error=?,attempts=attempts+?,updated_ns=? WHERE event_id=?",
                (state, error, 1 if increment_attempts else 0, time.time_ns(), event_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StateStoreError("outbox event is not journaled")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def claim_job(
        self, job_id: str, kind: str, payload: dict[str, object], *, retry_pending: bool = False,
    ) -> bool:
        if not job_id or len(job_id) > 200 or not kind or len(kind) > 100:
            raise ValueError("invalid durable job identity")
        now = time.time_ns()
        encoded = _json_dumps(payload)
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO jobs(job_id,kind,state,payload_json,error,created_ns,updated_ns) "
                "VALUES(?,?,'running',?,NULL,?,?)",
                (job_id, kind, encoded, now, now),
            )
            claimed = cursor.rowcount == 1
            if not claimed and retry_pending:
                cursor = connection.execute(
                    "UPDATE jobs SET state='running',error=NULL,updated_ns=? "
                    "WHERE job_id=? AND kind=? AND state='pending' AND payload_json=?",
                    (now, job_id, kind, encoded),
                )
                claimed = cursor.rowcount == 1
            connection.commit()
            return claimed
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def put_job(self, job_id: str, kind: str, payload: dict[str, object], *, state: JobState = "pending", error: str | None = None) -> None:
        if not job_id or len(job_id) > 200 or not kind or len(kind) > 100:
            raise ValueError("invalid durable job identity")
        _state(state, _JOB_STATES, "job state")
        now = time.time_ns()
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO jobs(job_id,kind,state,payload_json,error,created_ns,updated_ns) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET kind=excluded.kind,state=excluded.state,payload_json=excluded.payload_json,error=excluded.error,updated_ns=excluded.updated_ns",
                (job_id, kind, state, _json_dumps(payload), error, now, now),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_job(self, job_id: str) -> JobRecord | None:
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return None
        try:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return _job_from_row(row) if row is not None else None
        finally:
            connection.close()

    def list_jobs(
        self, states: tuple[JobState, ...] = (), *, kind: str | None = None,
        after_job_id: str | None = None, limit: int | None = None,
    ) -> tuple[JobRecord, ...]:
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ValueError("job query limit must be between one and 10000")
        try:
            connection = self._read_connection()
        except FileNotFoundError:
            return ()
        try:
            filters: list[str] = []
            parameters: list[object] = []
            if states:
                for state in states:
                    _state(state, _JOB_STATES, "job state")
                filters.append("state IN (" + ",".join("?" for _ in states) + ")")
                parameters.extend(states)
            if kind is not None:
                filters.append("kind=?")
                parameters.append(kind)
            if after_job_id is not None:
                filters.append("job_id>?")
                parameters.append(after_job_id)
            query = "SELECT * FROM jobs"
            if filters:
                query += " WHERE " + " AND ".join(filters)
            query += " ORDER BY job_id" if after_job_id is not None or limit is not None else " ORDER BY created_ns,job_id"
            if limit is not None:
                query += " LIMIT ?"
                parameters.append(limit)
            return tuple(_job_from_row(row) for row in connection.execute(query, parameters))
        finally:
            connection.close()


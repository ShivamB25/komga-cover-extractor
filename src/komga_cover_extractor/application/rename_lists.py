"""Bounded rename dictionaries and explicitly authorized, recoverable updates."""
from __future__ import annotations

import errno
import json
import os
import select
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.operations import (
    ArtifactSpec,
    FileFingerprint,
    Operation,
    OperationPlan,
    PlanReport,
    RootPermission,
)
from ..domain.releases import ReleaseSnapshot
from ..infrastructure.filesystem import FilesystemError, checked_path
from ..infrastructure.transactions import TransactionExecutor
from ..parsing.filenames import _get_extras
from ..parsing.normalization import similar

DICTIONARY_MAX_BYTES = 1_048_576
DICTIONARY_FILES = (
    "release_groups.txt",
    "publishers.txt",
    "skipped_release_group_files.txt",
    "skipped_publisher_files.txt",
)
_DICTIONARY_FIELDS = tuple(name.removesuffix(".txt") for name in DICTIONARY_FILES)
_Mode = Literal["release_group", "publisher"]


@dataclass(frozen=True, slots=True)
class DictionaryPolicy:
    generate_release_group_list_toggle: bool = False
    manual_rename: bool = True
    publisher_similarity_score: float = 0.9
    release_group_similarity_score: float = 0.8
    publisher_excluded_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if type(self.generate_release_group_list_toggle) is not bool or type(self.manual_rename) is not bool:
            raise ValueError("dictionary controls must be booleans")
        for score in (self.publisher_similarity_score, self.release_group_similarity_score):
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                raise ValueError("dictionary similarity must be between zero and one")
        roots = tuple(self.publisher_excluded_roots)
        if any(not root.is_absolute() or ".." in root.parts for root in roots):
            raise ValueError("publisher exclusions require absolute canonical roots")
        object.__setattr__(self, "publisher_excluded_roots", roots)

    def version(self, dictionaries: RenameDictionaries) -> str:
        behavior = (
            "rename-dictionaries-v1", dictionaries.version,
            self.generate_release_group_list_toggle, self.manual_rename,
            self.publisher_similarity_score, self.release_group_similarity_score,
            tuple(str(root) for root in self.publisher_excluded_roots),
        )
        return sha256(json.dumps(behavior, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RenameDictionaries:
    release_groups: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    skipped_release_group_files: tuple[str, ...] = ()
    skipped_publisher_files: tuple[str, ...] = ()
    version: str = field(init=False)
    diagnostics: tuple[Decision, ...] = ()

    def __post_init__(self) -> None:
        values: list[tuple[str, ...]] = []
        for name in _DICTIONARY_FIELDS:
            entries = tuple(getattr(self, name))
            object.__setattr__(self, name, entries)
            values.append(entries)
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        object.__setattr__(self, "version", sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class _ListFile:
    content: bytes
    entries: tuple[str, ...]
    fingerprint: FileFingerprint


class _DictionaryError(Exception):
    def __init__(self, status: Status, reason: str, path: Path | None = None) -> None:
        self.decision = Decision(status, reason, path)
        super().__init__(reason)


def _os_error(error: OSError, path: Path) -> _DictionaryError:
    if isinstance(error, FilesystemError):
        if error.reason == "source_changed":
            return _DictionaryError("stale_plan", "dictionary_source_changed", path)
        if error.reason == "symlink_traversal":
            return _DictionaryError("invalid_input", "dictionary_symlink", path)
    if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return _DictionaryError("permission_denied", "dictionary_not_readable", path)
    return _DictionaryError("invalid_input", "dictionary_read_failed", path)


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _DictionaryError("cancelled", "dictionary_maintenance_cancelled")


def _root_exists(root: Path, *, optional: bool) -> bool:
    if not root.is_absolute() or ".." in root.parts or "\x00" in str(root):
        raise _DictionaryError("invalid_input", "dictionary_root_not_canonical")
    try:
        checked_path(root, root, allow_missing=optional)
        info = root.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise _DictionaryError("invalid_input", "dictionary_import_root_missing", root) from None
    except OSError as error:
        raise _os_error(error, root) from None
    if not stat.S_ISDIR(info.st_mode):
        raise _DictionaryError("invalid_input", "dictionary_root_not_directory", root)
    return True


def _parse_entries(content: bytes, path: Path) -> tuple[str, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _DictionaryError("invalid_input", "dictionary_invalid_utf8", path) from None
    if "\x00" in text:
        raise _DictionaryError("invalid_input", "dictionary_invalid_entry", path)
    return tuple(dict.fromkeys(line.strip() for line in text.split("\n") if line.strip()))


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_list(root: Path, name: str, cancel: Event | None) -> _ListFile | None:
    path = root / name
    _check_cancel(cancel)
    try:
        checked_path(path, root, allow_missing=True)
        try:
            observed = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(observed.st_mode):
            raise _DictionaryError("invalid_input", "dictionary_not_regular_file", path)
        if observed.st_size > DICTIONARY_MAX_BYTES:
            raise _DictionaryError("invalid_input", "dictionary_byte_limit", path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise _DictionaryError("invalid_input", "dictionary_not_regular_file", path)
            if before.st_size > DICTIONARY_MAX_BYTES:
                raise _DictionaryError("invalid_input", "dictionary_byte_limit", path)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(DICTIONARY_MAX_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
        if _file_identity(observed) != _file_identity(before) or _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current) or len(content) != before.st_size:
            raise _DictionaryError("stale_plan", "dictionary_source_changed", path)
        if len(content) > DICTIONARY_MAX_BYTES:
            raise _DictionaryError("invalid_input", "dictionary_byte_limit", path)
        _check_cancel(cancel)
        entries = _parse_entries(content, path)
        observation = FileFingerprint(root, name, before.st_dev, before.st_ino, len(content), before.st_mtime_ns, sha256(content).hexdigest())
        return _ListFile(content, entries, observation)
    except OSError as error:
        raise _os_error(error, path) from None


def _load_state(root: Path, cancel: Event | None = None) -> tuple[RenameDictionaries, dict[str, _ListFile]]:
    files: dict[str, _ListFile] = {}
    diagnostics = []
    try:
        _check_cancel(cancel)
        _root_exists(root, optional=True)
    except _DictionaryError as error:
        return RenameDictionaries(diagnostics=(error.decision,)), files
    for name, field_name in zip(DICTIONARY_FILES, _DICTIONARY_FIELDS, strict=True):
        try:
            observed = _read_list(root, name, cancel)
        except _DictionaryError as error:
            diagnostics.append(error.decision)
            continue
        if observed is None:
            diagnostics.append(Decision("observed", f"missing_{field_name}", root / name))
        else:
            files[name] = observed
    snapshot = RenameDictionaries(
        release_groups=files["release_groups.txt"].entries if "release_groups.txt" in files else (),
        publishers=files["publishers.txt"].entries if "publishers.txt" in files else (),
        skipped_release_group_files=files["skipped_release_group_files.txt"].entries if "skipped_release_group_files.txt" in files else (),
        skipped_publisher_files=files["skipped_publisher_files.txt"].entries if "skipped_publisher_files.txt" in files else (),
        diagnostics=tuple(diagnostics),
    )
    return snapshot, files


def load_dictionaries(state_dir: Path) -> RenameDictionaries:
    """Observe only the four optional lists without initializing persistent state."""
    return _load_state(state_dir)[0]


def _strip_brackets(value: str) -> str:
    if value and value[0] in "([{":
        value = value[1:]
    if value and value[-1] in ")]}":
        value = value[:-1]
    return value


def _skip_signature(name: str) -> tuple[str, tuple[str, ...]]:
    extension = Path(name).suffix
    extras = _get_extras(name, kind="volume", series_name="", subtitle="", extension=extension)
    return extension, tuple(value.lower() for value in sorted(extras))


def _release_signature(release: ReleaseSnapshot) -> tuple[str, tuple[str, ...]]:
    return release.path.suffix, tuple(value.lower() for value in sorted(release.extras))


def _interactive_reader(cancel: Event) -> Callable[[str], str]:
    input_stream, output = sys.stdin, sys.stderr
    try:
        if not input_stream.isatty() or not output.isatty():
            raise _DictionaryError("review_required", "dictionary_interactive_terminal_required")
        descriptor = input_stream.fileno()
    except (OSError, ValueError):
        raise _DictionaryError("review_required", "dictionary_interactive_terminal_required") from None
    pending = bytearray()

    def read_choice(prompt: str) -> str:
        output.write(prompt)
        output.flush()
        while True:
            _check_cancel(cancel)
            newline = pending.find(b"\n")
            if newline >= 0:
                line = bytes(pending[:newline])
                del pending[:newline + 1]
                if len(line) > DICTIONARY_MAX_BYTES:
                    raise _DictionaryError("invalid_input", "dictionary_input_byte_limit")
                try:
                    return line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    raise _DictionaryError("invalid_input", "dictionary_invalid_utf8") from None
            if len(pending) > DICTIONARY_MAX_BYTES:
                raise _DictionaryError("invalid_input", "dictionary_input_byte_limit")
            try:
                ready, _, _ = select.select((descriptor,), (), (), 0.2)
                if not ready:
                    continue
                chunk = os.read(descriptor, min(4096, DICTIONARY_MAX_BYTES + 1 - len(pending)))
            except (OSError, ValueError):
                raise _DictionaryError("review_required", "dictionary_interactive_input_unavailable") from None
            if not chunk:
                raise _DictionaryError("cancelled", "dictionary_interactive_input_closed")
            pending.extend(chunk)

    return read_choice


def _suggestions(release: ReleaseSnapshot, mode: _Mode, known: Sequence[str], policy: DictionaryPolicy) -> tuple[int, ...]:
    item = release.release_group if mode == "release_group" else release.publisher
    evidence = tuple(value for value in (item, *(_strip_brackets(extra) for extra in release.extras)) if value)
    threshold = policy.release_group_similarity_score if mode == "release_group" else policy.publisher_similarity_score
    return tuple(index for index, entry in enumerate(known, start=1) if any(similar(value, entry) >= threshold for value in evidence))


def _choose_entry(read_choice: Callable[[str], str], release: ReleaseSnapshot, mode: _Mode, known: Sequence[str], suggestions: tuple[int, ...]) -> str:
    safe_name = "".join(character for character in release.name if character.isprintable())
    sys.stderr.write(f"\nChecking {safe_name}\n")
    for index in suggestions:
        entry = "".join(character for character in known[index - 1] if character.isprintable())
        sys.stderr.write(f"Known entry {index} = {entry}\n")
    while True:
        choice = read_choice(f"Enter {mode.replace('_', ' ')}, entry NUMBER, none, or skip > ")
        if choice.startswith("entry "):
            number = choice.removeprefix("entry ")
            if number.isascii() and number.isdigit() and len(number) < 10 and 1 <= int(number) <= len(known):
                return known[int(number) - 1]
            sys.stderr.write("Invalid known entry selection.\n")
            continue
        choice = choice.replace("\t", "").strip()
        if choice and all(character.isprintable() for character in choice):
            return choice
        sys.stderr.write("A nonempty dictionary entry is required.\n")


def _discover(
    releases: Sequence[ReleaseSnapshot], dictionaries: RenameDictionaries, *, policy: DictionaryPolicy,
    cancel: Event, read_choice: Callable[[str], str] | None,
) -> tuple[dict[str, list[str]], list[Decision]]:
    entries = {
        "release_groups.txt": list(dictionaries.release_groups),
        "publishers.txt": list(dictionaries.publishers),
        "skipped_release_group_files.txt": list(dictionaries.skipped_release_group_files),
        "skipped_publisher_files.txt": list(dictionaries.skipped_publisher_files),
    }
    decisions: list[Decision] = []
    modes: tuple[_Mode, ...] = ("release_group", "publisher")
    if read_choice is not None:
        while True:
            choice = read_choice("Dictionary mode, 1 release group, 2 publisher, 3 exit > ")
            if choice in {"1", "2", "3"}:
                break
            sys.stderr.write("Choose mode 1, 2, or 3.\n")
        if choice == "3":
            return entries, [Decision("skipped", "dictionary_discovery_declined")]
        modes = ("release_group",) if choice == "1" else ("publisher",)
    for mode in modes:
        known = entries["release_groups.txt" if mode == "release_group" else "publishers.txt"]
        skipped = entries[f"skipped_{mode}_files.txt"]
        signatures = {_skip_signature(name) for name in skipped}
        skipped_names = set(skipped)
        known_lower = {entry.lower() for entry in known}
        for release in releases:
            _check_cancel(cancel)
            if mode == "publisher" and (release.kind == "chapter" or any(release.path.is_relative_to(root) for root in policy.publisher_excluded_roots)):
                decisions.append(Decision("skipped", "publisher_chapter_scope_excluded", release.path))
                continue
            if release.name in skipped_names:
                decisions.append(Decision("skipped", f"{mode}_file_previously_skipped", release.path))
                continue
            item = release.release_group if mode == "release_group" else release.publisher
            signature = _release_signature(release)
            if not item and signature in signatures:
                skipped.append(release.name)
                skipped_names.add(release.name)
                decisions.append(Decision("planned", f"{mode}_skipped_extras_match", release.path))
                continue
            if (item and item.lower() in known_lower) or any(_strip_brackets(extra).lower() in known_lower for extra in release.extras):
                decisions.append(Decision("skipped", f"known_{mode}", release.path))
                continue
            suggestions = _suggestions(release, mode, known, policy)
            if read_choice is None:
                candidates = tuple(f"known_entry_{index}" for index in suggestions) + ("new_entry", "none", "skip")
                decisions.append(Decision("review_required", f"unknown_{mode}", release.path, candidates))
                continue
            choice = _choose_entry(read_choice, release, mode, known, suggestions)
            if choice == "none":
                skipped.append(release.name)
                skipped_names.add(release.name)
                signatures.add(signature)
                decisions.append(Decision("planned", f"{mode}_none_persisted", release.path))
            elif choice == "skip":
                decisions.append(Decision("skipped", f"{mode}_skipped_for_run", release.path))
            elif choice.lower() in known_lower:
                decisions.append(Decision("skipped", f"known_{mode}_selected", release.path))
            else:
                known.append(choice)
                known_lower.add(choice.lower())
                decisions.append(Decision("planned", f"new_{mode}_entry", release.path))
    return entries, decisions


def _physical_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    unique = sorted(set(roots), key=lambda root: (len(root.parts), str(root)))
    return tuple(root for root in unique if not any(root != parent and root.is_relative_to(parent) for parent in unique))


def _publish(
    operations: tuple[Operation, ...], *, state_dir: Path, import_from: Path | None,
    dictionaries: RenameDictionaries, policy: DictionaryPolicy, authorization: RunAuthorization, cancel: Event,
) -> RunResult:
    permissions: tuple[RootPermission, ...] = (RootPermission(state_dir),)
    roots: tuple[Path, ...] = (state_dir,)
    if import_from is not None and import_from != state_dir:
        permissions += (RootPermission(import_from, write=False),)
        roots = _physical_roots((state_dir, import_from))
    plan = OperationPlan(policy.version(dictionaries), f"dictionaries-{uuid4().hex}", operations, permissions, reason="rename_dictionary_import" if import_from is not None else "rename_dictionary_update")
    executor = TransactionExecutor(state_dir, roots, cancel=cancel, max_stage_bytes=8 * DICTIONARY_MAX_BYTES)
    if authorization.apply:
        prepared = executor.prepare_state_root(authorization=authorization)
        if prepared.status != "observed":
            return RunResult((prepared,), failed=int(prepared.status in {"publication_failed", "permission_denied", "failed"}),
                             plan_reports=(PlanReport(plan),))
    elif not state_dir.exists():
        return RunResult((Decision("planned", "dictionary_state_root_requires_apply", state_dir),),
                         plan_reports=(PlanReport(plan),))
    result = executor.apply(plan, authorization=authorization)
    return RunResult(result.decisions, committed=result.committed, skipped=result.skipped, failed=result.failed,
                     deferred_deliveries=result.deferred_deliveries, plan_reports=(PlanReport(plan),))


def maintain_rename_lists(
    releases: Sequence[ReleaseSnapshot], *, state_dir: Path, policy: DictionaryPolicy,
    authorization: RunAuthorization, cancel: Event, interactive: bool = False, import_from: Path | None = None,
) -> RunResult:
    """Import unchanged legacy lists or collect explicit choices and publish a group."""
    dictionaries, files = _load_state(state_dir, cancel)
    diagnostics = list(dictionaries.diagnostics)
    if any(decision.status != "observed" for decision in diagnostics):
        return RunResult(tuple(diagnostics))
    operations: list[Operation] = []
    decisions: list[Decision] = []
    try:
        _check_cancel(cancel)
        if import_from is not None:
            _root_exists(import_from, optional=False)
            imported, source_files = _load_state(import_from, cancel)
            if any(decision.status != "observed" for decision in imported.diagnostics):
                return RunResult(tuple(diagnostics) + imported.diagnostics)
            diagnostics.extend(Decision(item.status, item.reason.replace("missing_", "missing_import_", 1), item.path) for item in imported.diagnostics)
            for name in DICTIONARY_FILES:
                source = source_files.get(name)
                if source is None:
                    continue
                destination = files.get(name)
                if destination is not None:
                    if source.content == destination.content:
                        decisions.append(Decision("skipped", "dictionary_import_identical", state_dir / name))
                        continue
                    decisions.append(Decision("conflict", "dictionary_import_destination_conflict", state_dir / name))
                    continue
                operations.append(Operation(name.removesuffix(".txt"), "publish", state_dir / name,
                                            source=source.fingerprint, artifact=ArtifactSpec(source=source.fingerprint, media_type="text/plain; charset=utf-8"), reason="import_rename_dictionary"))
            if any(item.status == "conflict" for item in decisions):
                return RunResult(tuple(diagnostics + decisions), skipped=sum(item.status == "skipped" for item in decisions))
        elif policy.generate_release_group_list_toggle:
            read_choice = _interactive_reader(cancel) if interactive else None
            entries, decisions = _discover(releases, dictionaries, policy=policy, cancel=cancel, read_choice=read_choice)
            for name, field_name in zip(DICTIONARY_FILES, _DICTIONARY_FIELDS, strict=True):
                before_entries = getattr(dictionaries, field_name)
                additions = entries[name][len(before_entries):]
                if not additions:
                    continue
                observed = files.get(name)
                content = observed.content if observed is not None else b""
                if any(entry != entry.strip() or "\n" in entry or "\r" in entry or "\x00" in entry for entry in additions):
                    raise _DictionaryError("invalid_input", "dictionary_invalid_entry", state_dir / name)
                try:
                    content += b"".join(b"\n" + entry.encode("utf-8") for entry in additions)
                except UnicodeEncodeError:
                    raise _DictionaryError("invalid_input", "dictionary_invalid_utf8", state_dir / name) from None
                if len(content) > DICTIONARY_MAX_BYTES:
                    raise _DictionaryError("invalid_input", "dictionary_byte_limit", state_dir / name)
                operations.append(Operation(name.removesuffix(".txt"), "publish", state_dir / name,
                                            destination_before=observed.fingerprint if observed is not None else None,
                                            artifact=ArtifactSpec(content=content, media_type="text/plain; charset=utf-8"),
                                            collision="replace" if observed is not None else "absent", reason="maintain_rename_dictionary"))
        else:
            decisions.append(Decision("skipped", "dictionary_generation_disabled"))
        _check_cancel(cancel)
    except _DictionaryError as error:
        return RunResult(tuple(diagnostics) + (error.decision,))
    if not operations:
        if not decisions:
            decisions.append(Decision("skipped", "dictionary_no_changes"))
        return RunResult(tuple(diagnostics + decisions), skipped=sum(item.status == "skipped" for item in decisions))
    applied = _publish(tuple(operations), state_dir=state_dir, import_from=import_from,
                       dictionaries=dictionaries, policy=policy, authorization=authorization, cancel=cancel)
    intended = []
    for item in decisions:
        if item.status == "planned" and applied.committed:
            intended.append(Decision("committed", item.reason, item.path, item.candidates))
        elif item.status == "planned" and applied.exit_code != 0:
            intended.append(Decision("observed", "dictionary_change_not_committed", item.path))
        else:
            intended.append(item)
    return RunResult(tuple(diagnostics + intended) + applied.decisions, committed=applied.committed,
                     skipped=applied.skipped + sum(item.status == "skipped" for item in decisions), failed=applied.failed,
                     deferred_deliveries=applied.deferred_deliveries, plan_reports=applied.plan_reports)

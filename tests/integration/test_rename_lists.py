from __future__ import annotations

import json
import os
import stat
import struct
import sys
import termios
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from hashlib import sha256
from io import StringIO
from pathlib import Path
from threading import Event, Timer

import pytest
import regex

from komga_cover_extractor.application import rename_lists
from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.application.rename_lists import (
    DictionaryPolicy,
    load_dictionaries,
    maintain_rename_lists,
)
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.config import validate_config
from komga_cover_extractor.domain.decisions import RunAuthorization, RunResult
from komga_cover_extractor.domain.operations import OperationPlan
from komga_cover_extractor.domain.releases import FileObservation, ReleaseSnapshot
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.parsing.filenames import (
    ParsingPolicy,
    parse_release,
    render_release,
)

_NAMES = (
    "release_groups.txt", "publishers.txt",
    "skipped_release_group_files.txt", "skipped_publisher_files.txt",
)


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def _book(root: Path, name: str, *, metadata: tuple[tuple[str, str], ...] = (), parsing: ParsingPolicy | None = None) -> ReleaseSnapshot:
    path = root / "Series" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("01.png", _png())
        archive.writestr("ComicInfo.xml", "<ComicInfo><Title>Series</Title></ComicInfo>")
    observation = FileObservation(path, metadata=metadata, root_role="library", fingerprint=fingerprint(path, root))
    return parse_release(observation, parsing or ParsingPolicy(chapter_support_toggle=True))


def _root(tmp_path: Path, name: str = "state") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def _manifest(root: Path) -> dict[str, object]:
    if not root.exists():
        return {}
    result: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            result[name] = ("link", str(path.readlink()))
        elif stat.S_ISREG(info.st_mode):
            result[name] = ("file", path.read_bytes(), info.st_mtime_ns)
        elif stat.S_ISDIR(info.st_mode):
            result[name] = ("directory", stat.S_IMODE(info.st_mode))
        else:
            result[name] = ("special", stat.S_IFMT(info.st_mode))
    return result


def _run(state_dir: Path, releases: Sequence[ReleaseSnapshot] = (), *, apply: bool = False,
         interactive: bool = False, import_from: Path | None = None,
         policy: DictionaryPolicy | None = None, cancel: Event | None = None) -> RunResult:
    return maintain_rename_lists(releases, state_dir=state_dir,
                                 policy=policy or DictionaryPolicy(generate_release_group_list_toggle=True),
                                 authorization=RunAuthorization(apply=apply), cancel=cancel or Event(),
                                 interactive=interactive, import_from=import_from)


@contextmanager
def _terminal(monkeypatch: pytest.MonkeyPatch, choices: str = "") -> Iterator[bytearray]:
    master, slave = os.openpty()
    attributes = termios.tcgetattr(slave)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attributes)
    input_stream = os.fdopen(os.dup(slave), "r", encoding="utf-8")
    output_stream = os.fdopen(os.dup(slave), "w", encoding="utf-8")
    os.close(slave)
    captured = bytearray()
    try:
        if choices:
            os.write(master, choices.encode("utf-8"))
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", input_stream)
            patch.setattr(sys, "stderr", output_stream)
            yield captured
        output_stream.flush()
        os.set_blocking(master, False)
        while True:
            try:
                chunk = os.read(master, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            captured.extend(chunk)
    finally:
        input_stream.close()
        output_stream.close()
        os.close(master)


def test_missing_state_is_read_only_with_four_named_optional_diagnostics(tmp_path: Path) -> None:
    state = tmp_path / "new-state"
    before = _manifest(tmp_path)
    loaded = load_dictionaries(state)
    assert (loaded.release_groups, loaded.publishers, loaded.skipped_release_group_files,
            loaded.skipped_publisher_files) == ((), (), (), ())
    assert [(item.status, item.reason) for item in loaded.diagnostics] == [
        ("observed", "missing_release_groups"), ("observed", "missing_publishers"),
        ("observed", "missing_skipped_release_group_files"), ("observed", "missing_skipped_publisher_files"),
    ]
    assert _manifest(tmp_path) == before
    assert not state.exists()


def test_load_preserves_case_and_order_and_deduplicates_only_exact_trimmed_entries(tmp_path: Path) -> None:
    state = _root(tmp_path)
    (state / "release_groups.txt").write_bytes(b"\n Team Z \nTeam A\nTeam Z\nteam z\n \n")
    (state / "publishers.txt").write_bytes("Éditions Soleil\nAcme\nÉditions Soleil\n".encode())
    (state / "skipped_release_group_files.txt").write_text("First v01.cbz\nSecond v02.cbz\nFirst v01.cbz\n", encoding="utf-8")
    (state / "skipped_publisher_files.txt").write_text("Other v01.epub\n", encoding="utf-8")
    before = _manifest(state)
    loaded = load_dictionaries(state)
    assert loaded.release_groups == ("Team Z", "Team A", "team z")
    assert loaded.publishers == ("Éditions Soleil", "Acme")
    assert loaded.skipped_release_group_files == ("First v01.cbz", "Second v02.cbz")
    assert loaded.skipped_publisher_files == ("Other v01.epub",)
    assert loaded.diagnostics == ()
    assert _manifest(state) == before
    assert loaded.version == load_dictionaries(state).version
    (state / "release_groups.txt").write_text("Team A\nTeam Z\nteam z\n", encoding="utf-8")
    reordered = load_dictionaries(state)
    assert reordered.release_groups == ("Team A", "Team Z", "team z")
    assert reordered.version != loaded.version
    (state / "release_groups.txt").write_text("Team A\nTeam Z\nTeam Z\n", encoding="utf-8")
    recased = load_dictionaries(state)
    assert recased.release_groups == ("Team A", "Team Z")
    assert recased.version != reordered.version


@pytest.mark.parametrize(("size", "expected"), [(1_048_576, "observed"), (1_048_577, "invalid_input")])
def test_each_list_has_an_exact_one_mib_boundary(tmp_path: Path, size: int, expected: str) -> None:
    state = _root(tmp_path)
    for name in _NAMES:
        (state / name).write_bytes(b"X" * size)
    before = _manifest(state)
    loaded = load_dictionaries(state)
    if expected == "observed":
        assert loaded.release_groups == ("X" * size,)
        assert loaded.publishers == ("X" * size,)
        assert loaded.skipped_release_group_files == ("X" * size,)
        assert loaded.skipped_publisher_files == ("X" * size,)
        assert loaded.diagnostics == ()
    else:
        assert [(item.status, item.reason) for item in loaded.diagnostics] == [("invalid_input", "dictionary_byte_limit")] * 4
    assert _manifest(state) == before


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize(("bad_bytes", "reason"), [(b"Team\xffsecret", "dictionary_invalid_utf8"), (b"Team\x00hidden", "dictionary_invalid_entry")])
def test_invalid_dictionary_text_is_typed_and_never_reported_as_empty_success(tmp_path: Path, name: str, bad_bytes: bytes, reason: str) -> None:
    state = _root(tmp_path)
    (state / name).write_bytes(bad_bytes)
    before = _manifest(tmp_path)
    loaded = load_dictionaries(state)
    assert [(item.status, item.reason, item.path) for item in loaded.diagnostics if item.status != "observed"] == [("invalid_input", reason, state / name)]
    result = _run(state, apply=True)
    assert (result.exit_code, result.planned, result.committed) == (2, 0, 0)
    assert "secret" not in json.dumps(result.as_dict())
    assert _manifest(tmp_path) == before


@pytest.mark.parametrize("unsafe", ["symlink", "fifo", "directory"])
def test_unsafe_dictionary_entries_fail_before_state_initialization(tmp_path: Path, unsafe: str) -> None:
    state = _root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Untouched\n")
    target = state / "release_groups.txt"
    if unsafe == "symlink":
        target.symlink_to(outside)
        reason = "dictionary_symlink"
    elif unsafe == "fifo":
        os.mkfifo(target)
        reason = "dictionary_not_regular_file"
    else:
        target.mkdir()
        reason = "dictionary_not_regular_file"
    before = _manifest(tmp_path)
    result = _run(state, apply=True)
    assert result.exit_code == 2
    assert any((item.status, item.reason) == ("invalid_input", reason) for item in result.decisions)
    assert _manifest(tmp_path) == before
    assert outside.read_bytes() == b"Untouched\n"


@pytest.mark.parametrize("linked", ["root", "parent"])
def test_state_symlink_components_are_rejected_without_traversal(tmp_path: Path, linked: str) -> None:
    real = _root(tmp_path, "real")
    (real / "release_groups.txt").write_text("Do not touch\n", encoding="utf-8")
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    state = link if linked == "root" else link / "nested-state"
    before = _manifest(tmp_path)
    result = _run(state, apply=True)
    assert (result.exit_code, result.planned, result.committed) == (2, 0, 0)
    assert any(item.reason == "dictionary_symlink" for item in result.decisions)
    assert _manifest(tmp_path) == before


def test_import_preview_then_apply_preserves_exact_files_and_ignores_cached_paths(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy-logs")
    contents = {
        "release_groups.txt": b"\nLast\nFirst\nLast\nlast\n",
        "publishers.txt": "Éditions Soleil\nAcme\n".encode(),
        "skipped_release_group_files.txt": b"Z v03.cbz\nA v01.cbz\n",
        "skipped_publisher_files.txt": b"Z v03.epub\nA v01.epub\n",
    }
    for name, content in contents.items():
        (source / name).write_bytes(content)
    (source / "cached_paths.txt").write_bytes(b"/untrusted/library\n")
    (source / "settings.py").write_bytes(b"raise RuntimeError('must never execute')\n")
    book = _book(tmp_path / "library", "Series v01 (First).cbz")
    original_source = _manifest(source)
    original_library = _manifest(tmp_path / "library")
    state = tmp_path / "new-state"
    before = _manifest(tmp_path)
    preview = _run(state, (book,), import_from=source)
    assert (preview.exit_code, preview.planned, preview.committed) == (0, 1, 0)
    assert _manifest(tmp_path) == before
    assert not state.exists()
    plan = preview.plan_reports[0].as_dict()
    assert plan["reason"] == "rename_dictionary_import"
    operations = plan["operations"]
    assert isinstance(operations, list)
    assert [item["destination"] for item in operations] == [str(state / name) for name in _NAMES]
    assert operations[0]["source"]["root"] == str(source)
    assert operations[0]["source"]["relative_path"] == "release_groups.txt"
    assert operations[0]["source"]["sha256"] == sha256(contents["release_groups.txt"]).hexdigest()
    assert operations[0]["artifact"]["source"] == operations[0]["source"]
    applied = _run(state, (book,), apply=True, import_from=source)
    assert (applied.exit_code, applied.planned, applied.committed, applied.failed) == (0, 1, 1, 0)
    assert {name: (state / name).read_bytes() for name in _NAMES} == contents
    assert not (state / "cached_paths.txt").exists()
    assert not (state / "settings.py").exists()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert _manifest(source) == original_source
    assert _manifest(tmp_path / "library") == original_library
    snapshot = load_dictionaries(state)
    assert snapshot.release_groups == ("Last", "First", "last")
    assert snapshot.publishers == ("Éditions Soleil", "Acme")
    record = StateStore(state).get_operation(applied.plan_reports[0].operation_id)
    assert record is not None and record.phase == "complete"
    committed_state = _manifest(state)
    repeated = _run(state, apply=True, import_from=source)
    assert (repeated.exit_code, repeated.planned, repeated.committed, repeated.skipped) == (0, 0, 0, 4)
    assert _manifest(state) == committed_state
    assert _manifest(source) == original_source
    assert _manifest(tmp_path / "library") == original_library


def test_missing_optional_import_lists_stay_absent_with_named_diagnostics(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy")
    (source / "publishers.txt").write_bytes(b"Publisher\n")
    state = tmp_path / "state"
    applied = _run(state, apply=True, import_from=source)
    assert (applied.exit_code, applied.committed, applied.planned) == (0, 1, 1)
    assert (state / "publishers.txt").read_bytes() == b"Publisher\n"
    assert not (state / "release_groups.txt").exists()
    assert not (state / "skipped_release_group_files.txt").exists()
    assert not (state / "skipped_publisher_files.txt").exists()
    assert [item.reason for item in applied.decisions if item.reason.startswith("missing_import_")] == [
        "missing_import_release_groups", "missing_import_skipped_release_group_files", "missing_import_skipped_publisher_files",
    ]


def test_empty_import_is_read_only_even_under_apply(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy")
    (source / "cached_paths.txt").write_bytes(b"/stale/path")
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    result = _run(state, apply=True, import_from=source)
    assert (result.exit_code, result.planned, result.committed, result.skipped) == (0, 0, 0, 1)
    assert _manifest(tmp_path) == before
    assert not state.exists()


@pytest.mark.parametrize("apply", [False, True])
def test_import_conflict_preserves_all_destinations_and_does_not_apply_nonconflicting_members(tmp_path: Path, apply: bool) -> None:
    source = _root(tmp_path, "legacy")
    (source / "release_groups.txt").write_bytes(b"Incoming\n")
    (source / "publishers.txt").write_bytes(b"New Publisher\n")
    state = _root(tmp_path)
    (state / "release_groups.txt").write_bytes(b"Custom\n")
    before = _manifest(tmp_path)
    result = _run(state, apply=apply, import_from=source)
    assert (result.exit_code, result.committed, result.planned) == (3, 0, 0)
    assert any((item.status, item.reason) == ("conflict", "dictionary_import_destination_conflict") for item in result.decisions)
    assert _manifest(tmp_path) == before
    assert not (state / "publishers.txt").exists()


def test_normalized_equal_but_byte_different_import_is_still_a_conflict(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy")
    state = _root(tmp_path)
    (source / "release_groups.txt").write_bytes(b" Team \nTeam\n")
    (state / "release_groups.txt").write_bytes(b"Team\n")
    result = _run(state, apply=True, import_from=source)
    assert result.exit_code == 3
    assert (state / "release_groups.txt").read_bytes() == b"Team\n"
    assert (source / "release_groups.txt").read_bytes() == b" Team \nTeam\n"
    assert not (state / "state.sqlite3").exists()


@pytest.mark.parametrize("bad", ["missing_root", "root_symlink", "file_symlink", "fifo", "invalid_utf8", "too_large"])
def test_unsafe_import_is_refused_before_any_destination_or_journal_write(tmp_path: Path, bad: str) -> None:
    source = _root(tmp_path, "legacy")
    root = source
    if bad == "missing_root":
        root = tmp_path / "absent"
    elif bad == "root_symlink":
        root = tmp_path / "linked"
        root.symlink_to(source, target_is_directory=True)
    elif bad == "file_symlink":
        outside = tmp_path / "secret"
        outside.write_bytes(b"do not echo")
        (source / "release_groups.txt").symlink_to(outside)
    elif bad == "fifo":
        os.mkfifo(source / "release_groups.txt")
    elif bad == "invalid_utf8":
        (source / "release_groups.txt").write_bytes(b"private\xfftoken")
    else:
        (source / "release_groups.txt").write_bytes(b"X" * 1_048_577)
    (source / "publishers.txt").write_bytes(b"Good\n")
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    result = _run(state, apply=True, import_from=root)
    assert (result.exit_code, result.planned, result.committed) == (2, 0, 0)
    assert "private" not in json.dumps(result.as_dict())
    assert _manifest(tmp_path) == before
    assert not state.exists()


def test_noninteractive_discovery_reports_unknowns_without_prompt_or_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Unknown).cbz", metadata=(("Publisher", "New Publisher"),))
    monkeypatch.setattr(sys, "stdin", StringIO("this is not authorization\n"))
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    result = _run(state, (release,), apply=True)
    assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
    assert [(item.status, item.reason) for item in result.decisions if item.status == "review_required"] == [
        ("review_required", "unknown_release_group"), ("review_required", "unknown_publisher"),
    ]
    assert sys.stdin.tell() == 0
    assert _manifest(tmp_path) == before
    assert not state.exists()


@pytest.mark.parametrize("missing_tty", ["stdin", "stderr"])
def test_interactive_requires_both_input_and_diagnostic_terminals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_tty: str) -> None:
    release = _book(tmp_path / "library", "Series v01 (Unknown).cbz")
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    with _terminal(monkeypatch, "1\nnone\n"):
        with monkeypatch.context() as non_terminal:
            non_terminal.setattr(sys, missing_tty, StringIO())
            result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
    assert any(item.reason == "dictionary_interactive_terminal_required" for item in result.decisions)
    assert _manifest(tmp_path) == before


def test_terminal_presence_without_interactive_never_consumes_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Unknown).cbz")
    state = tmp_path / "state"
    with _terminal(monkeypatch, "1\nnone\n") as output:
        result = _run(state, (release,), apply=True)
    assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
    assert output == b""
    assert not state.exists()


@pytest.mark.parametrize("manual", [False, True])
def test_interactive_none_skip_new_and_known_choices_persist_in_order_independent_of_manual_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual: bool) -> None:
    state = _root(tmp_path)
    (state / "release_groups.txt").write_bytes(b"Known\n")
    releases = tuple(_book(tmp_path / "library", name) for name in (
        "Series v01 (NoTeam).cbz", "Series v02 (Undecided).cbz",
        "Series v03 (NewTeam).cbz", "Series v04 (NewTeam).cbz", "Series v05 (Unclear).cbz",
    ))
    before_library = _manifest(tmp_path / "library")
    old = load_dictionaries(state)
    policy = DictionaryPolicy(generate_release_group_list_toggle=True, manual_rename=manual)
    with _terminal(monkeypatch, "1\nnone\nskip\nNewTeam\nentry 1\n"):
        result = _run(state, releases, apply=True, interactive=True, policy=policy)
    assert (result.exit_code, result.planned, result.committed, result.failed) == (0, 1, 1, 0)
    assert (state / "release_groups.txt").read_bytes() == b"Known\n\nNewTeam"
    assert (state / "skipped_release_group_files.txt").read_bytes() == b"\nSeries v01 (NoTeam).cbz"
    assert not (state / "publishers.txt").exists()
    assert not (state / "skipped_publisher_files.txt").exists()
    assert _manifest(tmp_path / "library") == before_library
    refreshed = load_dictionaries(state)
    assert refreshed.release_groups == ("Known", "NewTeam")
    assert refreshed.skipped_release_group_files == ("Series v01 (NoTeam).cbz",)
    assert refreshed.version != old.version
    assert policy.version(refreshed) != policy.version(old)
    reasons = {item.path: item.reason for item in result.decisions if item.path in {release.path for release in releases}}
    assert reasons == {
        releases[0].path: "release_group_none_persisted", releases[1].path: "release_group_skipped_for_run",
        releases[2].path: "new_release_group_entry", releases[3].path: "known_release_group",
        releases[4].path: "known_release_group_selected",
    }
    rebuilt = ParsingPolicy(release_groups=tuple(regex.escape(entry) for entry in refreshed.release_groups))
    reparsed = parse_release(releases[3].observation, rebuilt)
    assert reparsed.release_group == "NewTeam"
    members = StateStore(state).list_members(result.plan_reports[0].operation_id)
    replaced = next(member for member in members if member.member_id == "release_groups")
    assert replaced.backup_path is not None
    assert replaced.backup_path.read_bytes() == b"Known\n"


def test_interactive_preview_does_not_persist_none_or_new_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    releases = tuple(_book(tmp_path / "library", name) for name in ("Series v01 (NoneTeam).cbz", "Series v02 (NewTeam).cbz"))
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    with _terminal(monkeypatch, "1\nnone\nNewTeam\n"):
        result = _run(state, releases, interactive=True)
    assert (result.exit_code, result.planned, result.committed) == (0, 1, 0)
    assert _manifest(tmp_path) == before
    assert not state.exists()
    plan = result.plan_reports[0].as_dict()
    operations = plan["operations"]
    assert isinstance(operations, list)
    assert {item["destination"] for item in operations} == {str(state / "release_groups.txt"), str(state / "skipped_release_group_files.txt")}
    assert all("content_base64" not in item["artifact"] for item in operations)


def test_skip_is_not_persistent_and_remains_reviewable_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Mystery).cbz")
    state = tmp_path / "state"
    with _terminal(monkeypatch, "1\nskip\n"):
        skipped = _run(state, (release,), apply=True, interactive=True)
    assert (skipped.exit_code, skipped.planned, skipped.committed, skipped.skipped) == (0, 0, 0, 1)
    assert not state.exists()
    pending = _run(state, (release,))
    assert pending.exit_code == 3
    assert any(item.reason == "unknown_release_group" for item in pending.decisions)


def test_exit_mode_creates_no_state_and_changes_no_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Mystery).cbz")
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    with _terminal(monkeypatch, "3\n"):
        result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.planned, result.committed, result.skipped) == (0, 0, 0, 1)
    assert _manifest(tmp_path) == before


def test_publisher_generation_uses_the_existing_menu_and_excludes_chapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _book(tmp_path / "library", "Series v01.cbz", metadata=(("Publisher", "Acme Books"),))
    chapter = _book(tmp_path / "library", "Series c002.cbz", metadata=(("Publisher", "Other Publisher"),))
    latest = _book(tmp_path / "library", "Series v03.cbz", metadata=(("Publisher", "acme books"),))
    assert chapter.kind == "chapter"
    state = tmp_path / "state"
    with _terminal(monkeypatch, "2\nAcme Books\n"):
        result = _run(state, (first, chapter, latest), apply=True, interactive=True)
    assert (result.exit_code, result.planned, result.committed) == (0, 1, 1)
    assert (state / "publishers.txt").read_bytes() == b"\nAcme Books"
    assert not (state / "release_groups.txt").exists()
    assert not (state / "skipped_publisher_files.txt").exists()
    assert any(item.path == chapter.path and item.reason == "publisher_chapter_scope_excluded" for item in result.decisions)
    assert any(item.path == latest.path and item.reason == "known_publisher" for item in result.decisions)


def test_publisher_none_uses_only_the_publisher_skip_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (NoPublisher).cbz")
    state = tmp_path / "state"
    with _terminal(monkeypatch, "2\nnone\n"):
        result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.committed) == (0, 1)
    assert (state / "skipped_publisher_files.txt").read_bytes() == b"\nSeries v01 (NoPublisher).cbz"
    assert not (state / "skipped_release_group_files.txt").exists()


def test_publisher_chapter_root_restriction_uses_components_not_substrings(tmp_path: Path) -> None:
    chapter_root = tmp_path / "chapters"
    excluded = _book(chapter_root, "Series v01.cbz")
    eligible = _book(tmp_path / "chapters-old", "Series v02.cbz")
    state = tmp_path / "state"
    policy = DictionaryPolicy(generate_release_group_list_toggle=True, publisher_excluded_roots=(chapter_root,))
    result = _run(state, (excluded, eligible), policy=policy)
    assert any(item.path == excluded.path and item.reason == "publisher_chapter_scope_excluded" for item in result.decisions)
    assert any(item.path == eligible.path and item.reason == "unknown_publisher" for item in result.decisions)
    assert not state.exists()


def test_known_groups_publishers_and_bracket_extras_are_case_insensitive_without_rewrites(tmp_path: Path) -> None:
    state = _root(tmp_path)
    (state / "release_groups.txt").write_bytes(b"Team One\n")
    (state / "publishers.txt").write_bytes(b"Acme Books\n")
    parsing = ParsingPolicy(release_groups=("Team One",), publishers=("Acme Books",))
    first = _book(tmp_path / "library", "Series v01 (ACME BOOKS)-team one.cbz", parsing=parsing)
    second = _book(tmp_path / "library", "Series v02 (team one) (acme books).cbz")
    before = _manifest(tmp_path)
    result = _run(state, (first, second), apply=True)
    assert (result.exit_code, result.planned, result.committed, result.skipped) == (0, 0, 0, 4)
    assert [item.reason for item in result.decisions if item.status == "skipped"] == [
        "known_release_group", "known_release_group", "known_publisher", "known_publisher",
    ]
    assert _manifest(tmp_path) == before


def test_previously_skipped_extras_propagate_in_sorted_order_but_not_over_explicit_groups(tmp_path: Path) -> None:
    state = _root(tmp_path)
    old = "Old v01 (English) (Digital).cbz"
    for name in ("skipped_release_group_files.txt", "skipped_publisher_files.txt"):
        (state / name).write_text(old + "\n", encoding="utf-8")
    release = _book(tmp_path / "library", "Fresh v02 (Digital) (English).cbz")
    result = _run(state, (release,), apply=True)
    assert (result.exit_code, result.planned, result.committed) == (0, 1, 1)
    for name in ("skipped_release_group_files.txt", "skipped_publisher_files.txt"):
        assert (state / name).read_text(encoding="utf-8") == old + "\n\n" + release.name
    before = _manifest(state)
    repeated = _run(state, (release,), apply=True)
    assert (repeated.exit_code, repeated.planned, repeated.committed, repeated.skipped) == (0, 0, 0, 2)
    assert _manifest(state) == before
    explicit = _book(tmp_path / "library", "Other v03 (English) (Digital)-NewTeam.cbz")
    pending = _run(state, (explicit,))
    assert pending.exit_code == 3
    assert any(item.path == explicit.path and item.reason == "unknown_release_group" for item in pending.decisions)
    assert explicit.name not in load_dictionaries(state).skipped_release_group_files


def test_similarity_suggestions_do_not_authorize_dictionary_changes(tmp_path: Path) -> None:
    state = _root(tmp_path)
    (state / "release_groups.txt").write_bytes(b"GroupB\n")
    (state / "publishers.txt").write_bytes(b"Acme Books\n")
    release = _book(tmp_path / "library", "Series v01-GroupA.cbz", metadata=(("Publisher", "Acme Book"),))
    before = _manifest(tmp_path)
    looser = _run(state, (release,), apply=True, policy=DictionaryPolicy(generate_release_group_list_toggle=True,
                      release_group_similarity_score=0.8, publisher_similarity_score=0.9))
    strict = _run(state, (release,), apply=True, policy=DictionaryPolicy(generate_release_group_list_toggle=True,
                      release_group_similarity_score=1, publisher_similarity_score=1))
    assert (looser.exit_code, strict.exit_code, looser.committed, strict.committed) == (3, 3, 0, 0)
    loose_pending = [item for item in looser.decisions if item.status == "review_required"]
    strict_pending = [item for item in strict.decisions if item.status == "review_required"]
    assert [item.candidates for item in loose_pending] == [
        ("known_entry_1", "new_entry", "none", "skip"), ("known_entry_1", "new_entry", "none", "skip"),
    ]
    assert [item.candidates for item in strict_pending] == [("new_entry", "none", "skip"), ("new_entry", "none", "skip")]
    assert _manifest(tmp_path) == before


def test_dictionary_inputs_are_literal_and_diagnostics_do_not_echo_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Mystery).cbz")
    state = tmp_path / "state"
    forbidden = tmp_path / "forbidden"
    choice = f"__import__('os').system('touch {forbidden}')"
    with _terminal(monkeypatch, "1\n" + choice + "\n") as output:
        result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.committed) == (0, 1)
    assert load_dictionaries(state).release_groups == (choice,)
    assert not forbidden.exists()
    assert choice not in json.dumps(result.as_dict())
    assert choice.encode() not in output


def test_generation_disabled_never_requests_a_terminal_or_initializes_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Mystery).cbz")
    state = tmp_path / "state"
    monkeypatch.setattr(sys, "stdin", StringIO("1\nnone\n"))
    result = _run(state, (release,), apply=True, interactive=True, policy=DictionaryPolicy(generate_release_group_list_toggle=False))
    assert (result.exit_code, result.planned, result.committed, result.skipped) == (0, 0, 0, 1)
    assert sys.stdin.tell() == 0
    assert not state.exists()


def test_cancelled_work_does_not_initialize_state(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy")
    (source / "release_groups.txt").write_bytes(b"Group\n")
    state = tmp_path / "state"
    cancel = Event()
    cancel.set()
    before = _manifest(tmp_path)
    result = _run(state, apply=True, import_from=source, cancel=cancel)
    assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
    assert any(item.status == "cancelled" for item in result.decisions)
    assert _manifest(tmp_path) == before


def test_waiting_terminal_input_is_cancellable_without_an_input_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _book(tmp_path / "library", "Series v01 (Mystery).cbz")
    state = tmp_path / "state"
    cancel = Event()
    timer = Timer(0.05, cancel.set)
    with _terminal(monkeypatch):
        timer.start()
        try:
            result = _run(state, (release,), apply=True, interactive=True, cancel=cancel)
        finally:
            timer.cancel()
            timer.join()
    assert (result.exit_code, result.planned, result.committed) == (3, 0, 0)
    assert any(item.status == "cancelled" for item in result.decisions)
    assert not state.exists()


def test_changed_import_bytes_with_restored_size_and_mtime_are_refused_by_actual_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _root(tmp_path, "legacy")
    target = source / "release_groups.txt"
    target.write_bytes(b"Original\n")
    before = target.stat()
    state = tmp_path / "state"
    actual_apply = TransactionExecutor.apply

    def change_before_apply(executor: TransactionExecutor, plan: OperationPlan, *, authorization: RunAuthorization) -> RunResult:
        target.write_bytes(b"Modified\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return actual_apply(executor, plan, authorization=authorization)

    monkeypatch.setattr(TransactionExecutor, "apply", change_before_apply)
    result = _run(state, apply=True, import_from=source)
    assert (result.exit_code, result.planned, result.committed) == (3, 1, 0)
    assert any(item.status == "stale_plan" for item in result.decisions)
    assert target.read_bytes() == b"Modified\n"
    assert not (state / "release_groups.txt").exists()
    assert StateStore(state).list_operations() == ()


def test_changed_existing_dictionary_is_not_replaced_by_an_outdated_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _root(tmp_path)
    target = state / "release_groups.txt"
    target.write_bytes(b"Old\n")
    release = _book(tmp_path / "library", "Series v01 (New).cbz")
    actual_apply = TransactionExecutor.apply

    def change_before_apply(executor: TransactionExecutor, plan: OperationPlan, *, authorization: RunAuthorization) -> RunResult:
        target.write_bytes(b"External edit\n")
        return actual_apply(executor, plan, authorization=authorization)

    monkeypatch.setattr(TransactionExecutor, "apply", change_before_apply)
    with _terminal(monkeypatch, "1\nNew\n"):
        result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.planned, result.committed) == (3, 1, 0)
    assert target.read_bytes() == b"External edit\n"
    assert any(item.status == "stale_plan" for item in result.decisions)
    assert StateStore(state).list_operations() == ()


def test_replaced_dictionary_during_observation_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _root(tmp_path)
    target = state / "release_groups.txt"
    target.write_bytes(b"Original\n")
    before = target.stat()
    replacement = state / "replacement.txt"
    replacement.write_bytes(b"Modified\n")
    actual_fstat = rename_lists.os.fstat

    def replace_read_source(descriptor: int) -> os.stat_result:
        observed = actual_fstat(descriptor)
        if ((observed.st_dev, observed.st_ino) == (before.st_dev, before.st_ino)
                and os.lseek(descriptor, 0, os.SEEK_CUR) >= before.st_size
                and target.stat().st_ino == before.st_ino):
            replacement.replace(target)
        return observed

    monkeypatch.setattr(rename_lists.os, "fstat", replace_read_source)
    loaded = load_dictionaries(state)
    assert any(item.status == "stale_plan" and item.reason == "dictionary_source_changed"
               for item in loaded.diagnostics)
    assert loaded.release_groups == () and target.read_bytes() == b"Modified\n"


def test_failed_publication_retains_prior_dictionary_and_can_be_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _root(tmp_path)
    target = state / "release_groups.txt"
    target.write_bytes(b"Old\n")
    release = _book(tmp_path / "library", "Series v01 (New).cbz")
    actual_apply = TransactionExecutor.apply

    def fail_before_publish(executor: TransactionExecutor, plan: OperationPlan, *, authorization: RunAuthorization) -> RunResult:
        def checkpoint(phase: str, member_id: str) -> None:
            if phase == "before_publish" and member_id == "release_groups":
                raise OSError("private-token-must-not-be-echoed")

        executor.checkpoint = checkpoint
        return actual_apply(executor, plan, authorization=authorization)

    monkeypatch.setattr(TransactionExecutor, "apply", fail_before_publish)
    with _terminal(monkeypatch, "1\nNew\n"):
        failed = _run(state, (release,), apply=True, interactive=True)
    assert (failed.exit_code, failed.planned, failed.committed, failed.failed) == (1, 1, 0, 1)
    assert target.read_bytes() == b"Old\n"
    assert "private-token" not in json.dumps(failed.as_dict())
    operation_id = failed.plan_reports[0].operation_id
    assert StateStore(state).list_outbox(operation_id=operation_id) == ()
    preview = TransactionExecutor(state, (state,)).recover(operation_id, authorization=RunAuthorization())
    assert (preview.planned, preview.committed) == (1, 0)
    recovered = TransactionExecutor(state, (state,)).recover(operation_id, authorization=RunAuthorization(apply=True))
    assert (recovered.exit_code, recovered.committed, recovered.failed) == (0, 1, 0)
    assert target.read_bytes() == b"Old\n\nNew"
    record = StateStore(state).get_operation(operation_id)
    assert record is not None and record.phase == "complete"
    members = StateStore(state).list_members(operation_id)
    assert members[0].backup_path is not None and members[0].backup_path.read_bytes() == b"Old\n"


def test_nested_import_and_state_scopes_are_covered_by_one_physical_root(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy")
    (source / "release_groups.txt").write_bytes(b"Imported\n")
    state = source / "new-state"
    preview = _run(state, import_from=source)
    assert (preview.exit_code, preview.planned, preview.committed) == (0, 1, 0)
    assert not state.exists()
    applied = _run(state, apply=True, import_from=source)
    assert (applied.exit_code, applied.committed) == (0, 1)
    assert (state / "release_groups.txt").read_bytes() == b"Imported\n"
    assert (source / "release_groups.txt").read_bytes() == b"Imported\n"


def test_dictionary_append_limit_preserves_existing_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _root(tmp_path)
    target = state / "release_groups.txt"
    original = b"X" * 1_048_576
    target.write_bytes(original)
    release = _book(tmp_path / "library", "Series v01 (New).cbz")
    before = _manifest(state)
    with _terminal(monkeypatch, "1\nNew\n"):
        result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.planned, result.committed) == (2, 0, 0)
    assert any(item.reason == "dictionary_byte_limit" for item in result.decisions)
    assert target.read_bytes() == original
    assert _manifest(state) == before


def test_unwritable_state_parent_refuses_apply_without_dictionary_output(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("POSIX permission denial requires a non-root test user")
    source = _root(tmp_path, "legacy")
    (source / "release_groups.txt").write_bytes(b"Imported\n")
    parent = _root(tmp_path, "read-only")
    state = parent / "state"
    parent.chmod(0o500)
    try:
        result = _run(state, apply=True, import_from=source)
        assert (result.exit_code, result.committed, result.failed) == (1, 0, 1)
        assert any(item.status == "permission_denied" for item in result.decisions)
        assert not state.exists()
        assert (source / "release_groups.txt").read_bytes() == b"Imported\n"
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize(("name", "reason"), [
    ("Series v01 (Mystery)\nSuffix.cbz", "dictionary_invalid_entry"),
    ("Series v01 (Mystery)\udcff.cbz", "dictionary_invalid_utf8"),
])
def test_none_rejects_filenames_that_cannot_be_persisted_as_one_utf8_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, reason: str) -> None:
    release = _book(tmp_path / "library", name)
    state = tmp_path / "state"
    before = _manifest(tmp_path)
    with _terminal(monkeypatch, "1\nnone\n"):
        result = _run(state, (release,), apply=True, interactive=True)
    assert (result.exit_code, result.planned, result.committed) == (2, 0, 0)
    assert any(item.reason == reason for item in result.decisions)
    assert _manifest(tmp_path) == before
    assert not state.exists()


def test_identical_existing_import_does_not_initialize_a_journal(tmp_path: Path) -> None:
    source = _root(tmp_path, "legacy")
    state = _root(tmp_path)
    for name in _NAMES:
        for root in (source, state):
            (root / name).write_bytes(b"Existing\n")
    before = _manifest(tmp_path)
    result = _run(state, apply=True, import_from=source)
    assert (result.exit_code, result.planned, result.committed, result.skipped) == (0, 0, 0, 4)
    assert _manifest(tmp_path) == before
    assert not (state / "state.sqlite3").exists()


def test_committed_import_refreshes_existing_pipeline_without_treating_entries_as_regex(tmp_path: Path) -> None:
    library = tmp_path / "library"
    literal = _book(library, "Series v01 (New.Group).cbz").path
    different = _book(library, "Series v02 (NewxGroup).cbz").path
    source = _root(tmp_path, "legacy")
    (source / "release_groups.txt").write_text("New.Group\n", encoding="utf-8")
    config = validate_config({"paths": [str(library)], "state_dir": str(tmp_path / "state")})
    request = RunRequest("inspect", books=(literal, different))
    with open_pipeline(config) as pipeline:
        before = pipeline.run(request)
        assert [release.release_group for release in before.releases] == [None, None]
        imported = pipeline.run(RunRequest(
            "rename-lists", import_from=source, authorization=RunAuthorization(apply=True),
        ))
        assert imported.committed == 1
        after = pipeline.run(request)
        assert [release.release_group for release in after.releases] == ["New.Group", None]


@pytest.mark.parametrize("preloaded", [False, True])
def test_pipeline_publisher_dictionaries_keep_literals_at_startup_and_import_refresh(tmp_path: Path, preloaded: bool) -> None:
    library = tmp_path / "library"
    path = _book(library, "Series v01 (Orbitx Press) (Orbit. Press) (Orbit+ Press) (Digital).cbz").path
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("01.png", _png())
        archive.writestr("ComicInfo.xml", "<ComicInfo><Publisher>Primary House</Publisher></ComicInfo>")
    bracket = _book(library, "Series v02 ([).cbz").path
    original_library = _manifest(library)
    source = _root(tmp_path, "legacy")
    content = b"[\nOrbit. Press\nOrbit+ Press\n"
    (source / "publishers.txt").write_bytes(content)
    original_source = _manifest(source)
    state = tmp_path / "state"
    if preloaded:
        state.mkdir(mode=0o700)
        (state / "publishers.txt").write_bytes(content)
    config = validate_config({
        "paths": [str(library)], "state_dir": str(state),
        "restructure_when_renaming": True,
        "add_publisher_name_to_file_name_when_renaming": True,
        "publisher_similarity_score": 1.0,
    })
    request = RunRequest("inspect", books=(path, bracket))
    with open_pipeline(config) as pipeline:
        if not preloaded:
            before = pipeline.run(request)
            assert before.exit_code == 0
            assert [release.publisher for release in before.releases] == ["Primary House", None]
            preview = pipeline.run(RunRequest("rename-lists", import_from=source))
            assert (preview.exit_code, preview.planned, preview.committed) == (0, 1, 0)
            assert not state.exists()
            imported = pipeline.run(RunRequest(
                "rename-lists", import_from=source, authorization=RunAuthorization(apply=True),
            ))
            assert (imported.exit_code, imported.committed, imported.failed) == (0, 1, 0)
            operation = StateStore(state).get_operation(imported.plan_reports[0].operation_id)
            assert operation is not None and operation.phase == "complete"
        result = pipeline.run(request)
        assert (result.exit_code, result.committed, result.failed) == (0, 0, 0)
        assert [release.publisher for release in result.releases] == ["Primary House", "["]
        assert render_release(result.releases[0], pipeline.renaming) == (
            "Series v01 (Primary House) (Orbitx Press) (Digital).cbz"
        )
        assert render_release(result.releases[1], pipeline.renaming) == "Series v02 ([).cbz"
    assert (state / "publishers.txt").read_bytes() == content
    assert _manifest(source) == original_source
    assert _manifest(library) == original_library
    if preloaded:
        assert not (state / "state.sqlite3").exists()

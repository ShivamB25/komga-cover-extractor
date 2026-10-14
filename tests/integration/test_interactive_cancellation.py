from __future__ import annotations

import errno
import json
import os
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
import tomli_w

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real PTY cancellation uses Linux process groups")
NATIVE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert not tmp_path.resolve().is_relative_to(Path(__file__).resolve().parents[2])
    for name in ("home", "library", "downloads", "scratch", "state"):
        (tmp_path / name).mkdir()
    (tmp_path / "state" / "release_groups.txt").write_bytes(b"Existing Group\n")
    (tmp_path / "state" / "publishers.txt").write_bytes(b"Existing Publisher\n")
    return tmp_path


def _configure(workspace: Path, *, correct_headers: bool) -> None:
    values = {
        "paths": [str(workspace / "library")],
        "download_folders": [str(workspace / "downloads")],
        "state_dir": str(workspace / "state"),
        "log_dir": str(workspace / "logs"),
        "correct_file_extensions_toggle": correct_headers,
        "convert_to_cbz_toggle": False,
        "delete_unacceptable_files_toggle": False,
        "delete_chapters_from_downloads_toggle": False,
        "rename_files_in_download_folders_toggle": False,
        "create_folders_for_items_in_download_folder_toggle": False,
        "rename_dirs_in_download_folder_toggle": False,
        "check_for_duplicate_volumes_toggle": False,
        "check_for_existing_series_toggle": False,
        "extract_covers_toggle": False,
        "check_for_missing_volumes_toggle": False,
        "generate_release_group_list_toggle": False,
        "move_series_to_correct_library_toggle": False,
        "move_new_series_to_library_toggle": False,
        "copy_existing_volume_covers_toggle": False,
        "move_lone_files_to_similar_folder": False,
        "match_through_identifiers": False,
        "match_through_image_similarity": False,
        "compare_detected_cover_to_blank_images": False,
        "manual_rename": True,
        "manual_delete": True,
        "rename_zip_to_cbz": True,
        "ignored_folder_names": [],
        "library_types": [],
        "unacceptable_keywords": [],
        "log_to_file": False,
        "watchdog_toggle": False,
        "bookwalker_check": False,
        "send_scan_request_to_komga_libraries_toggle": False,
        "uncheck_non_qbit_upgrades_toggle": False,
        "discord_webhook_url": [],
        "bookwalker_webhook_urls": [],
        "new_volume_webhook": "",
    }
    (workspace / "config.toml").write_text(tomli_w.dumps(values), encoding="utf-8")


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _zip(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pages/01.png", _png())
        archive.writestr("ComicInfo.xml", b"<ComicInfo><Title>Series</Title></ComicInfo>")
    return path.read_bytes()


def _manifest(root: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISREG(info.st_mode):
            result[name] = ("file", info.st_ino, info.st_mtime_ns, stat.S_IMODE(info.st_mode), path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            result[name] = ("directory", stat.S_IMODE(info.st_mode))
        elif stat.S_ISLNK(info.st_mode):
            result[name] = ("link", str(path.readlink()))
        else:
            result[name] = ("special", stat.S_IFMT(info.st_mode))
    return result


def _run_at_prompt(
    workspace: Path, command: str, books: tuple[Path, ...], *, prompt: str, apply: bool,
    answers: bytes | None = None,
) -> dict[str, Any]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(workspace / "home"),
        "XDG_CONFIG_HOME": str(workspace / "home" / "config"),
        "XDG_STATE_HOME": str(workspace / "home" / "state"),
        "XDG_CACHE_HOME": str(workspace / "home" / "cache"),
        "TMPDIR": str(workspace / "scratch"),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONUNBUFFERED": "1",
    }
    if "LD_LIBRARY_PATH" in os.environ:
        environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    invocation = [
        sys.executable, "-I", "-m", "komga_cover_extractor", "--config", "config.toml",
        command, *(str(book) for book in books), "--interactive", "--json",
    ]
    if apply:
        invocation.append("--apply")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    master, slave = os.openpty()
    selector = selectors.DefaultSelector()
    process: subprocess.Popen[bytes] | None = None
    try:
        attributes = termios.tcgetattr(slave)
        attributes[3] &= ~termios.ECHO
        if answers is not None:
            attributes[3] &= ~termios.ICANON
            attributes[6][termios.VMIN] = 1
            attributes[6][termios.VTIME] = 0
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        process = subprocess.Popen(
            invocation, cwd=workspace, env=environment, stdin=slave, stderr=slave,
            stdout=subprocess.PIPE, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        assert process.stdout is not None
        selector.register(master, selectors.EVENT_READ, "stderr")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")

        def drain() -> None:
            for key, _ in selector.select(timeout=0.05):
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError as error:
                    if key.fd != master or error.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured[key.data].extend(chunk)
                assert len(captured[key.data]) <= 1024 * 1024, "owned CLI output exceeded the test bound"

        expected_prompt = prompt.encode("utf-8")
        deadline = time.monotonic() + 30
        while expected_prompt not in captured["stderr"]:
            drain()
            assert process.poll() is None, (
                f"CLI exited before its pending choice\n{invocation!r}\n{captured!r}"
            )
            assert time.monotonic() < deadline, f"CLI did not show the exact prompt\n{captured!r}"
        assert captured["stderr"].count(expected_prompt) == 1
        assert captured["stdout"] == b"", "CLI completed instead of waiting for the choice"
        assert process.poll() is None
        started = time.monotonic()
        if answers is None:
            process.send_signal(signal.SIGTERM)
        else:
            assert os.write(master, answers) == len(answers)
        deadline = started + (5 if answers is None else 30)
        while process.poll() is None or selector.get_map():
            drain()
            assert time.monotonic() < deadline, f"CLI did not finish the supplied terminal action\n{captured!r}"
        process.wait(timeout=1)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
        payload = json.loads(captured["stdout"])
        if answers is None:
            assert process.returncode == 143, captured
            assert time.monotonic() - started < 5
            assert payload["exit_code"] == 143
            assert payload["ok"] is False
            assert payload["counts"] == {"planned": 0, "committed": 0, "skipped": 0, "failed": 0}
            assert payload["plans"] == []
            assert payload["deferred_deliveries"] == []
        else:
            assert process.returncode == 0, captured
            assert payload["exit_code"] == 0
        return payload
    finally:
        selector.close()
        try:
            if process is not None:
                try:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)


@pytest.mark.parametrize("apply", [False, True], ids=["preview", "apply"])
def test_sigterm_cancels_pending_header_rename_without_terminal_input(workspace: Path, apply: bool) -> None:
    pytest.importorskip("filetype", reason="real header observations require the conversion extra")
    _configure(workspace, correct_headers=True)
    source = workspace / "downloads" / "Series v01.cbr"
    original = _zip(source)
    destination = source.with_suffix(".cbz")
    sidecar = source.with_suffix(".jpg")
    sidecar.write_bytes(b"existing same-stem artwork retained during cancellation\n")
    before = _manifest(workspace)

    payload = _run_at_prompt(
        workspace, "run", (), apply=apply,
        prompt=f"Rename {str(source)!r} to {str(destination)!r}? Type rename or keep: ",
    )

    assert any(
        item["status"] == "cancelled" and item["reason"] == "shutdown_requested"
        and item["path"] == str(source) for item in payload["decisions"]
    )
    assert source.read_bytes() == original
    assert sidecar.read_bytes() == b"existing same-stem artwork retained during cancellation\n"
    assert not destination.exists()
    assert _manifest(workspace) == before


@pytest.mark.parametrize("apply", [False, True], ids=["preview", "apply"])
def test_sigterm_cancels_pending_native_conversion_replacement_without_terminal_input(
    workspace: Path, apply: bool,
) -> None:
    pytest.importorskip("filetype", reason="real container observations require the conversion extra")
    pytest.importorskip("rarfile", reason="real native RAR conversion requires the conversion extra")
    if shutil.which("unrar") is None:
        pytest.skip("actual RAR collision prompt requires the configured native unrar backend")
    _configure(workspace, correct_headers=False)
    source = workspace / "downloads" / "Series v01.rar"
    shutil.copyfile(NATIVE_FIXTURES / "rar5-subdirs.rar", source)
    original = source.read_bytes()
    destination = source.with_suffix(".cbz")
    existing = _zip(destination)
    sidecar = destination.with_suffix(".jpg")
    sidecar.write_bytes(b"existing destination artwork retained during cancellation\n")
    before = _manifest(workspace)

    payload = _run_at_prompt(
        workspace, "convert", (source,), apply=apply,
        prompt=f"Replace {str(destination)!r} and retain its previous bytes? Type replace to authorize, or keep: ",
    )

    assert {
        "status": "cancelled", "reason": "shutdown_requested", "path": None, "candidates": [],
    } in payload["decisions"]
    assert source.read_bytes() == original
    assert destination.read_bytes() == existing
    assert sidecar.read_bytes() == b"existing destination artwork retained during cancellation\n"
    assert _manifest(workspace) == before


def test_batched_header_answers_authorize_only_the_selected_book(workspace: Path) -> None:
    pytest.importorskip("filetype", reason="real header observations require the conversion extra")
    _configure(workspace, correct_headers=True)
    renamed = workspace / "downloads" / "Series v01.cbr"
    kept = workspace / "downloads" / "Series v02.cbr"
    renamed_bytes, kept_bytes = _zip(renamed), _zip(kept)
    destination = renamed.with_suffix(".cbz")
    payload = _run_at_prompt(
        workspace, "run", (), apply=True, answers=b"rename\nkeep\n",
        prompt=f"Rename {str(renamed)!r} to {str(destination)!r}? Type rename or keep: ",
    )
    assert destination.read_bytes() == renamed_bytes
    assert not renamed.exists()
    assert kept.read_bytes() == kept_bytes
    assert not kept.with_suffix(".cbz").exists()
    assert payload["counts"]["committed"] == 1

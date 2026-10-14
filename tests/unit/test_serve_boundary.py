from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path

import pytest

from komga_cover_extractor.application import serve as supervision
from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.config import load_config
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.libraries import LibraryRoot


def _config(tmp_path: Path, **overrides):
    return load_config(environ={}, cwd=tmp_path, overrides={
        "paths": [tmp_path / "library"], "download_folders": [tmp_path / "downloads"],
        "state_dir": tmp_path / "state", "log_dir": tmp_path / "logs", **overrides,
    })


def test_resolved_environment_roundtrips_policy_paths_records_and_secret_strings(tmp_path: Path) -> None:
    config = _config(
        tmp_path, image_quality=77, output_covers_as_webp=True,
        ranked_keywords=[{"name": r"\(Digital\)", "score": 3.25, "file_type": "volume"}],
        volume_keywords=[r"V(?:ol)?", 'Book "quoted"', "第🦊"],
        library_types=[{"name": "custom", "extensions": [".cbz"], "must_contain": [r"\bDigital\b"],
                        "must_not_contain": [], "match_percentage": 83}],
        komga_path_mappings=[{"local_root": tmp_path / "library", "remote_root": "/server/books"}],
        qbittorrent_username="fictional-user", qbittorrent_password='fictional "secret"\n雪🦊\\tail',
        discord_webhook_url=["https://discord.invalid/api/webhooks/fictional-token"],
        ignored_folder_names=["99", "true", "[literal]"],
    )
    roots = (
        LibraryRoot(tmp_path / "library", ("volume",), (".cbz",), ("custom",)),
        LibraryRoot(tmp_path / "downloads", roles=("download",)),
    )
    environment = supervision.child_environment(config, roots=roots, environ={
        "HOME": str(tmp_path), "KEEP": "preserved", "KCE_IMAGE_QUALITY": "12",
        "KCE_RESTURCTURE_WHEN_RENAMING": "true", "KCE_APPLY": "true",
        supervision.READY_ENV: "123", supervision.ROOTS_ENV: "stale-input",
    })
    resolved = load_config(environ=environment, cwd=tmp_path / "different-cwd")

    assert resolved.image_quality == 77
    assert resolved.output_covers_as_webp is True
    assert resolved.restructure_when_renaming is False
    assert resolved.paths == (tmp_path / "library",)
    assert resolved.state_dir == tmp_path / "state"
    assert resolved.log_dir == tmp_path / "logs"
    assert resolved.volume_keywords == (r"V(?:ol)?", 'Book "quoted"', "第🦊")
    assert resolved.ranked_keywords[0].as_dict() == {"name": r"\(Digital\)", "score": 3.25, "file_type": "volume"}
    assert resolved.library_types[0].as_dict() == {
        "name": "custom", "extensions": [".cbz"], "must_contain": [r"\bDigital\b"],
        "must_not_contain": [], "match_percentage": 83,
    }
    assert resolved.komga_path_mappings[0].as_dict() == {
        "local_root": str(tmp_path / "library"), "remote_root": "/server/books",
    }
    assert resolved.qbittorrent_password == 'fictional "secret"\n雪🦊\\tail'
    assert resolved.discord_webhook_url == ("https://discord.invalid/api/webhooks/fictional-token",)
    assert resolved.ignored_folder_names == ("99", "true", "[literal]")
    assert supervision.child_roots(environment) == roots
    assert environment["KEEP"] == "preserved" and environment["HOME"] == str(tmp_path)
    assert "KCE_APPLY" not in environment
    assert "KCE_RESTURCTURE_WHEN_RENAMING" not in environment
    assert supervision.READY_ENV not in environment
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists()


def test_installed_commands_contain_only_explicit_authorization_not_secrets(tmp_path: Path) -> None:
    request = RunRequest("serve", authorization=RunAuthorization(True, True, True), interactive=True,
                         config_overrides=(("qbittorrent_password", "fictional-password"),))
    selected = tmp_path / "config.toml"
    assert supervision.child_command("run", request, config_path=selected) == (
        sys.executable, "-I", "-m", "komga_cover_extractor", "--config", str(selected),
        "run", "--apply", "--allow-remote", "--allow-torrent-removal", "--interactive",
    )
    assert supervision.child_command("qbit", request, config_path=selected) == (
        sys.executable, "-I", "-m", "komga_cover_extractor", "--config", str(selected),
        "qbit", "--apply", "--allow-remote", "--allow-torrent-removal",
    )
    assert supervision.child_command("watch", RunRequest("serve")) == (
        sys.executable, "-I", "-m", "komga_cover_extractor", "watch",
    )
    with pytest.raises(ValueError, match="invalid_supervised_command"):
        supervision.child_command("python -c unexpected", request)


def test_parent_paths_and_roles_are_not_authorization() -> None:
    roots = supervision.child_roots({supervision.ROOTS_ENV: json.dumps({
        "schema_version": 1,
        "roots": [{"path": "/books", "formats": ["volume"], "extensions": [".cbz"],
                   "library_types": ["manga"], "roles": ["library", "download"]}],
    })})
    assert roots == (LibraryRoot(Path("/books"), ("volume",), (".cbz",), ("manga",), ("library", "download")),)
    request = RunRequest("run", roots=roots)
    assert request.authorization == RunAuthorization()
    assert supervision.child_command("run", request) == (
        sys.executable, "-I", "-m", "komga_cover_extractor", "run",
    )


@pytest.mark.parametrize("payload", [
    {"schema_version": 2, "roots": []},
    {"schema_version": True, "roots": []},
    {"schema_version": 1, "roots": [], "apply": True},
    {"schema_version": 1, "roots": [{"path": "/books"}]},
    {"schema_version": 1, "roots": [{"path": "relative", "formats": [], "extensions": [],
                                    "library_types": [], "roles": ["library"]}]},
    {"schema_version": 1, "roots": [{"path": "/books", "formats": ["chapter", "chapter"], "extensions": [],
                                    "library_types": [], "roles": ["library"]}]},
    {"schema_version": 1, "roots": [{"path": "/books", "formats": ["movie"], "extensions": [],
                                    "library_types": [], "roles": ["library"]}]},
    {"schema_version": 1, "roots": [{"path": "/books", "formats": [], "extensions": [],
                                    "library_types": [], "roles": ["apply"]}]},
    {"schema_version": 1, "roots": [{"path": "/books\x00other", "formats": [], "extensions": [],
                                    "library_types": [], "roles": ["library"]}]},
])
def test_child_root_transport_rejects_unknown_versions_fields_roles_and_malformed_roots(payload: object) -> None:
    with pytest.raises(ValueError, match="invalid_child_roots"):
        supervision.child_roots({supervision.ROOTS_ENV: json.dumps(payload)})


def test_child_root_transport_rejects_oversized_and_nested_input() -> None:
    with pytest.raises(ValueError, match="child_root_input_limit"):
        supervision.child_roots({supervision.ROOTS_ENV: "x" * (64 * 1024 + 1)})
    with pytest.raises(ValueError, match="invalid_child_roots"):
        supervision.child_roots({supervision.ROOTS_ENV: json.dumps({
            "schema_version": 1, "roots": [
                {"path": path, "formats": [], "extensions": [], "library_types": [], "roles": ["library"]}
                for path in ("/books", "/books/series")
            ],
        })})
    assert supervision.child_roots({}) == ()


def test_ready_notification_owns_one_inherited_pipe_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    reader, writer = os.pipe()
    os.set_inheritable(writer, True)
    monkeypatch.setenv(supervision.READY_ENV, str(writer))
    try:
        supervision.notify_ready()
        supervision.notify_ready()
        assert os.read(reader, 64) == b"ready\n"
        assert os.read(reader, 64) == b""
        assert supervision.READY_ENV not in os.environ
        with pytest.raises(OSError) as closed:
            os.fstat(writer)
        assert closed.value.errno == errno.EBADF
    finally:
        os.close(reader)
        try:
            os.close(writer)
        except OSError:
            pass


def test_readiness_rejects_regular_files_without_touching_or_closing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "unowned-file"
    output.write_bytes(b"keep-unowned-content")
    descriptor = os.open(output, os.O_WRONLY)
    os.set_inheritable(descriptor, True)
    monkeypatch.setenv(supervision.READY_ENV, str(descriptor))
    try:
        with pytest.raises(ValueError, match="readiness_descriptor_must_be_owned_pipe"):
            supervision.notify_ready()
        assert output.read_bytes() == b"keep-unowned-content"
        assert os.fstat(descriptor).st_size == 20
        assert supervision.READY_ENV not in os.environ
    finally:
        os.close(descriptor)


def test_apply_refuses_unwritable_state_before_child_start_or_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(os, "access", lambda *args, **kwargs: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("child must not start")

    monkeypatch.setattr(supervision.subprocess, "Popen", forbidden)
    status = supervision.serve(RunRequest("serve", authorization=RunAuthorization(apply=True)), config=config)
    assert status == 2
    assert capsys.readouterr() == ("", "kce serve: apply requires writable nonsymlink state storage\n")
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists()


def test_apply_refuses_symlinked_state_ancestor_without_mutating_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(outside, target_is_directory=True)
    config = _config(tmp_path, state_dir=link / "owned-state")
    status = supervision.serve(RunRequest("serve", authorization=RunAuthorization(apply=True)), config=config)
    assert status == 2
    assert capsys.readouterr() == ("", "kce serve: apply requires writable nonsymlink state storage\n")
    assert tuple(outside.iterdir()) == ()


def test_watch_without_download_roots_fails_before_launch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = supervision.serve(RunRequest("serve"), config=_config(tmp_path, watchdog_toggle=True, download_folders=[]))
    assert status == 2
    assert capsys.readouterr() == ("", "kce serve: watch requires a download root\n")


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan")])
def test_shutdown_deadline_is_positive_finite_and_at_most_thirty_seconds(
    tmp_path: Path, timeout: float, capsys: pytest.CaptureFixture[str],
) -> None:
    status = supervision.serve(RunRequest("serve"), config=_config(tmp_path), shutdown_timeout_seconds=timeout)
    assert status == 2
    assert capsys.readouterr() == ("", "kce serve: invalid lifecycle deadlines\n")


def test_duplicate_and_excessively_nested_root_payloads_are_invalid_inputs() -> None:
    for raw in (
        '{"schema_version":2,"schema_version":1,"roots":[]}',
        '[' * 5000 + ']' * 5000,
    ):
        with pytest.raises(ValueError, match="invalid_child_roots"):
            supervision.child_roots({supervision.ROOTS_ENV: raw})


@pytest.mark.skipif(sys.platform != "linux", reason="Owned inherited Linux pipe contract")
def test_readiness_does_not_write_to_an_unowned_named_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "unowned-fifo"
    os.mkfifo(path)
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    os.set_inheritable(descriptor, True)
    monkeypatch.setenv(supervision.READY_ENV, str(descriptor))
    try:
        with pytest.raises(ValueError, match="readiness_descriptor_must_be_owned_pipe"):
            supervision.notify_ready()
        with pytest.raises(BlockingIOError):
            os.read(descriptor, 64)
        assert os.fstat(descriptor).st_ino == path.stat().st_ino
        assert supervision.READY_ENV not in os.environ
    finally:
        os.close(descriptor)


def test_missing_proc_ownership_backend_fails_before_starting_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    original = Path.iterdir

    def missing(path: Path):
        if path == Path(f"/proc/{os.getpid()}/task"):
            raise FileNotFoundError
        return original(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("child must not start without ownership evidence")

    monkeypatch.setattr(Path, "iterdir", missing)
    monkeypatch.setattr(supervision.subprocess, "Popen", forbidden)
    status = supervision.serve(RunRequest("serve"), config=_config(tmp_path))
    assert status == 2
    assert capsys.readouterr() == ("", "kce serve: process or configuration prerequisites unavailable\n")
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists()

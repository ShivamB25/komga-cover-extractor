from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from komga_cover_extractor.compat.legacy_cli import (
    LegacyArgumentError,
    parse_docker_environment,
    parse_legacy_args,
)
from komga_cover_extractor.config import load_config
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.libraries import LibraryRoot


def test_repeated_roots_and_packed_restrictions_preserve_roles_and_order() -> None:
    request = parse_legacy_args([
        "-p", r"/books/manga\0volume\0.zip,.cbz\0manga\1/books/novels\0.epub\0light novel",
        "--paths", "/books/chapters", "chapter", ".cbz", "digital_comps",
        "-df", r"/downloads/volumes\0volume\1/downloads/chapters\0chapter",
        "--download_folders", "/downloads/other", ".epub",
    ])

    assert request.command == "run"
    assert request.roots == (
        LibraryRoot(Path("/books/manga"), ("volume",), (".zip", ".cbz"), ("manga",)),
        LibraryRoot(Path("/books/novels"), extensions=(".epub",), library_types=("light novel",)),
        LibraryRoot(Path("/books/chapters"), ("chapter",), (".cbz",), ("digital_comps",)),
        LibraryRoot(Path("/downloads/volumes"), ("volume",), roles=("download",)),
        LibraryRoot(Path("/downloads/chapters"), ("chapter",), roles=("download",)),
        LibraryRoot(Path("/downloads/other"), extensions=(".epub",), roles=("download",)),
    )
    assert dict(request.config_overrides) == {
        "paths": (Path("/books/manga"), Path("/books/novels"), Path("/books/chapters")),
        "download_folders": (Path("/downloads/volumes"), Path("/downloads/chapters"), Path("/downloads/other")),
    }
    assert request.authorization == RunAuthorization()


def test_same_root_merges_roles_without_expanding_restrictions() -> None:
    request = parse_legacy_args([
        "-p", "/books", "volume", ".cbz", "-p", "/books/../books", "volume", ".cbz",
        "-df", r"/books\0volume\0.cbz",
    ])

    assert request.roots == (
        LibraryRoot(Path("/books"), ("volume",), (".cbz",), roles=("library", "download")),
    )
    assert dict(request.config_overrides) == {
        "paths": (Path("/books"),), "download_folders": (Path("/books"),),
    }


def test_absent_cli_preserves_configured_values_and_resolves_roots_at_invocation_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    request = parse_legacy_args(["--paths", "library", "-df", "downloads"])
    config = load_config(
        environ={"KCE_IMAGE_QUALITY": "77", "KCE_LOG_TO_FILE": "true"},
        overrides={**dict(request.config_overrides), "state_dir": tmp_path / "state",
                   "log_dir": tmp_path / "logs"}, cwd=tmp_path,
    )

    assert request.command == "run"
    assert config.image_quality == 77
    assert config.log_to_file is True
    assert config.paths == (tmp_path / "library",)
    assert config.download_folders == (tmp_path / "downloads",)
    assert request.roots[0].path == tmp_path / "library"
    assert not (tmp_path / "library").exists()
    assert not config.state_dir.exists()
    assert not config.log_dir.exists()


@pytest.mark.parametrize(("flags", "key", "value", "expected"), [
    (("-bwc", "--bookwalker_check", "--bookwalker-check"), "bookwalker_check", " TrUe ", True),
    (("-c", "--compress"), "compress_image_option", "FALSE", False),
    (("-wd", "--watchdog"), "watchdog_toggle", "true", True),
    (("-ltf", "--log_to_file", "--log-to-file"), "log_to_file", " false ", False),
    (("--output_covers_as_webp", "--output-covers-as-webp"), "output_covers_as_webp", "TRUE", True),
    (("-cq", "--compress_quality", "--compress-quality"), "image_quality", "73", 73),
    (("--watchdog_discover_new_files_check_interval", "--watchdog-discover-new-files-check-interval"),
     "watchdog_discover_new_files_check_interval", "17", 17),
    (("--watchdog_file_transferred_check_interval", "--watchdog-file-transferred-check-interval"),
     "watchdog_file_transferred_check_interval", "3", 3),
    (("-nw", "--new_volume_webhook", "--new-volume-webhook"), "new_volume_webhook",
     "https://discord.invalid/api/webhooks/new/private", "https://discord.invalid/api/webhooks/new/private"),
])
def test_each_historical_scalar_alias_translates_only_its_explicit_value(
    flags: tuple[str, ...], key: str, value: str, expected: object,
) -> None:
    for flag in flags:
        request = parse_legacy_args(["-df", "/downloads", flag, value])
        assert dict(request.config_overrides) == {"download_folders": (Path("/downloads"),), key: expected}
        assert request.command == ("watch" if key == "watchdog_toggle" and expected else "run")
        assert request.authorization == RunAuthorization()


def test_repeated_scalar_uses_last_value_but_repeatable_hooks_append_unique_values() -> None:
    request = parse_legacy_args([
        "-c", "true", "--compress", "false", "-cq", "31", "--compress_quality", "64",
        "-wh", r"https://discord.invalid/a\1https://discord.invalid/b", "https://discord.invalid/a",
        "--webhook", "https://discord.invalid/c", "",
        "-bwk_whs", "https://discord.invalid/releases",
        "--bookwalker_webhook_urls", r"https://discord.invalid/releases\1https://discord.invalid/preorders",
    ])

    assert dict(request.config_overrides) == {
        "compress_image_option": False,
        "image_quality": 64,
        "discord_webhook_url": (
            "https://discord.invalid/a", "https://discord.invalid/b", "https://discord.invalid/c",
        ),
        "bookwalker_webhook_urls": (
            "https://discord.invalid/releases", "https://discord.invalid/preorders",
        ),
    }


def test_addon_remains_independent_and_authorization_is_invocation_only() -> None:
    request = parse_legacy_args([
        "--paths", r"/books\0volume\0.cbz", "-df", "/downloads",
        "--once", "--allow-remote", "--allow-torrent-removal", "--apply",
    ], addon=True)

    assert request.command == "qbit"
    assert request.once is True
    assert request.authorization == RunAuthorization(True, True, True)
    assert request.roots == (
        LibraryRoot(Path("/books"), ("volume",), (".cbz",)),
        LibraryRoot(Path("/downloads"), roles=("download",)),
    )
    assert dict(request.config_overrides) == {
        "paths": (Path("/books"),), "download_folders": (Path("/downloads"),),
    }
    with pytest.raises(LegacyArgumentError, match="invalid_legacy_arguments"):
        parse_legacy_args([], addon=True)
    with pytest.raises(LegacyArgumentError, match="invalid_legacy_arguments"):
        parse_legacy_args(["-p", "/books", "-c", "true"], addon=True)


@pytest.mark.parametrize("restriction", ["official", "fan", "raw", "english", "japanese", "chinese", "korean"])
def test_dormant_language_and_translation_restrictions_are_explicitly_unsupported(restriction: str) -> None:
    with pytest.raises(LegacyArgumentError) as failure:
        parse_legacy_args(["-p", "/books", restriction])
    assert failure.value.reason == "unsupported_policy"
    assert failure.value.status == "unsupported"


@pytest.mark.parametrize("arguments", [
    ["-p"], ["-p", ""], ["-p", "   "], ["-p", "/books\x00volume"],
    ["-p", r"/books\0"], ["-p", r"/books\1"],
    ["-p", "/books", "volume,.cbz"], ["-p", "/books", ".rar"],
    ["-p", "/books", "manga,unknown"], ["-p", "/books", "volume", "chapter"],
    ["-p", "/books", "/other"], ["-p", r"/books\1/other", "volume"],
    ["-p", r"/books\0volume", ".cbz"], ["-p", "/books", r"volume\1chapter"],
    ["-p", "/books", "volume", "-df", "/books", "chapter"],
    ["-p", "/books", "-p", "/books/nested"],
])
def test_malformed_or_conflicting_restrictions_never_become_unrestricted_roots(arguments: list[str]) -> None:
    with pytest.raises(LegacyArgumentError):
        parse_legacy_args(arguments)


@pytest.mark.parametrize(("flag", "values", "reason"), [
    ("--compress_quality", ("0", "101", "-1", "+2", "3.5", "", "not-a-number"), "invalid_image_quality"),
    ("--watchdog_discover_new_files_check_interval", ("0", "86401", "-1", "0.5", "", "nan"), "invalid_watch_interval"),
    ("--watchdog_file_transferred_check_interval", ("0", "86401", "-3", "", "1e3"), "invalid_watch_interval"),
    ("--compress", ("yes", "1", "off", "", "not-true"), "invalid_boolean"),
])
def test_invalid_quality_intervals_and_booleans_fail_instead_of_ignoring_input(
    flag: str, values: tuple[str, ...], reason: str,
) -> None:
    for value in values:
        with pytest.raises(LegacyArgumentError, match=reason):
            parse_legacy_args(["-p", "/books", f"{flag}={value}"])


def test_legacy_errors_do_not_echo_argv_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "https://discord.invalid/api/webhooks/private-token"
    with pytest.raises(LegacyArgumentError) as failure:
        parse_legacy_args(["--unknown", secret])
    assert str(failure.value) == "invalid_legacy_arguments"
    assert capsys.readouterr() == ("", "")


def test_help_is_argparse_help_without_operational_startup(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_legacy_args(["--help"])
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert "--compress_quality" in captured.out
    assert captured.err == ""


def test_docker_inputs_preserve_packed_roots_and_all_actual_cmd_values() -> None:
    request = parse_docker_environment({
        "PATHS": r"/books/manga\0.cbz\0manga\1/books/novels\0.epub",
        "DOWNLOAD_FOLDERS": r"/downloads\0chapter",
        "WEBHOOK": r"https://discord.invalid/a\1https://discord.invalid/b",
        "BOOKWALKER_CHECK": "True", "COMPRESS": "false", "COMPRESS_QUALITY": "53",
        "BOOKWALKER_WEBHOOK_URLS": r"https://discord.invalid/releases\1https://discord.invalid/preorders",
        "WATCHDOG": "true", "WATCHDOG_DISCOVER_NEW_FILES_CHECK_INTERVAL": "11",
        "WATCHDOG_FILE_TRANSFERRED_CHECK_INTERVAL": "2", "OUTPUT_COVERS_AS_WEBP": "true",
        "NEW_VOLUME_WEBHOOK": "https://discord.invalid/new",
        "APPLY": "true", "ALLOW_REMOTE": "true", "ALLOW_TORRENT_REMOVAL": "true",
        "KCE_APPLY": "true", "LOG_TO_FILE": "true", "UMASK": "027",
    })

    assert request.command == "watch"
    assert request.authorization == RunAuthorization()
    assert request.roots == (
        LibraryRoot(Path("/books/manga"), extensions=(".cbz",), library_types=("manga",)),
        LibraryRoot(Path("/books/novels"), extensions=(".epub",)),
        LibraryRoot(Path("/downloads"), ("chapter",), roles=("download",)),
    )
    assert dict(request.config_overrides) == {
        "paths": (Path("/books/manga"), Path("/books/novels")),
        "download_folders": (Path("/downloads"),),
        "bookwalker_check": True, "compress_image_option": False, "watchdog_toggle": True,
        "output_covers_as_webp": True,
        "watchdog_discover_new_files_check_interval": 11,
        "watchdog_file_transferred_check_interval": 2,
        "image_quality": 53, "new_volume_webhook": "https://discord.invalid/new",
        "discord_webhook_url": ("https://discord.invalid/a", "https://discord.invalid/b"),
        "bookwalker_webhook_urls": ("https://discord.invalid/releases", "https://discord.invalid/preorders"),
    }


def test_absent_and_empty_docker_inputs_do_not_override_configuration() -> None:
    request = parse_docker_environment({"PATHS": "", "DOWNLOAD_FOLDERS": "", "COMPRESS_QUALITY": ""})
    assert request.command == "run"
    assert request.roots == ()
    assert dict(request.config_overrides) == {}
    assert request.authorization == RunAuthorization()
    configured = load_config(environ={"KCE_IMAGE_QUALITY": "81", "KCE_PATHS": '["/configured"]'},
                             overrides=dict(request.config_overrides))
    assert configured.image_quality == 81
    assert configured.paths == (Path("/configured"),)


def test_adapter_never_imports_executable_main_or_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    importer = builtins.__import__

    def guarded(name: str, *args, **kwargs):
        if name in {"settings", "qbit_torrent_unchecker"}:
            raise AssertionError("executable source import")
        return importer(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    request = parse_legacy_args(["-p", "/books", "-c", "true"])
    assert request.command == "run"
    assert request.roots == (LibraryRoot(Path("/books")),)
    assert dict(request.config_overrides) == {"paths": (Path("/books"),), "compress_image_option": True}


@pytest.mark.parametrize("addon", [False, True])
def test_explicit_config_is_file_selection_not_a_configuration_value(addon: bool) -> None:
    request = parse_legacy_args(
        ["--config", "first.toml", "-p", "/books", "--config", "selected.toml"], addon=addon,
    )
    assert request.config_path == Path("selected.toml")
    assert dict(request.config_overrides) == {"paths": (Path("/books"),)}
    assert request.authorization == RunAuthorization()
    with pytest.raises(LegacyArgumentError, match="invalid_config_path"):
        parse_legacy_args(["-p", "/books", "--config", ""], addon=addon)


def test_original_unambiguous_argparse_prefixes_still_work_without_authorization_inference() -> None:
    request = parse_legacy_args([
        "--pa=/books", "--download_f", "/downloads", "--compress_qu=61",
        "--watchdog_d=7", "--watchdog_f=2", "--output_c=true", "--bookwalker_c=false",
    ])
    assert request.command == "run"
    assert request.authorization == RunAuthorization()
    assert dict(request.config_overrides) == {
        "paths": (Path("/books"),), "download_folders": (Path("/downloads"),),
        "bookwalker_check": False, "output_covers_as_webp": True,
        "watchdog_discover_new_files_check_interval": 7,
        "watchdog_file_transferred_check_interval": 2, "image_quality": 61,
    }
    for ambiguous_or_new_prefix in ("--comp", "--watch", "--allow-r", "--appl"):
        with pytest.raises(LegacyArgumentError, match="invalid_legacy_arguments"):
            parse_legacy_args(["-p", "/books", ambiguous_or_new_prefix])

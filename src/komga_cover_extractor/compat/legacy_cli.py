"""Translate the historical command line without importing executable sources."""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Never

if TYPE_CHECKING:
    from ..application.pipeline import RunRequest
    from ..domain.libraries import LibraryRoot


LEGACY_FORMATS = ("chapter", "volume")
LEGACY_EXTENSIONS = (".epub", ".zip", ".cbz")
LEGACY_LIBRARY_TYPES = ("manga", "light novel", "digital_comps")
_UNSUPPORTED_RESTRICTIONS = frozenset((
    "official", "fan", "raw", "english", "japanese", "chinese", "korean",
))
_BOOLEAN_FLAGS = (
    (("-bwc", "--bookwalker_check", "--bookwalker-check"), "bookwalker_check"),
    (("-c", "--compress"), "compress_image_option"),
    (("-wd", "--watchdog"), "watchdog_toggle"),
    (("-ltf", "--log_to_file", "--log-to-file"), "log_to_file"),
    (("--output_covers_as_webp", "--output-covers-as-webp"), "output_covers_as_webp"),
)
_INTERVAL_FLAGS = (
    ("--watchdog_discover_new_files_check_interval", "watchdog_discover_new_files_check_interval"),
    ("--watchdog_file_transferred_check_interval", "watchdog_file_transferred_check_interval"),
)
_DOCKER_FLAGS = (
    ("PATHS", "--paths"),
    ("DOWNLOAD_FOLDERS", "--download_folders"),
    ("WEBHOOK", "--webhook"),
    ("BOOKWALKER_CHECK", "--bookwalker_check"),
    ("COMPRESS", "--compress"),
    ("COMPRESS_QUALITY", "--compress_quality"),
    ("BOOKWALKER_WEBHOOK_URLS", "--bookwalker_webhook_urls"),
    ("WATCHDOG", "--watchdog"),
    ("WATCHDOG_DISCOVER_NEW_FILES_CHECK_INTERVAL", "--watchdog_discover_new_files_check_interval"),
    ("WATCHDOG_FILE_TRANSFERRED_CHECK_INTERVAL", "--watchdog_file_transferred_check_interval"),
    ("OUTPUT_COVERS_AS_WEBP", "--output_covers_as_webp"),
    ("NEW_VOLUME_WEBHOOK", "--new_volume_webhook"),
)


class LegacyArgumentError(ValueError):
    def __init__(self, reason: str, *, status: str = "invalid_input") -> None:
        self.reason = reason
        self.status = status
        super().__init__(reason)


class _LegacyParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse's default diagnostic includes unrecognized credential values.
        for suffix, reason in (
            ("quality must be between 1 and 100", "invalid_image_quality"),
            ("interval must be between 1 and 86400 seconds", "invalid_watch_interval"),
            ("expected true or false", "invalid_boolean"),
            ("config path must not be empty or contain NUL", "invalid_config_path"),
        ):
            if message.endswith(suffix):
                raise LegacyArgumentError(reason)
        raise LegacyArgumentError("invalid_legacy_arguments")


def _boolean(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return lowered == "true"


def _quality(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100:
        raise argparse.ArgumentTypeError("quality must be between 1 and 100")
    return int(value)


def _interval(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 86400:
        raise argparse.ArgumentTypeError("interval must be between 1 and 86400 seconds")
    return int(value)


def _config_path(value: str) -> Path:
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("config path must not be empty or contain NUL")
    return Path(value)


def _root_groups(groups: Sequence[Sequence[str]], *, role: str) -> tuple[LibraryRoot, ...]:
    from ..domain.libraries import LibraryRoot

    roots: list[LibraryRoot] = []
    for group in groups:
        if not group or any(not token or "\x00" in token for token in group):
            raise LegacyArgumentError("empty_or_invalid_root")
        entries: tuple[tuple[str, ...], ...]
        if r"\1" in group[0]:
            if len(group) != 1:
                raise LegacyArgumentError("packed_roots_with_separate_restrictions")
            entries = tuple((entry,) for entry in group[0].split(r"\1"))
        else:
            entries = (tuple(group),)
        for entry in entries:
            if any(r"\1" in token for token in entry[1:]):
                raise LegacyArgumentError("misplaced_root_delimiter")
            if r"\0" in entry[0]:
                if len(entry) != 1:
                    raise LegacyArgumentError("mixed_restriction_delimiters")
                tokens = tuple(entry[0].split(r"\0"))
            else:
                tokens = entry
            if any(not token.strip() or "\x00" in token for token in tokens):
                raise LegacyArgumentError("empty_or_invalid_root")
            restrictions: dict[str, tuple[str, ...]] = {}
            for token in tokens[1:]:
                values = tuple(part.strip() for part in token.split(","))
                if any(value in _UNSUPPORTED_RESTRICTIONS for value in values):
                    raise LegacyArgumentError("unsupported_policy", status="unsupported")
                if not all(values):
                    raise LegacyArgumentError("empty_root_restriction")
                allowed: tuple[str, ...]
                if values[0] in LEGACY_FORMATS:
                    field, allowed = "formats", LEGACY_FORMATS
                elif values[0] in LEGACY_EXTENSIONS:
                    field, allowed = "extensions", LEGACY_EXTENSIONS
                elif values[0] in LEGACY_LIBRARY_TYPES:
                    field, allowed = "library_types", LEGACY_LIBRARY_TYPES
                else:
                    raise LegacyArgumentError("unknown_root_restriction")
                if any(value not in allowed for value in values):
                    raise LegacyArgumentError("mixed_or_unknown_root_restriction")
                if field in restrictions:
                    raise LegacyArgumentError("duplicate_root_restriction")
                restrictions[field] = tuple(dict.fromkeys(values))
            roots.append(LibraryRoot(
                Path(tokens[0]),
                formats=restrictions.get("formats", ()),
                extensions=restrictions.get("extensions", ()),
                library_types=restrictions.get("library_types", ()),
                roles=(role,),
            ))
    return tuple(roots)


def _webhooks(groups: Sequence[Sequence[str]]) -> tuple[str, ...]:
    hooks: dict[str, None] = {}
    for group in groups:
        for token in group:
            if "\x00" in token:
                raise LegacyArgumentError("invalid_webhook_list")
            for hook in token.split(r"\1"):
                if hook:
                    hooks[hook] = None
    return tuple(hooks)


def _arguments(argv: Sequence[str], *, addon: bool) -> list[str]:
    historical: tuple[str, ...] = ("--help", "--paths", "--download_folders")
    if not addon:
        historical += (
            "--webhook", "--bookwalker_webhook_urls", "--new_volume_webhook", "--compress_quality",
            *(next(flag for flag in flags if flag.startswith("--")) for flags, _ in _BOOLEAN_FLAGS),
            *(flag for flag, _ in _INTERVAL_FLAGS),
        )
    normalized: list[str] = []
    for token in argv:
        name, separator, value = token.partition("=")
        if name.startswith("--") and name not in historical:
            matches = tuple(flag for flag in historical if flag.startswith(name))
            if len(matches) == 1:
                token = matches[0] + separator + value
        normalized.append(token)
    return normalized


def parse_legacy_args(argv: Sequence[str], *, addon: bool = False) -> RunRequest:
    """Return invocation policy and structured roots; absence never sets defaults."""
    from ..application.pipeline import RunRequest
    from ..domain.decisions import RunAuthorization
    from ..domain.libraries import normalize_library_roots

    parser = _LegacyParser(
        prog="kce legacy-qbit" if addon else "kce legacy",
        description="Legacy options translated to the installed application. Preview is the default.",
        argument_default=argparse.SUPPRESS,
        allow_abbrev=False,
    )
    parser.add_argument("-p", "--paths", action="append", nargs="*", required=addon)
    parser.add_argument("-df", "--download_folders", "--download-folders", action="append", nargs="*")
    parser.add_argument("--config", type=_config_path)
    for flag in ("apply", "allow-remote", "allow-torrent-removal", "interactive"):
        parser.add_argument(f"--{flag}", action="store_true")
    if addon:
        parser.add_argument("--once", action="store_true")
    else:
        parser.add_argument("-wh", "--webhook", action="append", nargs="*")
        parser.add_argument("-bwk_whs", "--bookwalker_webhook_urls", "--bookwalker-webhook-urls",
                            action="append", nargs="*")
        parser.add_argument("-nw", "--new_volume_webhook", "--new-volume-webhook")
        parser.add_argument("-cq", "--compress_quality", "--compress-quality", type=_quality)
        for flags, destination in _BOOLEAN_FLAGS:
            parser.add_argument(*flags, dest=destination, type=_boolean)
        for flag, destination in _INTERVAL_FLAGS:
            parser.add_argument(flag, flag.replace("_", "-"), dest=destination, type=_interval)
    namespace = vars(parser.parse_args(_arguments(argv, addon=addon)))
    library = _root_groups(namespace.get("paths", ()), role="library")
    downloads = _root_groups(namespace.get("download_folders", ()), role="download")
    if addon and not library:
        raise LegacyArgumentError("library_root_required")
    try:
        roots = normalize_library_roots((*library, *downloads))
    except ValueError:
        raise LegacyArgumentError("conflicting_or_nested_roots") from None
    overrides: dict[str, object] = {}
    if "paths" in namespace:
        overrides["paths"] = tuple(dict.fromkeys(root.path for root in library))
    if "download_folders" in namespace:
        overrides["download_folders"] = tuple(dict.fromkeys(root.path for root in downloads))
    for _, destination in _BOOLEAN_FLAGS:
        if destination in namespace:
            overrides[destination] = namespace[destination]
    for _, destination in _INTERVAL_FLAGS:
        if destination in namespace:
            overrides[destination] = namespace[destination]
    for flag, destination in (("compress_quality", "image_quality"),
                              ("new_volume_webhook", "new_volume_webhook")):
        if flag in namespace:
            overrides[destination] = namespace[flag]
    for flag, destination in (("webhook", "discord_webhook_url"),
                              ("bookwalker_webhook_urls", "bookwalker_webhook_urls")):
        if flag in namespace:
            overrides[destination] = _webhooks(namespace[flag])
    return RunRequest(
        "qbit" if addon else "watch" if overrides.get("watchdog_toggle") else "run",
        roots=roots,
        authorization=RunAuthorization(
            apply=namespace.get("apply", False),
            allow_remote=namespace.get("allow_remote", False),
            allow_torrent_removal=namespace.get("allow_torrent_removal", False),
        ),
        interactive=namespace.get("interactive", False),
        once=namespace.get("once", False),
        config_overrides=tuple(overrides.items()),
        config_path=namespace.get("config"),
    )


def parse_docker_environment(environ: Mapping[str, str]) -> RunRequest:
    """Translate only the uppercase inputs consumed by the historical Docker CMD."""
    arguments: list[str] = []
    for name, flag in _DOCKER_FLAGS:
        value = environ.get(name, "")
        if not isinstance(value, str):
            raise LegacyArgumentError("docker_input_must_be_string")
        if value != "":
            arguments.append(f"{flag}={value}")
    return parse_legacy_args(arguments)

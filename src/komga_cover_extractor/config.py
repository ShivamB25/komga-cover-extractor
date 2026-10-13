"""Validated, immutable application configuration.

The legacy application kept configuration in an executable ``settings.py`` and
mixed it with process state.  This module deliberately accepts data only:
TOML, environment variables, and explicit caller overrides are parsed into a
frozen value object without importing the legacy module or touching services.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urlsplit

import regex

MAX_CONFIG_BYTES = 1024 * 1024
_ENV_PREFIX = "KCE_"
_REDACTED = "********"


class ConfigurationError(ValueError):
    """Raised when configuration input is unknown, malformed, or incomplete."""


@dataclass(frozen=True, slots=True)
class Keyword:
    """One immutable ranked-keyword rule."""

    name: str
    score: float
    file_type: str = "both"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "score": self.score,
            "file_type": self.file_type,
        }


@dataclass(frozen=True, slots=True)
class LibraryType:
    """Immutable classification rule copied from the legacy policy."""

    name: str
    extensions: tuple[str, ...]
    must_contain: tuple[str, ...]
    must_not_contain: tuple[str, ...]
    match_percentage: int = 90

    def __post_init__(self) -> None:
        for name in ("extensions", "must_contain", "must_not_contain"):
            value = getattr(self, name)
            if isinstance(value, (list, tuple)):
                object.__setattr__(self, name, tuple(value))

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "extensions": list(self.extensions),
            "must_contain": list(self.must_contain),
            "must_not_contain": list(self.must_not_contain),
            "match_percentage": self.match_percentage,
        }


@dataclass(frozen=True, slots=True)
class KomgaPathMapping:
    """Map a local library root to its Komga-visible root."""

    local_root: Path
    remote_root: str

    def __post_init__(self) -> None:
        if not isinstance(self.local_root, Path):
            object.__setattr__(self, "local_root", Path(self.local_root))

    def as_dict(self) -> dict[str, object]:
        return {"local_root": str(self.local_root), "remote_root": self.remote_root}


@dataclass(frozen=True, slots=True, repr=False)
class Configuration:
    """A complete, configuration-only snapshot.

    Collection-valued attributes are tuples and ranked keyword records are
    frozen dataclasses.  ``sources`` is a read-only mapping whose values are
    ``default``, ``toml``, ``env``, or ``cli``.
    """

    # settings.py values (53 canonical settings)
    correct_file_extensions_toggle: bool
    convert_to_cbz_toggle: bool
    delete_unacceptable_files_toggle: bool
    delete_chapters_from_downloads_toggle: bool
    rename_files_in_download_folders_toggle: bool
    create_folders_for_items_in_download_folder_toggle: bool
    rename_dirs_in_download_folder_toggle: bool
    check_for_duplicate_volumes_toggle: bool
    extract_covers_toggle: bool
    check_for_existing_series_toggle: bool
    check_for_missing_volumes_toggle: bool
    send_scan_request_to_komga_libraries_toggle: bool
    uncheck_non_qbit_upgrades_toggle: bool
    preferred_volume_renaming_format: str
    add_volume_one_number_to_one_shots: bool
    add_issue_number_to_manga_file_name: bool
    manual_rename: bool
    restructure_when_renaming: bool
    search_and_add_premium_to_file_name: bool
    add_publisher_name_to_file_name_when_renaming: bool
    exception_keywords: tuple[str, ...]
    move_release_group_to_end_of_file_name: bool
    replace_unicode_when_restructuring: bool
    mute_discord_rename_notifications: bool
    move_lone_files_to_similar_folder: bool
    replace_series_name_in_file_name_with_similar_folder_name: bool
    match_through_identifiers: bool
    required_similarity_score: float
    ranked_keywords: tuple[Keyword, ...]
    ignored_folder_names: tuple[str, ...]
    log_to_file: bool
    unacceptable_keywords: tuple[str, ...]
    komga_ip: str
    komga_port: str
    komga_login_email: str
    komga_login_password: str
    generate_release_group_list_toggle: bool
    chapter_support_toggle: bool
    preferred_chapter_renaming_format: str
    output_chapter_covers_to_discord: bool
    rename_chapters_with_preferred_chapter_keyword: bool
    extract_chapter_covers: bool
    compare_detected_cover_to_blank_images: bool
    use_latest_volume_cover_as_series_cover: bool
    rename_zip_to_cbz: bool
    auto_classify_watchdog_paths: bool
    qbittorrent_ip: str
    qbittorrent_port: str
    qbittorrent_target_category: str
    qbittorrent_username: str
    qbittorrent_password: str
    delete_unacceptable_torrent_titles_in_qbit: bool

    # Main-module CLI policy values (13 declarations; log_to_file is shared)
    paths: tuple[Path, ...]
    download_folders: tuple[Path, ...]
    discord_webhook_url: tuple[str, ...]
    bookwalker_check: bool
    compress_image_option: bool
    image_quality: int
    bookwalker_webhook_urls: tuple[str, ...]
    watchdog_toggle: bool
    new_volume_webhook: str
    watchdog_discover_new_files_check_interval: int
    watchdog_file_transferred_check_interval: int
    output_covers_as_webp: bool

    # Explicit main-owned parsing, matching, cover, and organization policy.
    volume_keywords: tuple[str, ...]
    chapter_keywords: tuple[str, ...]
    exclusion_keywords: tuple[str, ...]
    subtitle_exclusion_keywords: tuple[str, ...]
    cover_patterns: tuple[str, ...]
    library_types: tuple[LibraryType, ...]
    zfill_volume_int_value: int
    zfill_volume_float_value: int
    zfill_chapter_int_value: int
    zfill_chapter_float_value: int
    publisher_similarity_score: float
    release_group_similarity_score: float
    required_matching_percentage: int
    match_through_image_similarity: bool
    required_image_similarity_score: float
    blank_cover_required_similarity_score: float
    series_cover_file_names: tuple[str, ...]
    manual_delete: bool
    copy_existing_volume_covers_toggle: bool
    move_series_to_correct_library_toggle: bool
    move_new_series_to_library_toggle: bool
    average_chapter_image_count: int
    return_data_image_quality: int

    # State and validated resource/transport/watch policy.
    state_dir: Path
    log_dir: Path
    archive_max_entries: int
    cover_max_candidates: int
    metadata_max_bytes: int
    cover_member_max_bytes: int
    conversion_member_max_bytes: int
    cover_read_max_bytes: int
    archive_expansion_max_bytes: int
    conversion_scratch_max_bytes: int
    archive_max_ratio: float
    image_max_pixels: int
    conversion_timeout_seconds: int
    http_connect_timeout_seconds: int
    http_read_timeout_seconds: int
    http_max_attempts: int
    bookwalker_request_timeout_seconds: int
    bookwalker_request_interval_seconds: int
    bookwalker_max_pages: int
    qbit_poll_interval_seconds: int
    qbit_reconnect_interval_seconds: int
    qbit_target_states: tuple[str, ...]
    komga_path_mappings: tuple[KomgaPathMapping, ...]
    watch_queue_capacity: int
    watch_reconcile_interval_seconds: int
    watch_max_pending_jobs: int
    watch_retry_attempts: int
    watch_shutdown_timeout_seconds: int

    sources: Mapping[str, str]
    compiled_policy_patterns: Mapping[str, tuple[object, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # A caller can construct the dataclass directly, so make every
        # collection immutable even outside the loader factory functions.
        for name in (
            _PATH_KEYS
            | _STRING_LIST_KEYS
            | _WEBHOOK_LIST_KEYS
            | _RECORD_LIST_KEYS
        ):
            value = getattr(self, name)
            if isinstance(value, (list, tuple)):
                object.__setattr__(self, name, tuple(value))
        for name in _SCALAR_PATH_KEYS:
            value = getattr(self, name)
            if not isinstance(value, Path):
                object.__setattr__(self, name, Path(value))
        if isinstance(self.ranked_keywords, (list, tuple)):
            object.__setattr__(
                self,
                "ranked_keywords",
                tuple(_coerce_keyword("ranked_keywords", item) for item in self.ranked_keywords),
            )
        if isinstance(self.library_types, (list, tuple)):
            records = tuple(self.library_types)
            if any(not isinstance(item, LibraryType) for item in records):
                records = tuple(
                    _coerce_library_type("library_types", item) for item in records
                )
            object.__setattr__(self, "library_types", records)
        if isinstance(self.komga_path_mappings, (list, tuple)):
            mappings = tuple(self.komga_path_mappings)
            if any(not isinstance(item, KomgaPathMapping) for item in mappings):
                base_dir = Path.cwd()
                mappings = tuple(
                    item
                    if isinstance(item, KomgaPathMapping)
                    else _coerce_komga_path_mapping(item, base_dir=base_dir)
                    for item in mappings
                )
            object.__setattr__(self, "komga_path_mappings", mappings)
        if not isinstance(self.sources, MappingProxyType):
            object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(
            self,
            "compiled_policy_patterns",
            MappingProxyType(_compile_policy_patterns(self)),
        )

    def as_dict(self, *, redact: bool = True) -> dict[str, object]:
        """Return a plain, serialization-friendly snapshot.

        ``redact=True`` replaces credential, endpoint, and webhook values.  A
        fresh structure is returned on every call, so mutating the result
        cannot mutate this configuration.
        """

        result: dict[str, object] = {}
        for field_info in fields(self):
            name = field_info.name
            if name in {"sources", "compiled_policy_patterns"}:
                continue
            value = getattr(self, name)
            result[name] = _public_value(name, value, redact=redact)
        return result

    def policy_fingerprint(self, *, rule_version: int = 1) -> str:
        """Hash secret-free behavior settings and the caller's rule version."""
        values = self.as_dict(redact=False)
        for name in _SECRET_KEYS | {"state_dir", "log_dir"}:
            values.pop(name, None)
        payload = {"rule_version": rule_version, "settings": values}
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def __repr__(self) -> str:
        # Never use the dataclass-generated repr: it would expose passwords and
        # webhook URLs.  The default redacted snapshot is intentionally used.
        return f"Configuration({self.as_dict()!r})"


# Keep these sets explicit rather than deriving a generalized schema.  They
# make the accepted surface auditable and keep validation rules near callers.
_BOOL_KEYS = frozenset(
    {
        "correct_file_extensions_toggle",
        "convert_to_cbz_toggle",
        "delete_unacceptable_files_toggle",
        "delete_chapters_from_downloads_toggle",
        "rename_files_in_download_folders_toggle",
        "create_folders_for_items_in_download_folder_toggle",
        "rename_dirs_in_download_folder_toggle",
        "check_for_duplicate_volumes_toggle",
        "extract_covers_toggle",
        "check_for_existing_series_toggle",
        "check_for_missing_volumes_toggle",
        "send_scan_request_to_komga_libraries_toggle",
        "uncheck_non_qbit_upgrades_toggle",
        "add_volume_one_number_to_one_shots",
        "add_issue_number_to_manga_file_name",
        "manual_rename",
        "restructure_when_renaming",
        "search_and_add_premium_to_file_name",
        "add_publisher_name_to_file_name_when_renaming",
        "move_release_group_to_end_of_file_name",
        "replace_unicode_when_restructuring",
        "mute_discord_rename_notifications",
        "move_lone_files_to_similar_folder",
        "replace_series_name_in_file_name_with_similar_folder_name",
        "match_through_identifiers",
        "log_to_file",
        "generate_release_group_list_toggle",
        "chapter_support_toggle",
        "output_chapter_covers_to_discord",
        "rename_chapters_with_preferred_chapter_keyword",
        "extract_chapter_covers",
        "compare_detected_cover_to_blank_images",
        "match_through_image_similarity",
        "manual_delete",
        "copy_existing_volume_covers_toggle",
        "move_series_to_correct_library_toggle",
        "move_new_series_to_library_toggle",
        "use_latest_volume_cover_as_series_cover",
        "rename_zip_to_cbz",
        "auto_classify_watchdog_paths",
        "delete_unacceptable_torrent_titles_in_qbit",
        "bookwalker_check",
        "compress_image_option",
        "watchdog_toggle",
        "output_covers_as_webp",
    }
)

_STRING_KEYS = frozenset(
    {
        "preferred_volume_renaming_format",
        "preferred_chapter_renaming_format",
        "komga_ip",
        "komga_port",
        "komga_login_email",
        "komga_login_password",
        "qbittorrent_ip",
        "qbittorrent_port",
        "qbittorrent_target_category",
        "qbittorrent_username",
        "qbittorrent_password",
        "new_volume_webhook",
    }
)
_PATH_KEYS = frozenset({"paths", "download_folders"})
_SCALAR_PATH_KEYS = frozenset({"state_dir", "log_dir"})
_STRING_LIST_KEYS = frozenset(
    {
        "exception_keywords",
        "ignored_folder_names",
        "unacceptable_keywords",
        "volume_keywords",
        "chapter_keywords",
        "exclusion_keywords",
        "subtitle_exclusion_keywords",
        "cover_patterns",
        "series_cover_file_names",
        "qbit_target_states",
    }
)
_WEBHOOK_LIST_KEYS = frozenset({"discord_webhook_url", "bookwalker_webhook_urls"})
_RECORD_LIST_KEYS = frozenset({"library_types", "komga_path_mappings"})
_LIST_KEYS = (
    _PATH_KEYS
    | _STRING_LIST_KEYS
    | _WEBHOOK_LIST_KEYS
    | _RECORD_LIST_KEYS
    | {"ranked_keywords"}
)
_INT_KEYS = frozenset(
    {
        "image_quality",
        "return_data_image_quality",
        "watchdog_discover_new_files_check_interval",
        "watchdog_file_transferred_check_interval",
        "zfill_volume_int_value",
        "zfill_volume_float_value",
        "zfill_chapter_int_value",
        "zfill_chapter_float_value",
        "required_matching_percentage",
        "average_chapter_image_count",
        "archive_max_entries",
        "cover_max_candidates",
        "metadata_max_bytes",
        "cover_member_max_bytes",
        "conversion_member_max_bytes",
        "cover_read_max_bytes",
        "archive_expansion_max_bytes",
        "conversion_scratch_max_bytes",
        "image_max_pixels",
        "conversion_timeout_seconds",
        "http_connect_timeout_seconds",
        "http_read_timeout_seconds",
        "http_max_attempts",
        "bookwalker_request_timeout_seconds",
        "bookwalker_request_interval_seconds",
        "bookwalker_max_pages",
        "qbit_poll_interval_seconds",
        "qbit_reconnect_interval_seconds",
        "watch_queue_capacity",
        "watch_reconcile_interval_seconds",
        "watch_max_pending_jobs",
        "watch_retry_attempts",
        "watch_shutdown_timeout_seconds",
    }
)
_FLOAT_KEYS = frozenset(
    {
        "required_similarity_score",
        "publisher_similarity_score",
        "release_group_similarity_score",
        "required_image_similarity_score",
        "blank_cover_required_similarity_score",
        "archive_max_ratio",
    }
)
_REGEX_KEYS = frozenset(
    {
        "exception_keywords",
        "unacceptable_keywords",
        "volume_keywords",
        "chapter_keywords",
        "exclusion_keywords",
        "subtitle_exclusion_keywords",
        "cover_patterns",
    }
)
_PORT_KEYS = frozenset({"komga_port", "qbittorrent_port"})

_MAX_CONFIG_INTEGER = (1 << 63) - 1
_TIMEOUT_KEYS = frozenset(
    {
        "conversion_timeout_seconds",
        "http_connect_timeout_seconds",
        "http_read_timeout_seconds",
        "bookwalker_request_timeout_seconds",
        "watch_shutdown_timeout_seconds",
    }
)
_INTERVAL_KEYS = frozenset(
    {
        "watchdog_discover_new_files_check_interval",
        "watchdog_file_transferred_check_interval",
        "bookwalker_request_interval_seconds",
        "qbit_poll_interval_seconds",
        "qbit_reconnect_interval_seconds",
        "watch_reconcile_interval_seconds",
    }
)
_COUNT_LIMITS = {
    "archive_max_entries": 10_000_000,
    "cover_max_candidates": 10_000_000,
    "watch_queue_capacity": 10_000_000,
    "watch_max_pending_jobs": 10_000_000,
}
_POLICY_TOKEN_KEYS = frozenset(
    {
        "volume_keywords",
        "chapter_keywords",
        "exclusion_keywords",
        "subtitle_exclusion_keywords",
        "cover_patterns",
        "series_cover_file_names",
        "qbit_target_states",
    }
)

_SECRET_KEYS = frozenset(
    {
        "komga_ip",
        "komga_port",
        "komga_login_email",
        "komga_login_password",
        "qbittorrent_ip",
        "qbittorrent_port",
        "qbittorrent_target_category",
        "qbittorrent_username",
        "qbittorrent_password",
        "discord_webhook_url",
        "bookwalker_webhook_urls",
        "new_volume_webhook",
    }
)

_ALIAS_TO_CANONICAL = {"resturcture_when_renaming": "restructure_when_renaming"}
LEGACY_SETTING_NAMES = frozenset(
    {
        "correct_file_extensions_toggle",
        "convert_to_cbz_toggle",
        "delete_unacceptable_files_toggle",
        "delete_chapters_from_downloads_toggle",
        "rename_files_in_download_folders_toggle",
        "create_folders_for_items_in_download_folder_toggle",
        "rename_dirs_in_download_folder_toggle",
        "check_for_duplicate_volumes_toggle",
        "extract_covers_toggle",
        "check_for_existing_series_toggle",
        "check_for_missing_volumes_toggle",
        "cache_each_root_for_each_path_in_paths_at_beginning_toggle",
        "send_scan_request_to_komga_libraries_toggle",
        "uncheck_non_qbit_upgrades_toggle",
        "preferred_volume_renaming_format",
        "add_volume_one_number_to_one_shots",
        "add_issue_number_to_manga_file_name",
        "manual_rename",
        "resturcture_when_renaming",
        "search_and_add_premium_to_file_name",
        "add_publisher_name_to_file_name_when_renaming",
        "exception_keywords",
        "move_release_group_to_end_of_file_name",
        "replace_unicode_when_restructuring",
        "mute_discord_rename_notifications",
        "move_lone_files_to_similar_folder",
        "replace_series_name_in_file_name_with_similar_folder_name",
        "match_through_identifiers",
        "required_similarity_score",
        "ranked_keywords",
        "ignored_folder_names",
        "log_to_file",
        "unacceptable_keywords",
        "komga_ip",
        "komga_port",
        "komga_login_email",
        "komga_login_password",
        "generate_release_group_list_toggle",
        "chapter_support_toggle",
        "preferred_chapter_renaming_format",
        "output_chapter_covers_to_discord",
        "rename_chapters_with_preferred_chapter_keyword",
        "extract_chapter_covers",
        "compare_detected_cover_to_blank_images",
        "use_latest_volume_cover_as_series_cover",
        "rename_zip_to_cbz",
        "auto_classify_watchdog_paths",
        "qbittorrent_ip",
        "qbittorrent_port",
        "qbittorrent_target_category",
        "qbittorrent_username",
        "qbittorrent_password",
        "delete_unacceptable_torrent_titles_in_qbit",
    }
)

_DEFAULT_VALUES: dict[str, object] = {
    "correct_file_extensions_toggle": False,
    "convert_to_cbz_toggle": False,
    "delete_unacceptable_files_toggle": False,
    "delete_chapters_from_downloads_toggle": False,
    "rename_files_in_download_folders_toggle": False,
    "create_folders_for_items_in_download_folder_toggle": False,
    "rename_dirs_in_download_folder_toggle": False,
    "check_for_duplicate_volumes_toggle": False,
    "extract_covers_toggle": True,
    "check_for_existing_series_toggle": False,
    "check_for_missing_volumes_toggle": False,
    "send_scan_request_to_komga_libraries_toggle": False,
    "uncheck_non_qbit_upgrades_toggle": False,
    "preferred_volume_renaming_format": "v",
    "add_volume_one_number_to_one_shots": False,
    "add_issue_number_to_manga_file_name": False,
    "manual_rename": True,
    "restructure_when_renaming": False,
    "search_and_add_premium_to_file_name": False,
    "add_publisher_name_to_file_name_when_renaming": False,
    "exception_keywords": (
        r"Extra",
        r"Ones?(-|)shot",
        r"Omake",
        r"Special",
        r"Bonus",
        r"Side(-|)story",
    ),
    "move_release_group_to_end_of_file_name": False,
    "replace_unicode_when_restructuring": False,
    "mute_discord_rename_notifications": False,
    "move_lone_files_to_similar_folder": False,
    "replace_series_name_in_file_name_with_similar_folder_name": False,
    "match_through_identifiers": False,
    "required_similarity_score": 0.979,
    "ranked_keywords": (),
    "ignored_folder_names": (),
    "log_to_file": False,
    "unacceptable_keywords": (),
    "komga_ip": "",
    "komga_port": "",
    "komga_login_email": "",
    "komga_login_password": "",
    "generate_release_group_list_toggle": False,
    "chapter_support_toggle": False,
    "preferred_chapter_renaming_format": "c",
    "output_chapter_covers_to_discord": False,
    "rename_chapters_with_preferred_chapter_keyword": False,
    "extract_chapter_covers": False,
    "compare_detected_cover_to_blank_images": False,
    "use_latest_volume_cover_as_series_cover": False,
    "rename_zip_to_cbz": True,
    "auto_classify_watchdog_paths": False,
    "qbittorrent_ip": "",
    "qbittorrent_port": "",
    "qbittorrent_target_category": "",
    "qbittorrent_username": "",
    "qbittorrent_password": "",
    "delete_unacceptable_torrent_titles_in_qbit": False,
    "paths": (),
    "download_folders": (),
    "discord_webhook_url": (),
    "bookwalker_check": False,
    "compress_image_option": False,
    "image_quality": 40,
    "bookwalker_webhook_urls": (),
    "watchdog_toggle": False,
    "new_volume_webhook": "",
    "watchdog_discover_new_files_check_interval": 5,
    "watchdog_file_transferred_check_interval": 1,
    "output_covers_as_webp": False,
    "volume_keywords": (
        "LN",
        "Light Novels?",
        "Novels?",
        "Books?",
        "Volumes?",
        "Vols?",
        "Discs?",
        "Tomo",
        "Tome",
        "Von",
        "V",
        "第",
        "T",
    ),
    "chapter_keywords": ("Chapters?", "Chaps?", "Chs?", "Cs?", "D"),
    "exclusion_keywords": (
        r"(\s)Part(\s)",
        r"(\s)Episode(\s)",
        r"(\s)Season(\s)",
        r"(\s)Arc(\s)",
        r"(\s)Prologue(\s)",
        r"(\s)Epilogue(\s)",
        r"(\s)Omake(\s)",
        r"(\s)Extra(\s)",
        r"(\s)- Special(\s)",
        r"(\s)Side Story(\s)",
        r"(\s)Act(\s)",
        r"(\s)Special Episode(\s)",
        r"(\s)Ep(\s)",
        r"(\s)- Version(\s)",
        r"(\s)Ver(\s)",
        r"(\s)PT\.",
        r"(\s)PT(\s)",
        r",",
        r"(\s)×",
        r"\d\s*-\s*",
        r"\bNo.",
        r"\bNo.(\s)",
        r"\bBonus(\s)",
        r"(\]|\}|\)) -",
        r"\bZom(\s)",
        "Tail -",
        "꞉",
        ":",
        r"\d\.",
    ),
    "subtitle_exclusion_keywords": (r"-(\s)", r"-", r"-\s[A-Za-z]+\s"),
    "cover_patterns": (
        r"(cover\.([A-Za-z]+))$",
        r"(\b(Cover([0-9]+|)|CoverDesign|page([-_. ]+)?cover)\b)",
        r"(\b(p000|page_000)\b)",
        r"((\s+)0+\.(.{2,}))",
        r"(\bindex[-_. ]1[-_. ]1\b)",
        r"(9([-_. :]+)?7([-_. :]+)?(8|9)(([-_. :]+)?[0-9]){10})",
    ),
    "library_types": (
        LibraryType(
            "manga",
            (".zip", ".cbz"),
            (r"\(Digital(\s(SD|HD))?\)",),
            (
                r"Webtoon",
                r"^(?=.*Digital)((?=.*Compilation)|(?=.*danke-repack))",
            ),
            1,
        ),
        LibraryType(
            "light novel",
            (".epub",),
            (
                r"\[[^\]]*(Lucaz|Stick|Oak|Yen (Press|On)|J-Novel|Seven Seas|Vertical|One Peace Books|Cross Infinite|Sol Press|Hanashi Media|Kodansha|Tentai Books|SB Creative|Hobby Japan|Impress Corporation|KADOKAWA|Viz Media|Tokyopop|Inori Books|Wuxiaworld)[^\]]*\]|(faratnis)",
            ),
            (),
            90,
        ),
        LibraryType(
            "digital_comps",
            (".zip", ".cbz"),
            (r"^(?=.*Digital)((?=.*Compilation)|(?=.*danke-repack))",),
            (),
            90,
        ),
    ),
    "zfill_volume_int_value": 2,
    "zfill_volume_float_value": 4,
    "zfill_chapter_int_value": 3,
    "zfill_chapter_float_value": 5,
    "publisher_similarity_score": 0.9,
    "release_group_similarity_score": 0.8,
    "required_matching_percentage": 90,
    "match_through_image_similarity": True,
    "required_image_similarity_score": 0.9,
    "blank_cover_required_similarity_score": 0.9,
    "series_cover_file_names": ("cover", "poster"),
    "manual_delete": False,
    "copy_existing_volume_covers_toggle": False,
    "move_series_to_correct_library_toggle": False,
    "move_new_series_to_library_toggle": False,
    "average_chapter_image_count": 85,
    "return_data_image_quality": 60,
    "state_dir": None,
    "log_dir": None,
    "archive_max_entries": 100000,
    "cover_max_candidates": 10000,
    "metadata_max_bytes": 4194304,
    "cover_member_max_bytes": 134217728,
    "conversion_member_max_bytes": 536870912,
    "cover_read_max_bytes": 1073741824,
    "archive_expansion_max_bytes": 8589934592,
    "conversion_scratch_max_bytes": 8589934592,
    "archive_max_ratio": 1000,
    "image_max_pixels": 100000000,
    "conversion_timeout_seconds": 300,
    "http_connect_timeout_seconds": 5,
    "http_read_timeout_seconds": 15,
    "http_max_attempts": 3,
    "bookwalker_request_timeout_seconds": 10,
    "bookwalker_request_interval_seconds": 2,
    "bookwalker_max_pages": 5,
    "qbit_poll_interval_seconds": 30,
    "qbit_reconnect_interval_seconds": 60,
    "qbit_target_states": ("pausedDL", "stoppedDL"),
    "komga_path_mappings": (),
    "watch_queue_capacity": 256,
    "watch_reconcile_interval_seconds": 10,
    "watch_max_pending_jobs": 4096,
    "watch_retry_attempts": 3,
    "watch_shutdown_timeout_seconds": 30,

}

_FIELD_ORDER = tuple(
    field_info.name
    for field_info in fields(Configuration)
    if field_info.name not in {"sources", "compiled_policy_patterns"}
)
_FIELD_NAMES = frozenset(_FIELD_ORDER)
_KNOWN_KEYS = _FIELD_NAMES | frozenset(_ALIAS_TO_CANONICAL)


def _public_value(name: str, value: object, *, redact: bool) -> object:
    if redact and name in _SECRET_KEYS:
        if name in _WEBHOOK_LIST_KEYS:
            if isinstance(value, (tuple, list)):
                return [_REDACTED for _ in value]
            return _REDACTED
        if isinstance(value, str):
            return _REDACTED if value else ""
    if name in _PATH_KEYS:
        if isinstance(value, (tuple, list)):
            return [str(item) for item in value]
        return value
    if name in _SCALAR_PATH_KEYS:
        return str(value)
    if name == "ranked_keywords":
        if isinstance(value, (tuple, list)):
            return [
                item.as_dict()
                for item in cast(tuple[Keyword, ...], tuple(value))
            ]
        return value
    if name == "library_types":
        return [item.as_dict() for item in cast(tuple[LibraryType, ...], value)]
    if name == "komga_path_mappings":
        return [
            item.as_dict() for item in cast(tuple[KomgaPathMapping, ...], value)
        ]
    if isinstance(value, tuple):
        return list(value)
    return value

def _compile_policy_patterns(config: Configuration) -> dict[str, tuple[object, ...]]:
    """Compile only the patterns captured by this immutable snapshot."""

    compiled: dict[str, tuple[object, ...]] = {}

    def compile_group(key: str, patterns: tuple[str, ...]) -> None:
        result: list[object] = []
        for index, pattern in enumerate(patterns):
            try:
                result.append(regex.compile(pattern, flags=regex.IGNORECASE))
            except Exception:
                raise _source_error(key, f"invalid regex at index {index}") from None
        compiled[key] = tuple(result)

    for key in sorted(_REGEX_KEYS):
        compile_group(key, cast(tuple[str, ...], getattr(config, key)))
    compile_group(
        "ranked_keywords",
        tuple(item.name for item in config.ranked_keywords),
    )
    for index, library_type in enumerate(config.library_types):
        compile_group(
            f"library_types[{index}].must_contain",
            library_type.must_contain,
        )
        compile_group(
            f"library_types[{index}].must_not_contain",
            library_type.must_not_contain,
        )
    return compiled


def _source_error(key: str, reason: str) -> ConfigurationError:
    # Reasons supplied by this module are static and never include the input
    # value.  Keeping this helper central prevents an accidental secret echo.
    return ConfigurationError(f"{key}: {reason}")


def _canonical_key(raw_key: object) -> str:
    if not isinstance(raw_key, str):
        raise ConfigurationError("configuration key must be a string")
    if raw_key not in _KNOWN_KEYS:
        raise _source_error(raw_key, "unknown configuration key")
    return _ALIAS_TO_CANONICAL.get(raw_key, raw_key)


def _normalise_mapping(
    values: Mapping[str, object],
    *,
    base_dir: Path,
    source: str,
    env: bool = False,
    skip_none: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {}
    seen: set[str] = set()
    for raw_key, raw_value in values.items():
        canonical = _canonical_key(raw_key)
        if skip_none and raw_value is None:
            continue
        if canonical in seen:
            raise _source_error(canonical, "canonical and legacy aliases conflict in one source")
        seen.add(canonical)
        result[canonical] = _coerce_value(
            canonical,
            raw_value,
            base_dir=base_dir,
            source=source,
            env=env,
        )
    return result


def _parse_env_value(key: str, raw_value: object) -> object:
    if not isinstance(raw_value, str):
        raise _source_error(key, "environment values must be strings")
    # A TOML literal permits arrays/tables and also handles quoted strings,
    # booleans, integers, and finite floats.  Bare strings remain useful for
    # paths, URLs, and names, so preserve them when literal parsing fails.
    try:
        parsed = tomllib.loads(f"value = {raw_value}\n")["value"]
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return raw_value
    return parsed


def _collect_environment(
    environ: Mapping[str, str], *, base_dir: Path
) -> tuple[dict[str, object], dict[str, str]]:
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    seen: set[str] = set()
    for env_key, raw_value in environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        raw_name = env_key[len(_ENV_PREFIX) :]
        if not raw_name:
            raise ConfigurationError("KCE_: environment key is empty")
        name = raw_name.lower()
        canonical = _canonical_key(name)
        if canonical in seen:
            raise _source_error(canonical, "canonical and legacy aliases conflict in one source")
        seen.add(canonical)
        values[canonical] = _coerce_value(
            canonical,
            _parse_env_value(canonical, raw_value),
            base_dir=base_dir,
            source="env",
            env=True,
        )
        sources[canonical] = "env"
    return values, sources


def _coerce_value(
    key: str,
    value: object,
    *,
    base_dir: Path,
    source: str,
    env: bool,
) -> object:
    if key in _BOOL_KEYS:
        if env and isinstance(value, str):
            lowered = value.lower()
            if lowered in {"true", "false"}:
                return lowered == "true"
        if type(value) is not bool:
            raise _source_error(key, "expected boolean true or false")
        return value

    if key in _INT_KEYS:
        result: int | float = _coerce_int(key, value)
        if result > _MAX_CONFIG_INTEGER:
            raise _source_error(key, "exceeds the supported integer range")
        if key in {"image_quality", "return_data_image_quality"}:
            if not 1 <= result <= 100:
                raise _source_error(key, "must be between 1 and 100")
        elif key == "required_matching_percentage":
            if not 0 <= result <= 100:
                raise _source_error(key, "must be between 0 and 100")
        elif result <= 0:
            raise _source_error(key, "must be a positive integer")
        if key in {"http_connect_timeout_seconds", "http_read_timeout_seconds",
                   "bookwalker_request_timeout_seconds"} and result > 60:
            raise _source_error(key, "must not exceed the 60-second HTTP budget")
        if key in _TIMEOUT_KEYS and result > 3600:
            raise _source_error(key, "must not exceed 3600 seconds")
        if key in _INTERVAL_KEYS and result > 86400:
            raise _source_error(key, "must not exceed 86400 seconds")
        if key == "http_max_attempts" and result > 3:
            raise _source_error(key, "must not exceed 3 attempts")
        if key == "bookwalker_max_pages" and result > 100:
            raise _source_error(key, "must not exceed 100 pages")
        if key == "watch_retry_attempts" and result > 10:
            raise _source_error(key, "must not exceed 10 attempts")
        if key in _COUNT_LIMITS and result > _COUNT_LIMITS[key]:
            raise _source_error(key, "exceeds the supported count limit")
        return result

    if key in _FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _source_error(key, "expected a finite number")
        try:
            result = float(value)
        except (OverflowError, ValueError):
            raise _source_error(key, "must be finite") from None
        if not math.isfinite(result):
            raise _source_error(key, "must be finite")
        if key == "archive_max_ratio":
            if result <= 0:
                raise _source_error(key, "must be positive")
        elif not 0 <= result <= 1:
            raise _source_error(key, "must be between 0 and 1")
        return result

    if key in _PORT_KEYS:
        return _coerce_port(key, value)
    if key in _PATH_KEYS:
        return _coerce_paths(key, value, base_dir=base_dir, env=env)
    if key in _SCALAR_PATH_KEYS:
        return _coerce_scalar_path(key, value, base_dir=base_dir)
    if key in _STRING_LIST_KEYS or key in _WEBHOOK_LIST_KEYS:
        return _coerce_string_list(key, value, env=env)
    if key == "ranked_keywords":
        if not isinstance(value, (list, tuple)):
            raise _source_error(key, "expected an array of keyword records")
        return tuple(_coerce_keyword(key, item) for item in value)
    if key == "library_types":
        if not isinstance(value, (list, tuple)):
            raise _source_error(key, "expected an array of library-type records")
        records = tuple(_coerce_library_type(key, item) for item in value)
        names = [record.name.casefold() for record in records]
        if len(names) != len(set(names)):
            raise _source_error(key, "record names must be unique")
        return records
    if key == "komga_path_mappings":
        if not isinstance(value, (list, tuple)):
            raise _source_error(key, "expected an array of path-mapping records")
        mappings = tuple(
            _coerce_komga_path_mapping(item, base_dir=base_dir) for item in value
        )
        roots = [mapping.local_root for mapping in mappings]
        if len(roots) != len(set(roots)):
            raise _source_error(key, "local roots must be unique")
        return mappings
    if key in _STRING_KEYS:
        if not isinstance(value, str):
            raise _source_error(key, "expected a string")
        if key in {
            "preferred_volume_renaming_format",
            "preferred_chapter_renaming_format",
        } and value.strip() == "":
            raise _source_error(key, "must not be empty")
        return value

    raise _source_error(key, "unsupported configuration field")


def _coerce_int(key: str, value: object) -> int:
    if isinstance(value, bool):
        raise _source_error(key, "expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            # int() accepts surrounding whitespace; CLI/env numeric values are
            # scalar syntax, so reject it rather than silently changing input.
            if not value or value != value.strip() or value[0] in {"+", "-"}:
                raise ValueError
            return int(value, 10)
        except (TypeError, ValueError):
            raise _source_error(key, "expected an integer") from None
    raise _source_error(key, "expected an integer")


def _coerce_port(key: str, value: object) -> str:
    if isinstance(value, bool):
        raise _source_error(key, "expected a port number or empty string")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if value == "":
            return ""
        if not value.isdecimal():
            raise _source_error(key, "expected a port number or empty string")
        number = int(value, 10)
    else:
        raise _source_error(key, "expected a port number or empty string")
    if not 1 <= number <= 65535:
        raise _source_error(key, "must be between 1 and 65535")
    return str(number)


def _coerce_paths(
    key: str, value: object, *, base_dir: Path, env: bool
) -> tuple[Path, ...]:
    if env and isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _source_error(key, "expected an array of paths")
    result: list[Path] = []
    for item in value:
        if not isinstance(item, (str, Path)):
            raise _source_error(key, "path entries must be strings")
        if isinstance(item, str) and item == "":
            raise _source_error(key, "path entries must not be empty")
        path = Path(item)
        if not path.is_absolute():
            path = base_dir / path
        result.append(path.absolute())
    return tuple(result)


def _coerce_scalar_path(key: str, value: object, *, base_dir: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise _source_error(key, "expected a path string")
    if isinstance(value, str) and value == "":
        raise _source_error(key, "path must not be empty")
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.absolute()


def _coerce_string_list(key: str, value: object, *, env: bool) -> tuple[str, ...]:
    if env and isinstance(value, str):
        value = [] if value == "" else [value]
    if not isinstance(value, (list, tuple)):
        raise _source_error(key, "expected an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _source_error(key, "array entries must be strings")
        if key in _POLICY_TOKEN_KEYS and item.strip() == "":
            raise _source_error(key, "array entries must not be empty")
        result.append(item)
    return tuple(result)


def _coerce_record_string_list(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _source_error(key, "expected an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item.strip() == "":
            raise _source_error(key, "record string entries must not be empty")
        result.append(item)
    return tuple(result)


def _coerce_library_type(key: str, value: object) -> LibraryType:
    allowed = {
        "name",
        "extensions",
        "must_contain",
        "must_not_contain",
        "match_percentage",
    }
    if isinstance(value, LibraryType):
        record: Mapping[str, object] = value.as_dict()
    elif isinstance(value, Mapping):
        record = value
    else:
        raise _source_error(key, "library-type records must be tables")
    if set(record) != allowed:
        raise _source_error(
            key,
            "library-type records require exactly name, extensions, must_contain, must_not_contain, and match_percentage",
        )
    name = record["name"]
    if not isinstance(name, str) or name.strip() == "":
        raise _source_error(key, "library-type name must not be empty")
    extensions = _coerce_record_string_list(key, record["extensions"])
    if not extensions:
        raise _source_error(key, "library-type extensions must not be empty")
    must_contain = _coerce_record_string_list(key, record["must_contain"])
    must_not_contain = _coerce_record_string_list(key, record["must_not_contain"])
    match_percentage = _coerce_int(
        f"{key}.match_percentage", record["match_percentage"]
    )
    if not 0 <= match_percentage <= 100:
        raise _source_error(f"{key}.match_percentage", "must be between 0 and 100")
    return LibraryType(
        name=name,
        extensions=extensions,
        must_contain=must_contain,
        must_not_contain=must_not_contain,
        match_percentage=match_percentage,
    )


def _coerce_komga_path_mapping(
    value: object, *, base_dir: Path
) -> KomgaPathMapping:
    key = "komga_path_mappings"
    allowed = {"local_root", "remote_root"}
    if isinstance(value, KomgaPathMapping):
        record: Mapping[str, object] = {
            "local_root": value.local_root,
            "remote_root": value.remote_root,
        }
    elif isinstance(value, Mapping):
        record = value
    else:
        raise _source_error(key, "path-mapping records must be tables")
    if set(record) != allowed:
        raise _source_error(
            key, "path-mapping records require exactly local_root and remote_root"
        )
    remote_root = record["remote_root"]
    if (
        not isinstance(remote_root, str)
        or remote_root.strip() == ""
        or "\x00" in remote_root
    ):
        raise _source_error(key, "remote_root must be a non-empty path string")
    local_root = _coerce_scalar_path(
        f"{key}.local_root", record["local_root"], base_dir=base_dir
    )
    return KomgaPathMapping(local_root=local_root, remote_root=remote_root)


def _coerce_keyword(key: str, value: object) -> Keyword:
    if isinstance(value, Keyword):
        item = value
    elif isinstance(value, Mapping):
        allowed = {"name", "score", "file_type"}
        if set(value) - allowed or "name" not in value or "score" not in value:
            raise _source_error(key, "keyword records require name and score")
        name = value["name"]
        score = value["score"]
        file_type = value.get("file_type", "both")
        if not isinstance(name, str) or not name:
            raise _source_error(key, "keyword name must be a non-empty string")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise _source_error(key, "keyword score must be numeric")
        try:
            score_number = float(score)
        except (OverflowError, ValueError):
            raise _source_error(key, "keyword score must be finite") from None
        if not math.isfinite(score_number):
            raise _source_error(key, "keyword score must be finite")
        if not isinstance(file_type, str) or file_type not in {"chapter", "volume", "both"}:
            raise _source_error(key, "keyword file_type must be chapter, volume, or both")
        item = Keyword(name=name, score=score_number, file_type=file_type)
    else:
        raise _source_error(key, "keyword records must be tables")
    if not isinstance(item.name, str) or not item.name:
        raise _source_error(key, "keyword name must be a non-empty string")
    if isinstance(item.score, bool) or not isinstance(item.score, (int, float)):
        raise _source_error(key, "keyword score must be numeric")
    try:
        score_number = float(item.score)
    except (OverflowError, ValueError):
        raise _source_error(key, "keyword score must be finite") from None
    if not math.isfinite(score_number):
        raise _source_error(key, "keyword score must be finite")
    if (
        not isinstance(item.file_type, str)
        or item.file_type not in {"chapter", "volume", "both"}
    ):
        raise _source_error(key, "keyword file_type must be chapter, volume, or both")
    return Keyword(name=item.name, score=score_number, file_type=item.file_type)


def _read_toml(path: Path) -> Mapping[str, object]:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file: {exc.__class__.__name__}") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigurationError("configuration file exceeds the 1 MiB limit")
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigurationError("configuration file is not valid UTF-8/TOML") from None
    if not isinstance(parsed, dict):
        raise ConfigurationError("configuration document must be a TOML table")
    return parsed


def _validate_dependencies(values: Mapping[str, object]) -> None:
    def require_nonempty(key: str, reason: str) -> None:
        value = values[key]
        if not isinstance(value, str) or value.strip() == "":
            raise _source_error(key, reason)

    def validate_url(key: str, value: object, *, required: bool = False) -> None:
        if not isinstance(value, str) or value == "":
            if required:
                raise _source_error(key, "enabled integration requires a configured URL")
            return
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            parsed.port
        except ValueError:
            raise _source_error(key, "requires a valid http or https URL") from None
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not host:
            raise _source_error(key, "requires a valid http or https URL")

    for key in ("komga_ip", "new_volume_webhook"):
        validate_url(key, values[key])
    for key in ("discord_webhook_url", "bookwalker_webhook_urls"):
        for endpoint in cast(tuple[str, ...], values[key]):
            validate_url(key, endpoint, required=True)

    if values["move_new_series_to_library_toggle"]:
        raise _source_error(
            "move_new_series_to_library_toggle", "unsupported_policy"
        )

    if values["send_scan_request_to_komga_libraries_toggle"]:
        if not values["check_for_existing_series_toggle"]:
            raise _source_error(
                "send_scan_request_to_komga_libraries_toggle",
                "requires check_for_existing_series_toggle",
            )
        validate_url("komga_ip", values["komga_ip"], required=True)
        require_nonempty("komga_login_email", "enabled integration requires a login email")
        require_nonempty("komga_login_password", "enabled integration requires a login password")

    bookwalker_hooks = cast(tuple[str, ...], values["bookwalker_webhook_urls"])
    if values["bookwalker_check"] and bookwalker_hooks:
        if len(bookwalker_hooks) != 2:
            raise _source_error(
                "bookwalker_webhook_urls",
                "BookWalker notifications require exactly two webhook URLs",
            )

    qbit_enabled = values["uncheck_non_qbit_upgrades_toggle"]
    if qbit_enabled:
        if not values["check_for_existing_series_toggle"]:
            raise _source_error(
                "uncheck_non_qbit_upgrades_toggle",
                "requires check_for_existing_series_toggle",
            )
        require_nonempty("qbittorrent_ip", "enabled integration requires a configured host")
        require_nonempty("qbittorrent_port", "enabled integration requires a configured port")
        require_nonempty(
            "qbittorrent_target_category",
            "enabled integration requires a target category",
        )
        username = values["qbittorrent_username"]
        password = values["qbittorrent_password"]
        if bool(username) != bool(password):
            raise _source_error(
                "qbittorrent_username",
                "username and password must be provided together",
            )

    if values["delete_unacceptable_torrent_titles_in_qbit"]:
        for prerequisite in (
            "uncheck_non_qbit_upgrades_toggle",
            "check_for_existing_series_toggle",
            "delete_unacceptable_files_toggle",
        ):
            if not values[prerequisite]:
                raise _source_error(
                    "delete_unacceptable_torrent_titles_in_qbit",
                    f"requires {prerequisite}",
                )
        require_nonempty("qbittorrent_ip", "enabled integration requires a configured host")
        require_nonempty("qbittorrent_port", "enabled integration requires a configured port")
        require_nonempty(
            "qbittorrent_target_category",
            "enabled integration requires a target category",
        )

    if values["output_chapter_covers_to_discord"] and not values["discord_webhook_url"]:
        raise _source_error(
            "output_chapter_covers_to_discord",
            "requires at least one configured Discord webhook",
        )



def _default_state_dir(environ: Mapping[str, str]) -> Path:
    xdg_state_home = environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        if not isinstance(xdg_state_home, str):
            raise ConfigurationError("XDG_STATE_HOME must be a path string")
        state_root = Path(xdg_state_home)
        if not state_root.is_absolute():
            raise ConfigurationError("XDG_STATE_HOME must be absolute")
    else:
        home_value = environ.get("HOME")
        if home_value:
            if not isinstance(home_value, str):
                raise ConfigurationError("HOME must be a path string")
            home = Path(home_value)
            if not home.is_absolute():
                raise ConfigurationError("HOME must be absolute")
        else:
            home = Path.home()
        state_root = home / ".local" / "state"
    return (state_root / "komga-cover-extractor").absolute()


def _build_configuration(
    values: Mapping[str, object],
    sources: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> Configuration:
    environment = os.environ if environ is None else environ
    working_dir = (
        Path.cwd() if base_dir is None else Path(base_dir)
    ).absolute()
    normalised = dict(_DEFAULT_VALUES)
    normalised["state_dir"] = _default_state_dir(environment)
    normalised.update(values)

    checked: dict[str, object] = {}
    for key in _FIELD_ORDER:
        if key not in normalised:
            raise _source_error(key, "missing configuration value")
        if key == "log_dir" and key not in values:
            state_dir = cast(Path, checked["state_dir"])
            checked[key] = state_dir / "logs"
            continue
        checked[key] = _coerce_value(
            key,
            normalised[key],
            base_dir=working_dir,
            source=sources.get(key, "default"),
            env=False,
        )
    _validate_dependencies(checked)
    provenance = {key: sources.get(key, "default") for key in _FIELD_ORDER}
    constructor_values = dict(checked)
    constructor_values["sources"] = MappingProxyType(provenance)
    return Configuration(**cast(Any, constructor_values))


def validate_config(
    values: Mapping[str, object], *, base_dir: Path | None = None
) -> Configuration:
    """Validate a mapping against defaults and return an immutable snapshot."""

    if not isinstance(values, Mapping):
        raise ConfigurationError("configuration values must be a mapping")
    base = Path.cwd() if base_dir is None else Path(base_dir)
    normalised = _normalise_mapping(
        values,
        base_dir=base.absolute(),
        source="values",
    )
    sources = {key: "values" for key in normalised}
    return _build_configuration(
        normalised, sources, base_dir=base.absolute()
    )


def load_config(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
    cwd: Path | None = None,
) -> Configuration:
    """Load defaults, selected TOML, environment, and explicit overrides.

    Precedence is ``overrides`` (CLI) > ``KCE_*`` environment > TOML >
    defaults.  TOML-relative paths use the TOML file's directory; environment
    and override paths use ``cwd`` (or the process current directory when not
    supplied).  No source performs scans, creates directories, imports
    settings, or constructs a client.
    """

    working_dir = (Path.cwd() if cwd is None else Path(cwd)).absolute()
    merged: dict[str, object] = {}
    provenance: dict[str, str] = {}

    if path is not None:
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = working_dir / config_path
        config_path = config_path.absolute()
        toml_values = _read_toml(config_path)
        parsed_toml = _normalise_mapping(
            toml_values,
            base_dir=config_path.parent,
            source="toml",
        )
        merged.update(parsed_toml)
        provenance.update({key: "toml" for key in parsed_toml})

    env_mapping = os.environ if environ is None else environ
    env_values, env_sources = _collect_environment(
        env_mapping,
        base_dir=working_dir,
    )
    merged.update(env_values)
    provenance.update(env_sources)

    if overrides is not None:
        cli_values = _normalise_mapping(
            overrides,
            base_dir=working_dir,
            source="cli",
            skip_none=True,
        )
        merged.update(cli_values)
        provenance.update({key: "cli" for key in cli_values})

    return _build_configuration(
        merged,
        provenance,
        environ=env_mapping,
        base_dir=working_dir,
    )


__all__ = [
    "Configuration",
    "ConfigurationError",
    "Keyword",
    "KomgaPathMapping",
    "LibraryType",
    "LEGACY_SETTING_NAMES",
    "MAX_CONFIG_BYTES",
    "load_config",
    "validate_config",
]

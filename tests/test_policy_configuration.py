from __future__ import annotations

from collections.abc import Mapping
from operator import setitem
from pathlib import Path

import pytest

from komga_cover_extractor.config import (
    Configuration,
    ConfigurationError,
    load_config,
    validate_config,
)


def _public_record(config: Configuration, key: str) -> Mapping[str, object]:
    serialized = config.as_dict(redact=False)
    records = serialized[key]
    assert isinstance(records, list)
    record = records[0]
    assert isinstance(record, Mapping)
    return record


def test_policy_snapshots_are_isolated_and_regex_order_is_preserved() -> None:
    first = validate_config(
        {
            "cover_patterns": [r"^front", r"^cover", r"^poster"],
            "volume_keywords": ["v1", "vol"],
            "library_types": [
                {
                    "name": "comics",
                    "extensions": [".cbz", ".zip"],
                    "must_contain": ["comic"],
                    "must_not_contain": ["sample"],
                    "match_percentage": 90,
                }
            ],
        }
    )
    second = validate_config(
        {
            "cover_patterns": [r"^poster", r"^front"],
            "volume_keywords": ["volume"],
            "library_types": [],
        }
    )

    assert first.cover_patterns == (r"^front", r"^cover", r"^poster")
    assert second.cover_patterns == (r"^poster", r"^front")
    assert first.volume_keywords == ("v1", "vol")
    assert second.volume_keywords == ("volume",)
    assert dict(_public_record(first, "library_types")) == {
        "name": "comics",
        "extensions": [".cbz", ".zip"],
        "must_contain": ["comic"],
        "must_not_contain": ["sample"],
        "match_percentage": 90,
    }
    assert first.library_types != second.library_types


def test_empty_optional_policy_lists_are_valid(tmp_path: Path) -> None:
    config_file = tmp_path / "empty-lists.toml"
    config_file.write_text(
        'volume_keywords = []\nchapter_keywords = []\n'
        'exclusion_keywords = []\nsubtitle_exclusion_keywords = []\ncover_patterns = []\n',
        encoding="utf-8",
    )

    config = load_config(config_file, environ={})

    assert config.volume_keywords == ()
    assert config.chapter_keywords == ()
    assert config.exclusion_keywords == ()
    assert config.subtitle_exclusion_keywords == ()
    assert config.cover_patterns == ()


def test_invalid_policy_regexes_and_numeric_bounds_are_rejected_from_toml(
    tmp_path: Path,
) -> None:
    invalid_regex = tmp_path / "bad-regex.toml"
    invalid_regex.write_text('cover_patterns = ["["]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="cover_patterns.*invalid regex"):
        load_config(invalid_regex, environ={})

    for filename, body, message in (
        ("zero-limit.toml", "archive_max_entries = 0\n", "positive"),
        ("negative-width.toml", "zfill_volume_int_value = -1\n", "positive"),
        ("bad-ratio.toml", "required_image_similarity_score = 1.01\n", "between 0 and 1"),
        ("bad-percent.toml", "required_matching_percentage = 101\n", "between 0 and 100"),
    ):
        path = tmp_path / filename
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigurationError, match=message):
            load_config(path, environ={})


def test_library_type_records_reject_unknown_and_duplicate_fields_in_toml(
    tmp_path: Path,
) -> None:
    unknown = tmp_path / "unknown-library-field.toml"
    unknown.write_text(
        '[[library_types]]\nname = "comics"\nextensions = [".cbz"]\n'
        'must_contain = []\nmust_not_contain = []\nmatch_percentage = 90\n'
        'guess = true\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_config(unknown, environ={})

    duplicate = tmp_path / "duplicate-library-field.toml"
    duplicate.write_text(
        '[[library_types]]\nname = "comics"\nname = "books"\n'
        'extensions = [".cbz"]\nmust_contain = []\n'
        'must_not_contain = []\nmatch_percentage = 90\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="TOML"):
        load_config(duplicate, environ={})


def test_library_type_record_requires_exact_documented_fields() -> None:
    with pytest.raises(ConfigurationError, match="library_types"):
        validate_config(
            {
                "library_types": [
                    {
                        "name": "comics",
                        "extensions": [".cbz"],
                        "must_contain": [],
                        "must_not_contain": [],
                    }
                ]
            }
        )


def test_state_and_log_defaults_follow_xdg_and_explicit_state(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg-state"
    invocation = tmp_path / "work"
    configured = load_config(
        environ={"XDG_STATE_HOME": str(xdg)}, cwd=invocation
    )
    assert configured.state_dir == xdg / "komga-cover-extractor"
    assert configured.log_dir.is_relative_to(configured.state_dir)

    toml = tmp_path / "config-dir" / "config.toml"
    toml.parent.mkdir()
    toml.write_text('state_dir = "persist"\n', encoding="utf-8")
    relative = load_config(toml, environ={}, cwd=invocation)
    assert relative.state_dir == (toml.parent / "persist").absolute()
    assert relative.log_dir.is_relative_to(relative.state_dir)

    explicit_log = load_config(
        toml,
        environ={},
        overrides={"log_dir": "separate-logs"},
        cwd=invocation,
    )
    assert explicit_log.state_dir == (toml.parent / "persist").absolute()
    assert explicit_log.log_dir == (invocation / "separate-logs").absolute()


def test_scalar_path_sources_use_toml_or_invocation_base(tmp_path: Path) -> None:
    config_file = tmp_path / "settings" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        'state_dir = "toml-state"\nlog_dir = "toml-logs"\n',
        encoding="utf-8",
    )
    invocation = tmp_path / "invocation"

    toml_config = load_config(config_file, environ={}, cwd=invocation)
    assert toml_config.state_dir == (config_file.parent / "toml-state").absolute()
    assert toml_config.log_dir == (config_file.parent / "toml-logs").absolute()

    env_config = load_config(
        config_file,
        environ={"KCE_STATE_DIR": "env-state", "KCE_LOG_DIR": "env-logs"},
        cwd=invocation,
    )
    assert env_config.state_dir == (invocation / "env-state").absolute()
    assert env_config.log_dir == (invocation / "env-logs").absolute()

    cli_config = load_config(
        config_file,
        environ={},
        overrides={"state_dir": "cli-state", "log_dir": "cli-logs"},
        cwd=invocation,
    )
    assert cli_config.state_dir == (invocation / "cli-state").absolute()
    assert cli_config.log_dir == (invocation / "cli-logs").absolute()


def test_regex_pattern_order_and_precedence_follow_config_sources(tmp_path: Path) -> None:
    config_file = tmp_path / "patterns.toml"
    config_file.write_text('cover_patterns = ["toml-first", "toml-second"]\n', encoding="utf-8")
    config = load_config(
        config_file,
        environ={"KCE_COVER_PATTERNS": '["env-first", "env-second"]'},
        overrides={"cover_patterns": ["cli-first", "cli-second"]},
    )

    assert config.cover_patterns == ("cli-first", "cli-second")
    assert config.sources["cover_patterns"] == "cli"


def test_environment_regex_pattern_order_is_preserved() -> None:
    config = load_config(
        environ={"KCE_COVER_PATTERNS": '["first", "second", "third"]'}
    )

    assert config.cover_patterns == ("first", "second", "third")
    assert config.sources["cover_patterns"] == "env"


def test_policy_precedence_and_absent_cli_values(tmp_path: Path) -> None:
    config_file = tmp_path / "policy.toml"
    config_file.write_text(
        "publisher_similarity_score = 0.4\narchive_max_entries = 400\n",
        encoding="utf-8",
    )

    config = load_config(
        config_file,
        environ={"KCE_PUBLISHER_SIMILARITY_SCORE": "0.6", "KCE_ARCHIVE_MAX_ENTRIES": "600"},
        overrides={"publisher_similarity_score": 0.8, "archive_max_entries": None},
    )
    assert config.publisher_similarity_score == pytest.approx(0.8)
    assert config.sources["publisher_similarity_score"] == "cli"
    assert config.archive_max_entries == 600
    assert config.sources["archive_max_entries"] == "env"


def test_nested_library_and_path_mapping_records_are_immutable() -> None:
    config = validate_config(
        {
            "library_types": [
                {
                    "name": "comics",
                    "extensions": [".cbz"],
                    "must_contain": ["comic"],
                    "must_not_contain": ["sample"],
                    "match_percentage": 90,
                }
            ],
            "komga_path_mappings": [
                {"local_root": "/books", "remote_root": "/manga"}
            ],
        }
    )

    library = config.library_types[0]
    mapping = config.komga_path_mappings[0]
    with pytest.raises(AttributeError):
        setattr(library, "name", "different")
    with pytest.raises(AttributeError):
        setattr(mapping, "remote_root", "/elsewhere")
    with pytest.raises(TypeError):
        setitem(library.extensions, 0, ".zip")
    public = config.as_dict(redact=False)
    records = public["library_types"]
    assert isinstance(records, list)
    records[0]["name"] = "copy only"
    assert config.library_types[0].name == "comics"


def test_dormant_policy_and_invocation_authorization_are_not_silent_config() -> None:
    with pytest.raises(ConfigurationError, match="unsupported_policy"):
        validate_config({"move_new_series_to_library_toggle": True})

    for key in ("apply", "allow_remote", "allow_torrent_removal"):
        with pytest.raises(ConfigurationError):
            validate_config({key: True})


def test_step_6_8_9_resource_limits_reject_zero_and_accept_positive_values() -> None:
    resource_limits = (
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
    )
    for key in resource_limits:
        with pytest.raises(ConfigurationError):
            validate_config({key: 0})

    with pytest.raises(ConfigurationError, match="archive_max_ratio"):
        validate_config({"archive_max_ratio": 0})
    accepted = validate_config(
        {
            "archive_max_entries": 1,
            "conversion_timeout_seconds": 1,
            "http_max_attempts": 1,
            "watch_queue_capacity": 1,
            "archive_max_ratio": 0.5,
        }
    )
    assert accepted.archive_max_entries == 1
    assert accepted.conversion_timeout_seconds == 1
    assert accepted.http_max_attempts == 1
    assert accepted.watch_queue_capacity == 1
    assert accepted.archive_max_ratio == pytest.approx(0.5)


def test_remote_mapping_records_are_structured_and_env_can_override(tmp_path: Path) -> None:
    config = load_config(
        environ={
            "KCE_KOMGA_PATH_MAPPINGS": '[{ local_root = "/books", remote_root = "/library" }]',
            "KCE_QBIT_TARGET_STATES": '["pausedDL", "stoppedDL"]',
        },
        cwd=tmp_path,
    )
    mapping = config.komga_path_mappings[0]
    assert mapping.local_root == Path("/books")
    assert mapping.remote_root == "/library"
    assert config.qbit_target_states == ("pausedDL", "stoppedDL")

    with pytest.raises(ConfigurationError, match="komga_path_mappings"):
        validate_config(
            {"komga_path_mappings": [{"local_root": "/books", "remote_root": "/r", "extra": 1}]}
        )



def test_policy_fingerprint_tracks_behavior_not_credentials_or_log_location(tmp_path: Path) -> None:
    baseline = load_config(environ={}, cwd=tmp_path)
    private = load_config(
        environ={}, cwd=tmp_path,
        overrides={
            "state_dir": "another-state", "log_dir": "another-log",
            "komga_login_password": "not-the-same-secret",
            "discord_webhook_url": ["https://example.invalid/secret-hook"],
        },
    )
    changed = load_config(environ={}, cwd=tmp_path, overrides={"required_similarity_score": 0.8})
    assert baseline.policy_fingerprint() == private.policy_fingerprint()
    assert baseline.policy_fingerprint() != changed.policy_fingerprint()
    assert baseline.policy_fingerprint(rule_version=2) != baseline.policy_fingerprint(rule_version=1)


@pytest.mark.parametrize(
    "key,value",
    [
        ("cache_each_root_for_each_path_in_paths_at_beginning_toggle", True),
        ("short_word_filter_percentage", 0.7),
    ],
)
def test_retired_controls_are_rejected_instead_of_accepting_noop_overrides(
    tmp_path: Path, key: str, value: bool | float,
) -> None:
    with pytest.raises(ConfigurationError):
        validate_config({key: value})
    with pytest.raises(ConfigurationError):
        load_config(environ={f"KCE_{key.upper()}": str(value).lower()}, cwd=tmp_path)


@pytest.mark.parametrize("key", [
    "http_connect_timeout_seconds", "http_read_timeout_seconds",
    "bookwalker_request_timeout_seconds",
])
def test_http_timeouts_cannot_exceed_the_transport_budget(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_config({key: 61})
    with pytest.raises(ConfigurationError):
        load_config(environ={f"KCE_{key.upper()}": "61"}, cwd=tmp_path)
    accepted = validate_config({key: 60})
    assert getattr(accepted, key) == 60

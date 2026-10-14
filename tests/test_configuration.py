from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from komga_cover_extractor.config import (
    ConfigurationError,
    load_config,
    validate_config,
)


def test_precedence_and_absent_cli_do_not_override(tmp_path: Path) -> None:
    config_file = tmp_path / "selected.toml"
    config_file.write_text('image_quality = 20\npaths = ["from-toml"]\n', encoding="utf-8")
    cwd = tmp_path / "invocation"

    config = load_config(
        config_file,
        environ={"KCE_IMAGE_QUALITY": "30", "KCE_PATHS": '["from-env"]'},
        overrides={"image_quality": 40, "paths": None},
        cwd=cwd,
    )

    assert config.image_quality == 40
    assert config.paths == ((cwd / "from-env").absolute(),)
    assert config.sources["image_quality"] == "cli"
    assert config.sources["paths"] == "env"


def test_each_source_has_its_own_path_base(tmp_path: Path) -> None:
    config_file = tmp_path / "configs" / "selected.toml"
    config_file.parent.mkdir()
    config_file.write_text('paths = ["library"]\n', encoding="utf-8")
    cwd = tmp_path / "run"

    config = load_config(
        config_file,
        environ={"KCE_DOWNLOAD_FOLDERS": '["downloads"]'},
        overrides={"paths": ["cli-library"]},
        cwd=cwd,
    )

    assert config.paths == ((cwd / "cli-library").absolute(),)
    assert config.download_folders == ((cwd / "downloads").absolute(),)

    toml_only = load_config(config_file, environ={}, cwd=cwd)
    assert toml_only.paths == ((config_file.parent / "library").absolute(),)



@pytest.mark.parametrize("key", ["paths", "download_folders"])
def test_empty_path_entries_are_rejected_before_path_conversion(key: str) -> None:
    with pytest.raises(ConfigurationError, match="must not be empty"):
        validate_config({key: [""]})


@pytest.mark.parametrize("key", ["KCE_PATHS", "KCE_DOWNLOAD_FOLDERS"])
def test_empty_environment_roots_are_rejected(key: str) -> None:
    with pytest.raises(ConfigurationError, match="must not be empty"):
        load_config(environ={key: ""})


def test_unknown_apply_key_and_invalid_values_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration key"):
        validate_config({"apply": True})
    with pytest.raises(ConfigurationError, match="expected boolean"):
        validate_config({"bookwalker_check": "yes"})
    with pytest.raises(ConfigurationError, match="between 1 and 100"):
        validate_config({"image_quality": 101})
    with pytest.raises(ConfigurationError, match="positive integer"):
        validate_config({"watchdog_file_transferred_check_interval": 0})
    with pytest.raises(ConfigurationError, match="invalid regex"):
        validate_config({"exception_keywords": ["["]})
    with pytest.raises(ConfigurationError, match="finite"):
        validate_config({"required_similarity_score": float("inf")})
    with pytest.raises(ConfigurationError, match="file_type"):
        validate_config(
            {"ranked_keywords": [{"name": "volume", "score": 1, "file_type": "book"}]}
        )


def test_alias_conflicts_are_source_local_but_precedence_is_normal(tmp_path: Path) -> None:
    conflict = tmp_path / "conflict.toml"
    conflict.write_text(
        "restructure_when_renaming = false\nresturcture_when_renaming = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="aliases conflict"):
        load_config(conflict, environ={})

    config_file = tmp_path / "legacy.toml"
    config_file.write_text("resturcture_when_renaming = false\n", encoding="utf-8")
    config = load_config(
        config_file,
        environ={"KCE_RESTRUCTURE_WHEN_RENAMING": "true"},
    )
    assert config.restructure_when_renaming is True
    assert config.sources["restructure_when_renaming"] == "env"


def test_configuration_and_nested_values_are_immutable() -> None:
    config = validate_config(
        {
            "paths": ["library"],
            "ranked_keywords": [{"name": "v", "score": 2, "file_type": "volume"}],
        }
    )

    with pytest.raises(FrozenInstanceError):
        config.image_quality = 80  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.paths[0] = Path("other")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        config.ranked_keywords[0].score = 3  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.sources["image_quality"] = "cli"  # type: ignore[index]

    rendered = config.as_dict(redact=False)
    rendered["paths"].append("other")  # type: ignore[union-attr]
    rendered["ranked_keywords"][0]["score"] = 99  # type: ignore[index]
    assert len(config.paths) == 1
    assert config.ranked_keywords[0].score == 2




def test_redaction_covers_credentials_and_webhooks() -> None:
    password = "p@ssword-secret"
    webhook = "https://discord.example/api/webhooks/id/token-secret"
    config = validate_config(
        {
            "komga_ip": "https://user:private@example.test",
            "komga_login_email": "private@example.test",
            "komga_login_password": password,
            "discord_webhook_url": [webhook],
            "bookwalker_webhook_urls": [webhook],
            "new_volume_webhook": webhook,
        }
    )

    assert password not in repr(config)
    assert webhook not in repr(config)
    redacted = config.as_dict()
    assert redacted["komga_login_password"] == "********"
    assert redacted["discord_webhook_url"] == ["********"]
    assert redacted["bookwalker_webhook_urls"] == ["********"]
    assert redacted["new_volume_webhook"] == "********"
    assert config.as_dict(redact=False)["komga_login_password"] == password


def test_torrent_integration_requires_target_category() -> None:

    with pytest.raises(ConfigurationError, match="target category"):
        validate_config(
            {
                "uncheck_non_qbit_upgrades_toggle": True,
                "check_for_existing_series_toggle": True,
                "qbittorrent_ip": "localhost",
                "qbittorrent_port": "8080",
            }
        )


def test_toml_input_is_bounded(tmp_path: Path) -> None:
    config_file = tmp_path / "too-large.toml"
    config_file.write_bytes(b"#" * (1024 * 1024 + 1))
    with pytest.raises(ConfigurationError, match="1 MiB"):
        load_config(config_file, environ={})


@pytest.mark.parametrize("private_key", ["state_dir", "log_dir"])
@pytest.mark.parametrize("media_key", ["paths", "download_folders"])
@pytest.mark.parametrize("relation", ["equal", "ancestor"])
def test_private_directories_cannot_exclude_a_configured_media_root(
    tmp_path: Path, private_key: str, media_key: str, relation: str,
) -> None:
    media = tmp_path / "private" / "media"
    private = media if relation == "equal" else media.parent
    marker = tmp_path / "keep.txt"
    marker.write_bytes(b"Configuration validation must not change this file.")
    values: dict[str, object] = {
        media_key: [str(media)], "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"), private_key: str(private),
    }
    with pytest.raises(ConfigurationError, match=private_key + " must not equal or contain a media root"):
        validate_config(values, base_dir=tmp_path)
    assert set(tmp_path.iterdir()) == {marker}
    assert marker.read_bytes() == b"Configuration validation must not change this file."


@pytest.mark.parametrize("layout", ["children", "unrelated", "prefix_neighbors"])
def test_private_directories_allow_child_exclusions_and_unrelated_paths(tmp_path: Path, layout: str) -> None:
    media = tmp_path / "media"
    if layout == "children":
        state, logs = media / "state", media / "logs"
    elif layout == "unrelated":
        state, logs = tmp_path / "state", tmp_path / "logs"
    else:
        state, logs = tmp_path / "media-old", tmp_path / "media-logs"
    config = validate_config({
        "paths": [str(media)], "state_dir": str(state), "log_dir": str(logs),
    }, base_dir=tmp_path)
    assert config.paths == (media,)
    assert (config.state_dir, config.log_dir) == (state, logs)
    assert tuple(tmp_path.iterdir()) == ()

"""The observed qBittorrent Web API contract, without SDK mutation retries."""
from __future__ import annotations

import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from .http import HttpError

if TYPE_CHECKING:
    from requests import Response

    from .http import HttpClient

_START_API_VERSION = (2, 11, 0)
_FILE_PRIORITIES = frozenset({0, 1, 4, 6, 7})
_HASH = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_VERSION = re.compile(r"2\.([0-9]+)(?:\.([0-9]+))?\Z")


def _text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (not empty and not value):
        raise HttpError("invalid_input", "qbit_invalid_response_schema")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise HttpError("invalid_input", "qbit_invalid_response_schema")
    return value


def _progress(value: object) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 1 or not math.isfinite(value)):
        raise HttpError("invalid_input", "qbit_invalid_progress")
    return float(value)


def _hash(value: object) -> str:
    result = _text(value)
    if _HASH.fullmatch(result) is None:
        raise HttpError("invalid_input", "qbit_invalid_torrent_hash")
    return result.lower()


def _relative_name(value: object) -> str:
    name = _text(value)
    if ("\\" in name or PurePosixPath(name).is_absolute()
            or PureWindowsPath(name).drive
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise HttpError("invalid_input", "qbit_unsafe_relative_file_name")
    return name


def _json(response: Response) -> object:
    invalid = False
    payload: object = None
    try:
        payload = response.json()
    except ValueError:
        invalid = True
    if invalid:
        raise HttpError("invalid_input", "qbit_invalid_json")
    return payload


def validate_torrents(payload: object) -> tuple[dict[str, object], ...]:
    """Keep only validated observation fields; never retain response objects."""
    if not isinstance(payload, list):
        raise HttpError("invalid_input", "qbit_invalid_torrent_list")
    torrents: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise HttpError("invalid_input", "qbit_invalid_torrent_list")
        torrent_hash = _hash(raw.get("hash"))
        if torrent_hash in seen:
            raise HttpError("invalid_input", "qbit_duplicate_torrent_hash")
        seen.add(torrent_hash)
        item: dict[str, object] = {
            "hash": torrent_hash,
            "name": _text(raw.get("name")),
            "category": _text(raw.get("category"), empty=True),
            "state": _text(raw.get("state")),
            "progress": _progress(raw.get("progress")),
            "added_on": None,
            "save_path": _text(raw.get("save_path", ""), empty=True),
            "content_path": _text(raw.get("content_path", ""), empty=True),
            "total_size": None,
        }
        if raw.get("added_on") is not None:
            item["added_on"] = _integer(raw["added_on"])
        if raw.get("total_size") is not None:
            item["total_size"] = _integer(raw["total_size"])
        torrents.append(item)
    return tuple(torrents)


def validate_files(payload: object) -> tuple[dict[str, object], ...]:
    """Old servers omit all indexes; preserve their complete response order."""
    if not isinstance(payload, list):
        raise HttpError("invalid_input", "qbit_invalid_file_list")
    if any(not isinstance(raw, dict) for raw in payload):
        raise HttpError("invalid_input", "qbit_invalid_file_list")
    indexed = ["index" in raw for raw in payload]
    if any(indexed) and not all(indexed):
        raise HttpError("invalid_input", "qbit_partial_file_indexes")
    files: list[dict[str, object]] = []
    indexes: set[int] = set()
    names: set[str] = set()
    for position, raw in enumerate(payload):
        index = _integer(raw.get("index", position))
        name = _relative_name(raw.get("name"))
        size = _integer(raw.get("size"))
        priority = _integer(raw.get("priority"))
        if index in indexes or name in names:
            raise HttpError("invalid_input", "qbit_duplicate_file_identity")
        if priority not in _FILE_PRIORITIES:
            raise HttpError("invalid_input", "qbit_invalid_file_priority")
        progress = _progress(raw.get("progress"))
        indexes.add(index)
        names.add(name)
        files.append({"index": index, "name": name, "size": size,
                      "priority": priority, "progress": progress,
                      "index_from_position": not bool(indexed and indexed[0])})
    return tuple(files)


def parse_webapi_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value.strip())
    if match is None:
        raise HttpError("unsupported", "qbit_unsupported_webapi_version")
    version: tuple[int, int, int] | None = None
    try:
        version = 2, int(match.group(1)), int(match.group(2) or "0")
    except ValueError:
        pass
    if version is None:
        raise HttpError("unsupported", "qbit_unsupported_webapi_version")
    return version


def authenticate(
    client: HttpClient, *, username: str = "", password: str = ""
) -> tuple[int, int, int]:
    """A login body is not authorization proof; callers also read torrents."""
    if bool(username) != bool(password):
        raise HttpError("invalid_input", "qbit_incomplete_credentials")
    if username:
        response = client.request(
            "POST", "api/v2/auth/login", data={"username": username, "password": password},
            read_only=False,
        )
        if response.text != "Ok.":
            raise HttpError("permission_denied", "qbit_authentication_rejected")
    response = client.request("GET", "api/v2/app/webapiVersion", read_only=True)
    return parse_webapi_version(response.text)


def read_torrents(
    client: HttpClient, *, torrent_hash: str | None = None
) -> tuple[dict[str, object], ...]:
    data: dict[str, object] = {}
    if torrent_hash is not None:
        data["hashes"] = _hash(torrent_hash)
    response = client.request("POST", "api/v2/torrents/info", data=data, read_only=True)
    torrents = validate_torrents(_json(response))
    if torrent_hash is not None and any(item["hash"] != torrent_hash.lower() for item in torrents):
        raise HttpError("invalid_input", "qbit_hash_filter_not_respected")
    return torrents


def read_files(client: HttpClient, torrent_hash: str) -> tuple[dict[str, object], ...]:
    response = client.request(
        "POST", "api/v2/torrents/files", data={"hash": _hash(torrent_hash)}, read_only=True,
    )
    return validate_files(_json(response))


def set_file_priority(client: HttpClient, torrent_hash: str, indexes: tuple[int, ...]) -> None:
    if not indexes or len(set(indexes)) != len(indexes):
        raise HttpError("invalid_input", "qbit_invalid_priority_indexes")
    ids = "|".join(str(_integer(index)) for index in indexes)
    client.request("POST", "api/v2/torrents/filePrio",
                   data={"hash": _hash(torrent_hash), "id": ids, "priority": 0}, read_only=False)


def start_torrent(
    client: HttpClient, torrent_hash: str, version: tuple[int, int, int]
) -> None:
    if len(version) != 3 or version[0] != 2 or any(type(value) is not int or value < 0 for value in version):
        raise HttpError("unsupported", "qbit_unsupported_webapi_version")
    method = "start" if version >= _START_API_VERSION else "resume"
    client.request("POST", f"api/v2/torrents/{method}",
                   data={"hashes": _hash(torrent_hash)}, read_only=False)


def remove_torrent(client: HttpClient, torrent_hash: str) -> None:
    client.request("POST", "api/v2/torrents/delete",
                   data={"hashes": _hash(torrent_hash), "deleteFiles": "false"}, read_only=False)

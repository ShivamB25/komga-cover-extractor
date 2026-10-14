"""Komga library observations and authorized, single-attempt scan delivery."""
from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from ..domain.decisions import Decision, RunAuthorization
from ..domain.libraries import PathMapping, map_local_path
from .http import HttpClient, HttpError

_LIBRARY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DRIVE_ROOT = re.compile(r"[A-Za-z]:/")


def komga_base_url(address: str, port: str = "") -> str:
    """Keep an existing base prefix and never append a second port."""
    invalid = False
    try:
        parts = urlsplit(address)
        existing_port = parts.port
        selected_port = int(port) if port else existing_port
    except ValueError:
        invalid = True
        parts = urlsplit("")
        existing_port = selected_port = None
    if (invalid or parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or selected_port is not None and not 1 <= selected_port <= 65535
            or existing_port is not None and port and existing_port != selected_port
            or any(ord(char) < 33 for char in address) or "\\" in address):
        raise HttpError("invalid_input", "invalid_komga_endpoint")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{selected_port}" if selected_port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _library_root(root: object) -> str:
    if not isinstance(root, str) or not root.strip() or any(ord(char) < 32 for char in root):
        raise HttpError("invalid_input", "invalid_komga_library_schema")
    path = root.replace("\\", "/")
    if (not path.startswith("/") and not _DRIVE_ROOT.match(path)
            or any(part in {".", ".."} for part in path.split("/"))):
        raise HttpError("invalid_input", "invalid_komga_library_schema")
    return posixpath.normpath(path)


def get_komga_libraries(client: HttpClient) -> tuple[dict[str, str], ...]:
    response = client.request("GET", "api/v1/libraries", read_only=True, expected_statuses=(200,))
    invalid = False
    try:
        payload = response.json()
    except ValueError:
        invalid = True
        payload = None
    finally:
        response.close()
    if invalid or not isinstance(payload, list):
        raise HttpError("invalid_input", "invalid_komga_library_schema")
    libraries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise HttpError("invalid_input", "invalid_komga_library_schema")
        library_id, name = item.get("id"), item.get("name")
        if (not isinstance(library_id, str) or not _LIBRARY_ID.fullmatch(library_id)
                or library_id in seen or not isinstance(name, str) or not name.strip()):
            raise HttpError("invalid_input", "invalid_komga_library_schema")
        libraries.append({"id": library_id, "name": name, "root": _library_root(item.get("root"))})
        seen.add(library_id)
    return tuple(libraries)


def affected_libraries(
    paths: Iterable[Path], libraries: Sequence[Mapping[str, str]], *, mappings: tuple[PathMapping, ...] = (),
) -> tuple[dict[str, str], ...]:
    """Coalesce affected IDs using explicit longest-prefix local/server mapping."""
    mapped = tuple(
        remote for path in paths
        if (remote := map_local_path(path, mappings) if mappings else path.as_posix()) is not None
    )
    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    for library in libraries:
        root = PurePosixPath(library["root"])
        if library["id"] in seen:
            continue
        if any(PurePosixPath(remote).is_relative_to(root) for remote in mapped):
            matches.append({"id": library["id"], "name": library["name"], "root": library["root"]})
            seen.add(library["id"])
    return tuple(matches)


def scan_komga_library(
    client: HttpClient, library_id: str, *, authorization: RunAuthorization,
) -> Decision:
    if not _LIBRARY_ID.fullmatch(library_id):
        return Decision("invalid_input", "invalid_komga_library_id")
    channel_key = f"komga/{library_id}"
    if not authorization.allow_remote:
        return Decision("planned", "remote_not_authorized", candidates=(channel_key,))
    error = None
    try:
        response = client.request("POST", f"api/v1/libraries/{library_id}/scan", expected_statuses=(202,))
    except HttpError as caught:
        error = caught
    else:
        response.close()
        return Decision("committed", "komga_scan_accepted", candidates=(channel_key,))
    if error.pending_unknown:
        return Decision("review_required", "komga_scan_pending_unknown", candidates=(channel_key,))
    return Decision(error.status, error.reason, candidates=(channel_key,))

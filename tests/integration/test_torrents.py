"""Stateful qBittorrent protocol and journal tests with no network."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from komga_cover_extractor.application.torrents import run_torrents
from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.integrations.http import HttpClient
from komga_cover_extractor.integrations.qbittorrent import (
    authenticate,
    read_torrents,
    set_file_priority,
)
from komga_cover_extractor.matching.ranking import RankingPolicy, RankRule
from komga_cover_extractor.matching.series import MatchingPolicy
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release

requests = pytest.importorskip("requests")

HASH = "a" * 40
PARSING = ParsingPolicy()
MATCHING = MatchingPolicy(match_through_image_similarity=False)
RANKING = RankingPolicy((RankRule("Digital", 2),))


class RawBody(io.BytesIO):
    def __init__(self, content: bytes, cookie: str | None = None) -> None:
        super().__init__(content)
        message = Message()
        if cookie:
            message["Set-Cookie"] = cookie
        self._original_response = SimpleNamespace(msg=message)

    def read1(self, size: int = -1, *, decode_content: bool = True) -> bytes:
        return self.read(size)


class Server(requests.adapters.BaseAdapter):
    def __init__(self, files: list[dict[str, object]], *, torrent_hash: str = HASH,
                 version: str = "2.11.0") -> None:
        self.version = version
        self.torrents: dict[str, dict[str, object]] = {
            torrent_hash: {"hash": torrent_hash, "name": "Series", "category": "books",
                           "state": "pausedDL", "progress": 0.0, "added_on": 100,
                           "save_path": "/remote/downloads", "content_path": "/remote/downloads/Series",
                           "total_size": sum(cast(int, file["size"]) for file in files)},
        }
        self.files = {torrent_hash: copy.deepcopy(files)}
        self.calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.failure: dict[str, str] = {}
        self.before: Callable[[str, dict[str, str]], None] | None = None
        self.require_cookie = False
        self.login_body = "Ok."
        self.fail_confirmation = False
        self.crash_after: str | None = None
        self.persist_path: Path | None = None
        self.closed = False

    def _response(self, request, payload: object, status: int = 200, *, cookie: str | None = None):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers.update({"Content-Length": str(len(body)), "Content-Type": "application/json"})
        response.raw = RawBody(body, cookie)
        return response

    def send(self, request, **kwargs):
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        raw = request.body.decode() if isinstance(request.body, bytes) else request.body or ""
        data = {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        self.calls.append((request.method, urlsplit(request.url).path, data, dict(request.headers)))
        if self.before is not None:
            self.before(endpoint, data)
        if endpoint == "login":
            return self._response(request, self.login_body, cookie="SID=fixture-session; Path=/prefix/")
        if self.require_cookie and request.headers.get("Cookie") != "SID=fixture-session":
            return self._response(request, "unauthorized", 403)
        mode = self.failure.get(endpoint)
        if mode == "denied":
            return self._response(request, "credential-containing server error", 403)
        if mode == "unavailable":
            return self._response(request, "server unavailable", 503)
        if endpoint == "webapiVersion":
            return self._response(request, self.version)
        if endpoint == "info":
            if self.fail_confirmation and data.get("hashes"):
                self.fail_confirmation = False
                return self._response(request, "unavailable confirmation", 403)
            wanted = data.get("hashes")
            return self._response(request, [item for key, item in self.torrents.items() if not wanted or key == wanted])
        if endpoint == "files":
            if data.get("hash") not in self.torrents:
                return self._response(request, "not found", 404)
            return self._response(request, self.files[data["hash"]])
        if endpoint in {"filePrio", "start", "resume", "delete"}:
            if mode != "timeout_no_apply":
                if endpoint == "filePrio":
                    indexes = {int(value) for value in data["id"].split("|")}
                    for position, file in enumerate(self.files[data["hash"]]):
                        if file.get("index", position) in indexes:
                            file["priority"] = int(data["priority"])
                elif endpoint in {"start", "resume"}:
                    self.torrents[data["hashes"]]["state"] = "downloading"
                else:
                    assert data["deleteFiles"] == "false"
                    del self.torrents[data["hashes"]]
            if self.crash_after == endpoint:
                assert self.persist_path is not None
                self.persist_path.write_text(json.dumps({"torrents": self.torrents, "files": self.files}))
                os._exit(86)
            if mode == "timeout_confirmation":
                self.fail_confirmation = True
            if mode in {"timeout_apply", "timeout_no_apply", "timeout_confirmation"}:
                raise requests.Timeout("raw URL /secret/password must not cross the HTTP boundary")
            return self._response(request, "")
        return self._response(request, "unknown endpoint", 404)

    def close(self) -> None:
        self.closed = True

    @property
    def mutations(self) -> list[tuple[str, dict[str, str]]]:
        return [(path.rsplit("/", 1)[-1], data) for _, path, data, _ in self.calls
                if path.rsplit("/", 1)[-1] in {"filePrio", "start", "resume", "delete"}]


def make_files(*names: str, indexed: bool = True) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for index, name in enumerate(names):
        item: dict[str, object] = {"name": name, "priority": 1, "size": 100, "progress": 0.0}
        if indexed:
            item["index"] = index
        files.append(item)
    return files


class MemoryClient:
    def __init__(self, server: Server, cancel: Event | None = None, *,
                 base_url: str = "http://fixture.invalid:8080/prefix/") -> None:
        self.server = server
        self.cancel = cancel
        self.base_url = base_url.rstrip("/")
        self.endpoint_key = hashlib.sha256(self.base_url.encode()).hexdigest()
        self.session = requests.Session()
        self.session.trust_env = False
        self.closed = False

    def request(self, method: str, relative: str, *, data=None, json=None, files=None,
                read_only: bool = False, expected_statuses=(200,)):
        from komga_cover_extractor.integrations.http import HttpError
        if self.cancel is not None and self.cancel.is_set():
            raise HttpError("cancelled", "fixture_cancelled")
        prepared = self.session.prepare_request(requests.Request(
            method, self.base_url + "/" + relative.lstrip("/"), data=data, json=json, files=files,
        ))
        response = None
        transport_failed = False
        try:
            response = self.server.send(prepared)
        except requests.RequestException:
            transport_failed = True
        if transport_failed:
            raise HttpError("delivery_deferred", "fixture_transport_failed", pending_unknown=not read_only)
        assert response is not None
        requests.cookies.extract_cookies_to_jar(self.session.cookies, prepared, response.raw)
        response._content = response.raw.read()
        response._content_consumed = True
        response.close()
        if response.status_code not in expected_statuses:
            if response.status_code in {401, 403}:
                raise HttpError("permission_denied", "fixture_authentication_denied")
            raise HttpError("delivery_deferred", "fixture_service_unavailable", pending_unknown=not read_only)
        return response

    def close(self) -> None:
        self.closed = True
        self.session.close()
        self.server.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


def make_client(server: Server, cancel: Event | None = None, *,
                base_url: str = "http://fixture.invalid:8080/prefix/") -> HttpClient:
    return cast(HttpClient, MemoryClient(server, cancel, base_url=base_url))


def library() -> LibrarySnapshot:
    release = parse_release(FileObservation(Path("/library/Series/Series v01 (Digital).cbz")), PARSING)
    return LibrarySnapshot((release,), (LibraryRoot(Path("/library")),))


def invoke(client: HttpClient, state: StateStore, *, authorization: RunAuthorization = RunAuthorization(),
           options: dict[str, object] | None = None, cancel: Event | None = None,
           provider: Callable[[], LibrarySnapshot] = library, once: bool = True,
           journal_ready: Callable[[], Decision] | None = None, on_ready: Callable[[], None] | None = None,
           policy_version: str = "fixture-policy-v1"):
    def prepare() -> Decision:
        state.initialize()
        return Decision("observed", "fixture_journal_ready")
    return run_torrents(client, provider, parsing=PARSING, matching=MATCHING, ranking=RANKING,
                        options={"qbittorrent_target_category": "books", **(options or {})},
                        authorization=authorization, state=state, journal_ready=journal_ready or prepare,
                        cancel=cancel or Event(), once=once, on_ready=on_ready, policy_version=policy_version)


def test_preview_and_local_apply_never_authorize_remote_effects(tmp_path: Path) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    for authorization in (RunAuthorization(), RunAuthorization(apply=True)):
        with make_client(server) as client:
            result = invoke(client, state, authorization=authorization,
                            journal_ready=lambda: pytest.fail("preview initialized state"))
        assert result.exit_code == 0
        assert result.planned == 2
        assert result.committed == 0
        assert [(item["name"], item["priority"]) for item in server.files[HASH]] == [
            ("Series v01.cbz", 1), ("Series v02.cbz", 1),
        ]
    assert server.mutations == []
    assert not state.state_dir.exists()


@pytest.mark.parametrize(("version", "method"), [("2.8.2", "resume"), ("2.11.0", "start")])
def test_form_endpoints_version_branch_and_verified_action_journal(
    tmp_path: Path, version: str, method: str,
) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"), version=version)
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert result.exit_code == 0
    assert result.committed == result.planned == 2
    assert server.mutations == [
        ("filePrio", {"hash": HASH, "id": "0", "priority": "0"}),
        (method, {"hashes": HASH}),
    ]
    assert server.files[HASH][0]["priority"] == 0
    assert server.files[HASH][1]["priority"] == 1
    assert server.torrents[HASH]["state"] == "downloading"
    jobs = state.list_jobs(kind="qbit_action", limit=10)
    assert len(jobs) == 2
    assert {job.state for job in jobs} == {"complete"}
    assert all(job.kind == "qbit_action" for job in jobs)
    assert {path for _, path, _, _ in server.calls} >= {
        "/prefix/api/v2/app/webapiVersion", "/prefix/api/v2/torrents/info", "/prefix/api/v2/torrents/files",
    }


def test_full_path_and_actual_index_select_only_intended_equal_basename(tmp_path: Path) -> None:
    files = make_files("first/v01.cbz", "second/v01.cbz")
    files[0]["index"] = 4
    files[1]["index"] = 9
    server = Server(files)
    existing = parse_release(
        FileObservation(Path("/library/first/first v01 (Digital).cbz")), PARSING,
    )
    catalog = LibrarySnapshot((existing,), (LibraryRoot(Path("/library")),))
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, provider=lambda: catalog,
                        authorization=RunAuthorization(allow_remote=True))
    assert result.committed == 2
    assert server.mutations == [
        ("filePrio", {"hash": HASH, "id": "4", "priority": "0"}),
        ("start", {"hashes": HASH}),
    ]
    assert [file["priority"] for file in server.files[HASH]] == [0, 1]


@pytest.mark.parametrize("indexed", [True, False])
def test_equal_basenames_are_unchecked_by_distinct_indexes(tmp_path: Path, indexed: bool) -> None:
    files = make_files("first/Series v01.cbz", "second/Series v01.cbz", indexed=indexed)
    if indexed:
        files[0]["index"] = 4
        files[1]["index"] = 9
    server = Server(files)
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert result.committed == 2
    assert server.mutations == [
        ("filePrio", {"hash": HASH, "id": "4" if indexed else "0", "priority": "0"}),
        ("filePrio", {"hash": HASH, "id": "9" if indexed else "1", "priority": "0"}),
    ]
    assert [file["priority"] for file in server.files[HASH]] == [0, 0]
    assert server.torrents[HASH]["state"] == "pausedDL"
    assert result.decisions[-1].reason == "qbit_all_excluded_torrent_retained"


@pytest.mark.parametrize("allow_removal", [False, True])
def test_removal_is_separately_authorized_and_never_deletes_content(tmp_path: Path, allow_removal: bool) -> None:
    server = Server(make_files("Series v01.cbz"))
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True,
                                                                   allow_torrent_removal=allow_removal))
    assert result.exit_code == 0
    assert result.committed == (2 if allow_removal else 1)
    assert server.mutations == [
        ("filePrio", {"hash": HASH, "id": "0", "priority": "0"}),
        *([("delete", {"hashes": HASH, "deleteFiles": "false"})] if allow_removal else []),
    ]
    assert (HASH not in server.torrents) is allow_removal
    assert server.files[HASH][0]["name"] == "Series v01.cbz"


def test_login_uses_cookie_and_durable_payloads_do_not_include_credentials(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    server.require_cookie = True
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True),
                        options={"qbittorrent_username": "private-user", "qbittorrent_password": "private-secret"})
    assert result.committed == 1
    assert server.calls[0][:3] == ("POST", "/prefix/api/v2/auth/login",
                                    {"username": "private-user", "password": "private-secret"})
    assert all(headers.get("Cookie") == "SID=fixture-session" for _, _, _, headers in server.calls[1:])
    serialized = json.dumps([job.payload for job in state.list_jobs(kind="qbit_action", limit=10)])
    assert "private-user" not in serialized
    assert "private-secret" not in serialized
    assert "fixture.invalid" not in serialized
    assert "private-secret" not in json.dumps(result.as_dict())


@pytest.mark.parametrize("failure", ["invalid_login", "denied", "version"])
def test_authentication_or_version_failure_leaves_state_and_remote_unchanged(tmp_path: Path, failure: str) -> None:
    server = Server(make_files("Series v02.cbz"))
    options: dict[str, object] = {}
    if failure == "invalid_login":
        options = {"qbittorrent_username": "fixture-user", "qbittorrent_password": "fixture-password"}
        server.login_body = "Fails."
    elif failure == "denied":
        server.failure["info"] = "denied"
    else:
        server.version = "unknown"
    state = StateStore(tmp_path / "state")
    ready: list[str] = []
    with make_client(server) as client:
        result = invoke(client, state, options=options, authorization=RunAuthorization(allow_remote=True),
                        on_ready=lambda: ready.append("ready"))
    assert result.exit_code != 0
    assert result.committed == 0
    assert server.mutations == []
    assert not state.state_dir.exists()
    assert ready == []


def test_missing_generation_or_library_refuses_even_authorized_actions(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    del server.torrents[HASH]["added_on"]
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert result.decisions[0].reason == "qbit_observation_generation_unavailable"
    assert result.exit_code == 3
    assert not state.state_dir.exists()
    server.torrents[HASH]["added_on"] = 100
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True),
                        provider=lambda: LibrarySnapshot(available=False))
    assert result.exit_code == 2
    assert server.mutations == []


@pytest.mark.parametrize("change", ["category", "state", "path", "old_order", "added_on"])
def test_complete_observation_is_rechecked_before_any_effect(tmp_path: Path, change: str) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz", indexed=change != "old_order"))
    changed = False
    def before(endpoint: str, data: dict[str, str]) -> None:
        nonlocal changed
        if endpoint != "info" or not data.get("hashes") or changed:
            return
        changed = True
        if change == "category":
            server.torrents[HASH]["category"] = "other"
        elif change == "state":
            server.torrents[HASH]["state"] = "downloading"
        elif change == "path":
            server.files[HASH][0]["name"] = "another/Series v01.cbz"
        elif change == "old_order":
            server.files[HASH].reverse()
        else:
            server.torrents[HASH]["added_on"] = 200
    server.before = before
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert result.exit_code == 3
    assert result.decisions[-1].reason == "qbit_observation_changed"
    assert server.mutations == []
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "pending"


def test_unknown_start_is_reconciled_without_network_replay(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    server.failure["start"] = "timeout_no_apply"
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        server.torrents[HASH]["state"] = "downloading"
        third = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert first.exit_code == second.exit_code == 3
    assert first.committed == second.committed == 0
    assert third.exit_code == 0 and third.committed == 1
    assert server.mutations == [("start", {"hashes": HASH})]
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "complete"
    assert "raw URL" not in json.dumps(first.as_dict())


def test_timeout_after_applied_priority_defers_then_finishes_only_pending_start(tmp_path: Path) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    server.failure["filePrio"] = "timeout_confirmation"
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        first_jobs = state.list_jobs(kind="qbit_action", limit=10)
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert first.exit_code == 3 and first.committed == 0
    assert first_jobs[0].state == "needs_review"
    assert first_jobs[0].payload["last_observation"] is None
    assert second.exit_code == 0 and second.committed == 2
    assert server.mutations == [
        ("filePrio", {"hash": HASH, "id": "0", "priority": "0"}),
        ("start", {"hashes": HASH}),
    ]
    assert {job.state for job in state.list_jobs(kind="qbit_action", limit=10)} == {"complete"}


def test_unknown_removal_never_targets_readded_same_hash(tmp_path: Path) -> None:
    files = make_files("Series v01.cbz")
    files[0]["priority"] = 0
    server = Server(files)
    server.failure["delete"] = "timeout_no_apply"
    state = StateStore(tmp_path / "state")
    authorization = RunAuthorization(allow_remote=True, allow_torrent_removal=True)
    with make_client(server) as client:
        first = invoke(client, state, authorization=authorization)
        server.torrents[HASH]["added_on"] = 200
        second = invoke(client, state, authorization=authorization)
    assert first.exit_code == second.exit_code == 3
    assert first.committed == second.committed == 0
    assert second.decisions[0].reason == "qbit_torrent_generation_changed"
    assert server.mutations == [("delete", {"hashes": HASH, "deleteFiles": "false"})]
    assert server.torrents[HASH]["added_on"] == 200
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "needs_review"


def test_unknown_removal_can_settle_only_after_read_proves_absence(tmp_path: Path) -> None:
    files = make_files("Series v01.cbz")
    files[0]["priority"] = 0
    server = Server(files)
    server.failure["delete"] = "timeout_no_apply"
    state = StateStore(tmp_path / "state")
    authorization = RunAuthorization(allow_remote=True, allow_torrent_removal=True)
    with make_client(server) as client:
        first = invoke(client, state, authorization=authorization)
        del server.torrents[HASH]
        second = invoke(client, state, authorization=authorization)
    assert first.exit_code == 3
    assert second.exit_code == 0 and second.committed == 1
    assert server.mutations == [("delete", {"hashes": HASH, "deleteFiles": "false"})]
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "complete"


def test_process_crash_after_dispatch_retains_running_claim_and_cannot_replay(tmp_path: Path) -> None:
    state_path = tmp_path / "state"
    observed = tmp_path / "remote-observation.json"
    script = '''import runpy,sys
from pathlib import Path
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.infrastructure.state import StateStore
scope=runpy.run_path(sys.argv[1])
server=scope['Server'](scope['make_files']('Series v01.cbz','Series v02.cbz'))
server.crash_after='filePrio'
server.persist_path=Path(sys.argv[3])
with scope['make_client'](server) as client:
    scope['invoke'](client,StateStore(Path(sys.argv[2])),authorization=RunAuthorization(allow_remote=True))
'''
    child = subprocess.run([sys.executable, "-I", "-c", script, __file__, str(state_path), str(observed)],
                           capture_output=True, timeout=10, check=False)
    assert child.returncode == 86, child.stderr.decode()
    snapshot = json.loads(observed.read_text())
    assert snapshot["files"][HASH][0]["priority"] == 0
    server = Server(snapshot["files"][HASH])
    server.torrents = snapshot["torrents"]
    state = StateStore(state_path)
    before = state.list_jobs(kind="qbit_action", limit=10)[0]
    with make_client(server) as client:
        recovered = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    after = state.get_job(before.job_id)
    assert recovered.exit_code == 3 and recovered.committed == 0
    assert recovered.decisions[0].reason == "qbit_action_claimed_or_interrupted"
    assert after == before
    assert before.state == "running"
    assert server.mutations == []


def test_failure_to_prepare_journal_prevents_dispatch(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    with make_client(server) as client:
        result = invoke(client, StateStore(tmp_path / "state"), authorization=RunAuthorization(allow_remote=True),
                        journal_ready=lambda: Decision("permission_denied", "fixture_journal_denied"))
    assert result.exit_code == 1
    assert result.decisions[-1].reason == "fixture_journal_denied"
    assert server.mutations == []


class CancelOnWait(Event):
    def __init__(self, stop_after: int = 1) -> None:
        super().__init__()
        self.waits: list[float | None] = []
        self.stop_after = stop_after

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after:
            self.set()
        return self.is_set()


def test_empty_poll_real_readiness_is_once_and_shutdown_closes_transport(tmp_path: Path) -> None:
    server = Server([])
    server.torrents.clear()
    cancel = CancelOnWait(stop_after=2)
    ready: list[str] = []
    catalog_calls: list[str] = []
    def provider() -> LibrarySnapshot:
        catalog_calls.append("observed")
        return LibrarySnapshot(roots=(LibraryRoot(tmp_path / "empty-library"),))
    with make_client(server, cancel) as client:
        result = invoke(client, StateStore(tmp_path / "state"), cancel=cancel, provider=provider,
                        once=False, on_ready=lambda: ready.append("ready"))
    assert ready == ["ready"]
    assert catalog_calls == ["observed", "observed"]
    assert cancel.waits == [30.0, 30.0]
    assert result.decisions[-1].reason == "qbit_cancelled"
    assert server.closed
    assert server.mutations == []
    assert not (tmp_path / "state").exists()


def test_reconnect_wait_is_cancellable_and_does_not_restart_after_stop(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    server.failure["webapiVersion"] = "unavailable"
    cancel = CancelOnWait()
    with make_client(server, cancel) as client:
        result = invoke(client, StateStore(tmp_path / "state"), cancel=cancel, once=False)
    assert cancel.waits == [60.0]
    assert len(server.calls) == 1
    assert result.decisions[-1].reason == "qbit_cancelled"
    assert server.mutations == []


def test_priority_adapter_encodes_pipe_delimited_indexes_without_sdk(tmp_path: Path) -> None:
    server = Server(make_files("one.txt", "two.txt", "three.txt"))
    with make_client(server) as client:
        assert authenticate(client) == (2, 11, 0)
        assert read_torrents(client)[0]["hash"] == HASH
        set_file_priority(client, HASH, (0, 2))
    assert server.mutations == [("filePrio", {"hash": HASH, "id": "0|2", "priority": "0"})]
    assert [file["priority"] for file in server.files[HASH]] == [0, 1, 0]


def test_stable_smallest_file_count_first_including_ties(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz", "Series v03.cbz"))
    for torrent_hash, number in (("b" * 40, "04"), ("c" * 40, "05")):
        server.torrents[torrent_hash] = {**server.torrents[HASH], "hash": torrent_hash, "total_size": 100}
        server.files[torrent_hash] = make_files(f"Series v{number}.cbz")
    with make_client(server) as client:
        result = invoke(client, StateStore(tmp_path / "state"), authorization=RunAuthorization(allow_remote=True))
    assert result.committed == 3
    assert server.mutations == [
        ("start", {"hashes": "b" * 40}),
        ("start", {"hashes": "c" * 40}),
        ("start", {"hashes": HASH}),
    ]


def test_reconciliation_preview_preserves_durable_unknown_job_bytes(tmp_path: Path) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    server.failure["filePrio"] = "timeout_confirmation"
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        before = {path.name: path.read_bytes() for path in state.state_dir.iterdir() if path.is_file()}
        preview = invoke(client, state, journal_ready=lambda: pytest.fail("preview journal write"))
        after = {path.name: path.read_bytes() for path in state.state_dir.iterdir() if path.is_file()}
    assert first.exit_code == preview.exit_code == 3
    assert preview.decisions[0].reason == "qbit_reconciliation_requires_remote_authorization"
    assert before == after
    assert server.mutations == [("filePrio", {"hash": HASH, "id": "0", "priority": "0"})]
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "needs_review"


def test_pending_unknown_action_is_not_replayed_under_new_policy(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    server.failure["start"] = "timeout_no_apply"
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True), policy_version="policy-one")
        server.failure.clear()
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True), policy_version="policy-two")
    assert first.exit_code == second.exit_code == 3
    assert first.committed == second.committed == 0
    jobs = state.list_jobs(kind="qbit_action", limit=10)
    assert len(jobs) == 1
    assert jobs[0].payload["policy_version"] == "policy-one"
    assert jobs[0].state == "needs_review"
    assert server.torrents[HASH]["state"] == "pausedDL"
    assert server.mutations == [("start", {"hashes": HASH})]


def test_completed_action_is_not_blindly_repeated_after_external_pause(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        server.torrents[HASH]["state"] = "pausedDL"
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert first.committed == 1 and first.exit_code == 0
    assert second.committed == 0 and second.exit_code == 3
    assert second.decisions[-1].reason == "qbit_action_already_attempted"
    assert server.mutations == [("start", {"hashes": HASH})]


def test_pending_jobs_are_namespaced_by_endpoint(tmp_path: Path) -> None:
    first_server = Server(make_files("Series v02.cbz"))
    first_server.failure["start"] = "timeout_no_apply"
    state = StateStore(tmp_path / "state")
    with make_client(first_server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    second_server = Server(make_files("Series v02.cbz"))
    with make_client(second_server, base_url="http://second-fixture.invalid:8080/prefix/") as client:
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert first.exit_code == 3 and second.exit_code == 0
    assert second.committed == 1
    assert second_server.mutations == [("start", {"hashes": HASH})]
    assert {job.state for job in state.list_jobs(kind="qbit_action", limit=10)} == {"needs_review", "complete"}


def test_journal_failure_after_one_commit_does_not_lose_partial_counts(tmp_path: Path) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    preparations = 0
    def prepare() -> Decision:
        nonlocal preparations
        preparations += 1
        if preparations > 1:
            return Decision("permission_denied", "fixture_journal_denied")
        state.initialize()
        return Decision("observed", "fixture_journal_ready")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True), journal_ready=prepare)
    assert result.exit_code == 1
    assert result.committed == 1 and result.failed == 1
    assert result.decisions[-1].reason == "fixture_journal_denied"
    assert server.mutations == [("filePrio", {"hash": HASH, "id": "0", "priority": "0"})]
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "complete"


def test_library_change_between_plan_and_dispatch_preserves_priorities(tmp_path: Path) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    observed = 0
    def provider() -> LibrarySnapshot:
        nonlocal observed
        observed += 1
        return library() if observed == 1 else LibrarySnapshot(available=False)
    with make_client(server) as client:
        result = invoke(client, StateStore(tmp_path / "state"), authorization=RunAuthorization(allow_remote=True), provider=provider)
    assert result.exit_code == 3
    assert result.decisions[-1].reason == "qbit_library_decision_changed"
    assert [file["priority"] for file in server.files[HASH]] == [1, 1]
    assert server.mutations == []


def test_cancellation_before_dispatch_allows_a_fresh_authorized_attempt(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    cancel = Event()
    observed = 0
    def provider() -> LibrarySnapshot:
        nonlocal observed
        observed += 1
        if observed == 2:
            cancel.set()
        return library()
    with make_client(server, cancel) as client:
        first = invoke(client, state, cancel=cancel,
                       authorization=RunAuthorization(allow_remote=True), provider=provider)
        pending = state.list_jobs(kind="qbit_action", limit=10)[0]
        assert first.exit_code == 3 and first.committed == 0
        assert first.decisions[-1].reason == "qbit_cancelled"
        assert pending.state == "pending" and pending.error == "qbit_cancelled"
        assert server.torrents[HASH]["state"] == "pausedDL"
        assert server.mutations == []
        cancel.clear()
        preview = invoke(client, state, cancel=cancel, policy_version="policy-two")
        assert preview.exit_code == 0 and preview.planned_count == 1
        assert state.get_job(pending.job_id) == pending
        assert server.mutations == []
        server.torrents[HASH]["progress"] = 0.25
        server.files[HASH][0]["progress"] = 0.5
        second = invoke(client, state, cancel=cancel, authorization=RunAuthorization(allow_remote=True),
                        provider=provider, policy_version="policy-two",
                        options={"qbit_target_states": ("pausedDL",)})
    assert second.exit_code == 0 and second.committed == 1
    assert server.torrents[HASH]["state"] == "downloading"
    assert server.mutations == [("start", {"hashes": HASH})]
    jobs = state.list_jobs(kind="qbit_action", limit=10)
    assert len(jobs) == 1 and jobs[0].job_id == pending.job_id and jobs[0].state == "complete"
    assert jobs[0].payload["policy_version"] == "policy-two"
    assert jobs[0].payload["target_states"] == ["pausedDL"]
    assert cast(dict[str, object], jobs[0].payload["torrent"])["progress"] == 0.25
    assert cast(list[dict[str, object]], jobs[0].payload["files"])[0]["progress"] == 0.5


def test_competing_action_claim_is_not_overwritten_or_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    claim = state.claim_job
    competitor = None
    def compete(job_id: str, kind: str, payload: dict[str, object], *, retry_pending: bool = False) -> bool:
        nonlocal competitor
        claimed = claim(job_id, kind, payload, retry_pending=retry_pending)
        if claimed and competitor is None:
            foreign = copy.deepcopy(payload)
            foreign["action"] = {"kind": "start"}
            key = json.dumps([foreign["endpoint"], foreign["generation"], foreign["action"]],
                             sort_keys=True, separators=(",", ":"))
            foreign_id = "qbit-" + hashlib.sha256(key.encode()).hexdigest()
            assert claim(foreign_id, kind, foreign)
            competitor = state.get_job(foreign_id)
        return claimed
    monkeypatch.setattr(state, "claim_job", compete)
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert result.exit_code == 3 and result.committed == 0
    assert result.decisions[-1].reason == "qbit_other_action_unsettled"
    assert competitor is not None and competitor.state == "running"
    assert state.get_job(competitor.job_id) == competitor
    assert server.mutations == []


@pytest.mark.parametrize("change", ["paused_to_stopped", "torrent_progress", "file_progress", "priority"])
def test_mutable_remote_observation_cannot_silently_change_before_dispatch(tmp_path: Path, change: str) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    def before(endpoint: str, data: dict[str, str]) -> None:
        if endpoint == "info" and data.get("hashes"):
            if change == "paused_to_stopped":
                server.torrents[HASH]["state"] = "stoppedDL"
            elif change == "torrent_progress":
                server.torrents[HASH]["progress"] = 0.1
            elif change == "file_progress":
                server.files[HASH][0]["progress"] = 0.1
            else:
                server.files[HASH][1]["priority"] = 6
    server.before = before
    with make_client(server) as client:
        result = invoke(client, StateStore(tmp_path / "state"), authorization=RunAuthorization(allow_remote=True))
    assert result.exit_code == 3 and result.committed == 0
    assert result.decisions[-1].reason == "qbit_observation_changed"
    assert server.mutations == []


def test_verified_priority_snapshot_is_the_next_actions_expected_progress(tmp_path: Path) -> None:
    server = Server(make_files("Series v01.cbz", "Series v02.cbz"))
    def before(endpoint: str, data: dict[str, str]) -> None:
        if endpoint == "info" and data.get("hashes") and server.files[HASH][0]["priority"] == 0:
            server.torrents[HASH]["progress"] = 0.25
    server.before = before
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert result.exit_code == 0 and result.committed == 2
    assert server.mutations == [
        ("filePrio", {"hash": HASH, "id": "0", "priority": "0"}),
        ("start", {"hashes": HASH}),
    ]
    start_job = next(job for job in state.list_jobs(kind="qbit_action", limit=10)
                     if job.payload["action"] == {"kind": "start"})
    assert start_job.payload["torrent"]["progress"] == 0.25


def test_missing_mutable_progress_refuses_authorized_work(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    del server.files[HASH][0]["progress"]
    with make_client(server) as client:
        result = invoke(client, StateStore(tmp_path / "state"), authorization=RunAuthorization(allow_remote=True))
    assert result.exit_code == 3 and result.committed == 0
    assert result.decisions[-1].reason == "qbit_downloader_progress_unavailable"
    assert server.mutations == []
    assert not (tmp_path / "state").exists()


def test_confirmed_applied_start_is_not_replayed_by_the_service(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    server.failure["start"] = "timeout_apply"
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
    assert first.exit_code == second.exit_code == 0
    assert first.committed == 1 and second.committed == 0
    assert server.mutations == [("start", {"hashes": HASH})]
    assert server.torrents[HASH]["state"] == "downloading"
    assert state.list_jobs(kind="qbit_action", limit=10)[0].state == "complete"


def test_default_empty_library_snapshot_never_authorizes_remote_effects(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    ready: list[str] = []
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        result = invoke(client, state, provider=LibrarySnapshot, authorization=RunAuthorization(allow_remote=True),
                        on_ready=lambda: ready.append("ready"))
    assert result.exit_code == 2 and result.committed == 0
    assert result.decisions[-1].reason == "qbit_library_roots_unavailable"
    assert server.mutations == []
    assert ready == []
    assert not state.state_dir.exists()


def test_metadata_wait_does_not_end_polling_or_dispatch_before_observation(tmp_path: Path) -> None:
    server = Server([])
    polls = 0
    def before(endpoint: str, data: dict[str, str]) -> None:
        nonlocal polls
        if endpoint == "info" and not data.get("hashes"):
            polls += 1
            if polls == 2:
                server.files[HASH] = make_files("Series v02.cbz")
                server.torrents[HASH]["total_size"] = 100
    server.before = before
    cancel = CancelOnWait(stop_after=2)
    ready: list[str] = []
    with make_client(server, cancel) as client:
        result = invoke(client, StateStore(tmp_path / "state"), once=False, cancel=cancel,
                        authorization=RunAuthorization(allow_remote=True), on_ready=lambda: ready.append("ready"))
    assert polls == 2 and ready == ["ready"]
    assert result.committed == 1
    assert server.mutations == [("start", {"hashes": HASH})]
    assert result.decisions[-1].reason == "qbit_cancelled"


@pytest.mark.parametrize("failure", ["info", "files", "webapiVersion", "library"])
def test_readonly_preflight_failure_retries_only_a_fresh_authorized_attempt(tmp_path: Path, failure: str) -> None:
    server = Server(make_files("Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    observations = 0
    def provider() -> LibrarySnapshot:
        nonlocal observations
        observations += 1
        if failure == "library" and observations == 2:
            raise OSError("temporary fixture library outage")
        return library()
    def before(endpoint: str, data: dict[str, str]) -> None:
        if failure != "library" and endpoint == "info" and data.get("hashes"):
            server.failure[failure] = "unavailable"
    server.before = before
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True), provider=provider)
        pending = state.list_jobs(kind="qbit_action", limit=10)[0]
        assert first.committed == 0 and first.exit_code == (2 if failure == "library" else 3)
        assert first.decisions[-1].reason == (
            "qbit_library_unavailable" if failure == "library" else "fixture_service_unavailable"
        )
        assert pending.state == "pending"
        assert server.mutations == [] and server.torrents[HASH]["state"] == "pausedDL"
        server.before = None
        server.failure.clear()
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True),
                        policy_version="fresh-policy")
    assert second.exit_code == 0 and second.committed == 1
    assert server.mutations == [("start", {"hashes": HASH})]
    assert server.torrents[HASH]["state"] == "downloading"
    jobs = state.list_jobs(kind="qbit_action", limit=10)
    assert len(jobs) == 1 and jobs[0].job_id == pending.job_id and jobs[0].state == "complete"
    assert jobs[0].payload["policy_version"] == "fresh-policy"


@pytest.mark.parametrize("change", ["generation", "progress", "library"])
def test_pending_retry_revalidates_inputs_again_after_its_claim(tmp_path: Path, change: str) -> None:
    server = Server(make_files("Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    cancel = Event()
    observations = 0
    def cancel_provider() -> LibrarySnapshot:
        nonlocal observations
        observations += 1
        if observations == 2:
            cancel.set()
        return library()
    with make_client(server, cancel) as client:
        first = invoke(client, state, cancel=cancel, authorization=RunAuthorization(allow_remote=True),
                       provider=cancel_provider)
        pending = state.list_jobs(kind="qbit_action", limit=10)[0]
        assert first.committed == 0 and pending.state == "pending"
        cancel.clear()
        def before(endpoint: str, data: dict[str, str]) -> None:
            if endpoint == "info" and data.get("hashes"):
                if change == "generation":
                    server.torrents[HASH]["added_on"] = 200
                elif change == "progress":
                    server.files[HASH][0]["progress"] = 0.5
        fresh_observations = 0
        def provider() -> LibrarySnapshot:
            nonlocal fresh_observations
            fresh_observations += 1
            return LibrarySnapshot(available=False) if change == "library" and fresh_observations == 2 else library()
        server.before = before
        second = invoke(client, state, cancel=cancel, authorization=RunAuthorization(allow_remote=True),
                        provider=provider, policy_version="fresh-policy")
        assert second.exit_code == 3 and second.committed == 0
        assert second.decisions[-1].reason == (
            "qbit_library_decision_changed" if change == "library" else "qbit_observation_changed"
        )
        assert server.mutations == [] and server.torrents[HASH]["state"] == "pausedDL"
        after = state.get_job(pending.job_id)
        assert after is not None and after.state == "pending"
        server.before = None
        third = invoke(client, state, cancel=cancel, authorization=RunAuthorization(allow_remote=True),
                       policy_version="fresh-policy")
    assert third.exit_code == 0 and third.committed == 1
    assert server.mutations == [("start", {"hashes": HASH})]
    assert server.torrents[HASH]["state"] == "downloading"
    completed = state.list_jobs(("complete",), kind="qbit_action", limit=10)
    assert len(completed) == 1
    if change == "generation":
        assert completed[0].job_id != pending.job_id
        assert cast(dict[str, object], completed[0].payload["torrent"])["added_on"] == 200
    else:
        assert completed[0].job_id == pending.job_id
        if change == "progress":
            assert cast(list[dict[str, object]], completed[0].payload["files"])[0]["progress"] == 0.5


def test_pending_retry_uses_current_policy_instead_of_the_old_start_decision(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    state = StateStore(tmp_path / "state")
    observations = 0
    def provider() -> LibrarySnapshot:
        nonlocal observations
        observations += 1
        if observations == 2:
            raise OSError("temporary fixture library outage")
        return library()
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True), provider=provider)
        pending = state.list_jobs(kind="qbit_action", limit=10)[0]
        assert first.exit_code == 2 and pending.state == "pending"
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True),
                        policy_version="exclude-policy",
                        options={"delete_unacceptable_files_toggle": True, "unacceptable_keywords": ("Series",)})
    assert second.exit_code == 0 and second.committed == 1
    assert server.mutations == [("filePrio", {"hash": HASH, "id": "0", "priority": "0"})]
    assert server.torrents[HASH]["state"] == "pausedDL" and server.files[HASH][0]["priority"] == 0
    assert state.get_job(pending.job_id) == pending
    completed = state.list_jobs(("complete",), kind="qbit_action", limit=10)
    assert len(completed) == 1 and completed[0].payload["policy_version"] == "exclude-policy"
    assert completed[0].payload["action"] == {"kind": "priority", "name": "Series v02.cbz", "index": 0}


def test_two_fresh_retry_claims_dispatch_one_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = Server(make_files("Series v02.cbz"))
    stores = (StateStore(tmp_path / "state"), StateStore(tmp_path / "state"))
    observations = 0
    def provider() -> LibrarySnapshot:
        nonlocal observations
        observations += 1
        if observations == 2:
            raise OSError("temporary fixture library outage")
        return library()
    with make_client(server) as client:
        seed = invoke(client, stores[0], authorization=RunAuthorization(allow_remote=True), provider=provider)
    pending = stores[0].list_jobs(kind="qbit_action", limit=10)[0]
    assert seed.exit_code == 2 and pending.state == "pending" and server.mutations == []
    stores[1].initialize()
    rendezvous = Barrier(2)
    claim = StateStore.claim_job
    def compete(self: StateStore, job_id: str, kind: str, payload: dict[str, object], *,
                retry_pending: bool = False) -> bool:
        if retry_pending:
            rendezvous.wait(timeout=5)
        return claim(self, job_id, kind, payload, retry_pending=retry_pending)
    monkeypatch.setattr(StateStore, "claim_job", compete)
    def attempt(index: int) -> RunResult:
        with make_client(server) as client:
            return invoke(client, stores[index], authorization=RunAuthorization(allow_remote=True),
                          policy_version="fresh-policy",
                          journal_ready=lambda: Decision("observed", "fixture_journal_ready"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(pool.submit(attempt, index) for index in range(2))
        results = tuple(future.result(timeout=10) for future in futures)
    assert sorted(result.committed for result in results) == [0, 1]
    assert sorted(result.exit_code for result in results) == [0, 3]
    assert server.mutations == [("start", {"hashes": HASH})]
    assert server.torrents[HASH]["state"] == "downloading"
    jobs = stores[0].list_jobs(kind="qbit_action", limit=10)
    assert len(jobs) == 1 and jobs[0].job_id == pending.job_id and jobs[0].state == "complete"
    assert jobs[0].payload["policy_version"] == "fresh-policy"


def test_failed_dispatch_is_not_reclassified_as_a_pending_preflight_retry(tmp_path: Path) -> None:
    server = Server(make_files("Series v02.cbz"))
    server.failure["start"] = "denied"
    state = StateStore(tmp_path / "state")
    with make_client(server) as client:
        first = invoke(client, state, authorization=RunAuthorization(allow_remote=True))
        failed = state.list_jobs(kind="qbit_action", limit=10)[0]
        assert first.exit_code == 1 and failed.state == "failed"
        server.failure.clear()
        second = invoke(client, state, authorization=RunAuthorization(allow_remote=True),
                        policy_version="fresh-policy")
    assert second.exit_code == 3 and second.committed == 0
    assert second.decisions[-1].reason == "qbit_action_already_attempted"
    assert state.get_job(failed.job_id) == failed
    assert server.mutations == [("start", {"hashes": HASH})]
    assert server.torrents[HASH]["state"] == "pausedDL"

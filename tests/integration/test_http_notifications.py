from __future__ import annotations

import base64
import gzip
import io
import json
import select
import shutil
import socket
import ssl
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from email import policy
from email.parser import BytesParser
from functools import partial
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from urllib.parse import parse_qs
from zipfile import ZipFile

import pytest

from komga_cover_extractor.application.delivery import settle_committed_deliveries
from komga_cover_extractor.covers.selection import CoverPolicy
from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot, PathMapping
from komga_cover_extractor.domain.operations import (
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.domain.releases import FileObservation, ReleaseSnapshot
from komga_cover_extractor.infrastructure import workers
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
from komga_cover_extractor.integrations import http
from komga_cover_extractor.integrations.discord import (
    DiscordDelivery,
    batch_discord_payloads,
    format_embed,
    group_chapter_notifications,
    prepare_discord_payload,
)
from komga_cover_extractor.integrations.http import HttpClient, HttpError
from komga_cover_extractor.integrations.komga import (
    affected_libraries,
    get_komga_libraries,
    komga_base_url,
    scan_komga_library,
)
from komga_cover_extractor.matching.ranking import RankingPolicy
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release


@dataclass(frozen=True, slots=True)
class WireRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes
    received_at: float


@dataclass(frozen=True, slots=True)
class Reply:
    status: int = 200
    body: bytes = b"{}"
    headers: tuple[tuple[str, str], ...] = ()


class WireHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: WireServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def begin(self, status: int, headers: tuple[tuple[str, str], ...]) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def respond(self, reply: Reply) -> None:
        headers = reply.headers
        names = {name.lower() for name, _ in headers}
        if "content-length" not in names and "transfer-encoding" not in names:
            headers += (("Content-Length", str(len(reply.body))),)
        self.begin(reply.status, headers)
        if any(name.lower() == "transfer-encoding" and value == "chunked" for name, value in headers):
            if reply.body:
                self.chunk(reply.body)
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(reply.body)
        self.wfile.flush()

    def chunk(self, body: bytes) -> None:
        self.wfile.write(f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n")
        self.wfile.flush()

    def dispatch(self) -> None:
        self.connection.settimeout(5)
        length = int(self.headers.get("Content-Length", "0"))
        request = WireRequest(self.command, self.path, {name.lower(): value for name, value in self.headers.items()},
                              self.rfile.read(length), monotonic())
        with self.owner.lock:
            self.owner.calls.append(request)
            step = self.owner.steps.popleft() if self.owner.steps else None
        if step is None:
            self.owner.errors.append("unexpected HTTP request")
            self.respond(Reply(500, b"unexpected request"))
            return
        try:
            if isinstance(step, Reply):
                self.respond(step)
            else:
                step(self)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:
            self.owner.errors.append(type(error).__name__)
        finally:
            self.close_connection = True

    do_GET = dispatch
    do_POST = dispatch


class WireServer:
    def __init__(self, *, tls: ssl.SSLContext | None = None) -> None:
        self.steps: deque[Reply | Callable[[WireHandler], None]] = deque()
        self.calls: list[WireRequest] = []
        self.errors: list[str] = []
        self.lock = Lock()
        self.stopping = Event()
        owner = self

        class Handler(WireHandler):
            pass

        Handler.owner = owner
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        if tls is not None:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.scheme = "https" if tls is not None else "http"
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})

    def __enter__(self) -> WireServer:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "fixture HTTP listener did not stop"
        assert self.errors == [], f"fixture HTTP failures: {self.errors}"

    def url(self, path: str = "") -> str:
        return f"{self.scheme}://127.0.0.1:{self.server.server_port}{path}"

    def reply(self, status: int = 200, body: bytes = b"{}", *, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.steps.append(Reply(status, body, headers))


@pytest.fixture
def wire() -> Iterator[WireServer]:
    with WireServer() as server:
        yield server


@pytest.fixture
def tls_context(tmp_path: Path) -> ssl.SSLContext:
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("offline TLS fixture requires the openssl executable")
    certificate, key = tmp_path / "loopback.pem", tmp_path / "loopback-key.pem"
    subprocess.run(
        [executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
         "-keyout", str(key), "-out", str(certificate)],
        check=True, capture_output=True, timeout=10,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context


def _reset_connection(handler: WireHandler) -> None:
    handler.connection.shutdown(socket.SHUT_RDWR)


def _wait_for_peer_close(handler: WireHandler) -> None:
    while not handler.owner.stopping.is_set():
        readable, _, _ = select.select([handler.connection], [], [], 0.05)
        if readable and not handler.connection.recv(1):
            return


def _cookies(request: WireRequest) -> dict[str, str]:
    parsed: SimpleCookie = SimpleCookie()
    parsed.load(request.headers.get("cookie", ""))
    return {name: morsel.value for name, morsel in parsed.items()}


def _child_pids() -> set[int]:
    children: set[int] = set()
    for path in Path("/proc/self/task").glob("*/children"):
        try:
            children.update(int(value) for value in path.read_text().split())
        except FileNotFoundError:
            continue
    return children


def _image_bytes(image_format: str, color: str) -> bytes:
    from PIL import Image

    with io.BytesIO() as stream, Image.new("RGB", (16, 16), color) as image:
        image.save(stream, format=image_format)
        return stream.getvalue()


def _multipart(request: WireRequest) -> dict[str, object]:
    content_type = request.headers["content-type"]
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + request.body)
    parts: dict[str, object] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        body = part.get_payload(decode=True)
        assert isinstance(name, str) and isinstance(body, bytes)
        parts[name] = json.loads(body) if name == "payload_json" else (part.get_filename(), part.get_content_type(), body)
    return parts


@pytest.fixture
def committed_notice(tmp_path: Path) -> tuple[StateStore, tuple[LibraryRoot, ...], ReleaseSnapshot, RunResult]:
    library = tmp_path / "library"
    series = library / "Series"
    series.mkdir(parents=True)
    source = series / "Series v1.cbz"
    with ZipFile(source, "w") as archive:
        archive.writestr("01.png", _image_bytes("PNG", "red"))
    original = parse_release(FileObservation(source, root_role="library", fingerprint=fingerprint(source, library)), ParsingPolicy())
    destination = series / "Series v01.cbz"
    state = StateStore(tmp_path / "state")
    plan = OperationPlan("http-delivery-tests-v1", "notice-move",
                         (Operation("book", "move", destination, source=original.observation.fingerprint, reason="organization"),),
                         (RootPermission(library),))
    result = TransactionExecutor(state.state_dir, (library,)).apply(plan, authorization=RunAuthorization(apply=True))
    assert (result.committed, result.failed) == (1, 0)
    published = parse_release(FileObservation(destination, root_role="library", fingerprint=fingerprint(destination, library)), ParsingPolicy())
    return state, (LibraryRoot(library),), original, replace(result, releases=(published,))


def test_bootstrap_commit_callback_sends_package_version_on_actual_discord_payloads(
    tmp_path: Path, wire: WireServer,
) -> None:
    from komga_cover_extractor import __version__
    from komga_cover_extractor.application.pipeline import RunRequest
    from komga_cover_extractor.bootstrap import open_pipeline
    from komga_cover_extractor.config import validate_config

    library, downloads = tmp_path / "library", tmp_path / "downloads"
    existing, source = library / "Series" / "Series v01.cbz", downloads / "Series" / "Series v02.cbz"
    for path in (existing, source):
        path.parent.mkdir(parents=True)
        with ZipFile(path, "w") as archive:
            archive.writestr("01.png", _image_bytes("PNG", "red"))
            archive.writestr("cover.png", _image_bytes("PNG", "blue"))
    original = source.read_bytes()
    config = validate_config({
        "paths": [str(library)], "download_folders": [str(downloads)],
        "state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs"),
        "manual_rename": False, "manual_delete": False, "log_to_file": False,
        "generate_release_group_list_toggle": False, "extract_covers_toggle": False,
        "compare_detected_cover_to_blank_images": False, "output_covers_as_webp": True,
        "match_through_image_similarity": False, "bookwalker_check": False,
        "check_for_existing_series_toggle": True,
        "send_scan_request_to_komga_libraries_toggle": False,
        "discord_webhook_url": [wire.url("/api/webhooks/1/fixture-normal")],
        "new_volume_webhook": wire.url("/api/webhooks/2/fixture-new-release"),
    }, base_dir=tmp_path)
    wire.reply(body=b'{"id":"123"}')
    wire.reply(body=b'{"id":"124"}')
    request = RunRequest("organize", authorization=RunAuthorization(apply=True, allow_remote=True))
    with open_pipeline(config) as pipeline:
        result = pipeline.run(request)
        repeated = pipeline.run(request)
    assert (result.committed, result.failed, result.deferred_deliveries, result.exit_code) == (1, 0, (), 0)
    assert (repeated.committed, repeated.failed, repeated.deferred_deliveries, repeated.exit_code) == (0, 0, (), 0)
    assert sorted((call.method, call.target) for call in wire.calls) == [
        ("POST", "/api/webhooks/1/fixture-normal?wait=true"),
        ("POST", "/api/webhooks/2/fixture-new-release?wait=true"),
    ]
    by_target = {call.target: call for call in wire.calls}
    normal = json.loads(by_target["/api/webhooks/1/fixture-normal?wait=true"].body)
    added = _multipart(by_target["/api/webhooks/2/fixture-new-release?wait=true"])["payload_json"]
    embeds = [*normal["embeds"], *added["embeds"]]
    assert [embed["title"] for embed in embeds] == [
        "Found Series Match", "Moved File", "Moved Folder", "New Volume(s) Added",
    ]
    assert [embed["footer"] for embed in embeds] == [{"text": f"v{__version__}"}] * 4
    assert added["embeds"][0]["fields"] == [
        {"name": "Volume Number(s)", "value": "```2```", "inline": False},
        {"name": "Volume Name(s)", "value": "```Series v02.cbz```", "inline": False},
    ]
    assert (library / "Series" / "Series v02.cbz").read_bytes() == original
    assert not source.exists()


def test_bootstrap_release_callback_sends_package_version_without_a_formatter_stub(
    tmp_path: Path, wire: WireServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komga_cover_extractor import __version__
    from komga_cover_extractor.application.pipeline import RunRequest
    from komga_cover_extractor.bootstrap import open_pipeline
    from komga_cover_extractor.config import validate_config
    from komga_cover_extractor.integrations import bookwalker

    library = tmp_path / "library"
    source = library / "Series" / "Series v01.cbz"
    source.parent.mkdir(parents=True)
    with ZipFile(source, "w") as archive:
        archive.writestr("01.png", _image_bytes("PNG", "red"))
    original = source.read_bytes()
    config = validate_config({
        "paths": [str(library)], "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"), "log_to_file": False,
        "bookwalker_check": True, "bookwalker_request_interval_seconds": 1,
        "bookwalker_webhook_urls": [wire.url("/api/webhooks/3/fixture-released"),
                                   wire.url("/api/webhooks/4/fixture-preorder")],
        "send_scan_request_to_komga_libraries_toggle": False,
    }, base_dir=tmp_path)
    monkeypatch.setattr(bookwalker, "BOOKWALKER_ORIGIN", wire.url("/store/"))
    series = b"<ul class='o-tile-list'><li class='o-tile'></li></ul>"
    products = (
        b"<ul class='o-tile-list'><li class='o-tile'><div class='o-tile-book-info'>"
        b"<a class='a-tile-thumb-img' href='https://global.bookwalker.jp/book-2/'></a>"
        b"<div class='a-tag-manga'>Manga</div><h2 class='a-tile-ttl'>Series Volume 2</h2>"
        b"</div></li></ul>"
    )
    detail = (
        b"<div class='product-detail-inner'><table class='product-detail'>"
        b"<tr><th>Series Title</th><td>Series</td></tr>"
        b"<tr><th>Release Date</th><td>October 13, 2020</td></tr></table></div>"
    )
    for body in (series, products, detail):
        wire.reply(body=body, headers=(("Content-Type", "text/html; charset=utf-8"),))
    wire.reply(body=b'{"id":"125"}')
    for body in (series, products, detail):
        wire.reply(body=body, headers=(("Content-Type", "text/html; charset=utf-8"),))
    request = RunRequest("releases", authorization=RunAuthorization(allow_remote=True))
    with open_pipeline(config) as pipeline:
        result = pipeline.run(request)
        repeated = pipeline.run(request)
    assert (result.committed, result.failed, result.deferred_deliveries, result.exit_code) == (0, 0, (), 0)
    assert (repeated.committed, repeated.failed, repeated.deferred_deliveries, repeated.exit_code) == (0, 0, (), 0)
    assert [(call.method, call.target) for call in wire.calls] == [
        ("GET", "/store/search/?word=Series&np=0"),
        ("GET", "/store/search/?word=Series&page=1&qcat=2"),
        ("GET", "/store/book-2/"),
        ("POST", "/api/webhooks/3/fixture-released?wait=true"),
        ("GET", "/store/search/?word=Series&np=0"),
        ("GET", "/store/search/?word=Series&page=1&qcat=2"),
        ("GET", "/store/book-2/"),
    ]
    payload = json.loads(wire.calls[3].body)
    embed, = payload["embeds"]
    assert (embed["title"], embed["footer"], embed["fields"]) == (
        "Series Volume 2", {"text": f"v{__version__}"},
        [{"name": "Type", "value": "manga", "inline": False},
         {"name": "Release Date", "value": "2020-10-13", "inline": False}],
    )
    assert source.read_bytes() == original


def test_request_preserves_prefix_and_explicit_auth_without_proxy_or_netrc_bleed(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    with WireServer() as proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, proxy.url())
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "untrusted-certificate"))
        netrc = tmp_path / "netrc"
        netrc.write_text("machine 127.0.0.1 login unwanted password private-netrc-password\n")
        monkeypatch.setenv("NETRC", str(netrc))
        wire.reply(body=b'{"result":"ok"}')
        with HttpClient(wire.url("/proxy/komga/"), auth=("reader", "fictional-password")) as client:
            assert client.request("GET", "/api/v1/libraries?active=true", read_only=True).json() == {"result": "ok"}
        assert [(call.method, call.target, call.body) for call in wire.calls] == [
            ("GET", "/proxy/komga/api/v1/libraries?active=true", b""),
        ]
        assert base64.b64decode(wire.calls[0].headers["authorization"].removeprefix("Basic ")) == b"reader:fictional-password"
        assert proxy.calls == []
        with pytest.raises(HttpError) as caught:
            client.request("GET", "api/v1/libraries", read_only=True)
        assert (caught.value.status, caught.value.reason) == ("invalid_input", "http_client_closed")
        assert len(wire.calls) == 1


def test_untrusted_tls_certificate_is_refused_on_the_actual_wire(
    wire: WireServer, tls_context: ssl.SSLContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    wire.reply(body=b"permitted HTTP fixture")
    with HttpClient(wire.url(), max_attempts=1) as client:
        assert client.request("GET", "health", read_only=True).content == b"permitted HTTP fixture"
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "loopback.pem"))
    with WireServer(tls=tls_context) as secure:
        with HttpClient(secure.url(), max_attempts=1, connect_timeout=1, read_timeout=1) as client:
            with pytest.raises(HttpError) as caught:
                client.request("GET", "health", read_only=True)
        assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("permission_denied", "http_tls_failed", False)
        assert secure.calls == []
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/health")]


def test_komga_basic_auth_preserves_original_utf8_credentials(wire: WireServer) -> None:
    wire.reply(body=b"[]")
    with HttpClient(wire.url("/komga"), auth=("reader", "日本語-password")) as client:
        assert get_komga_libraries(client) == ()
    assert (wire.calls[0].method, wire.calls[0].target, wire.calls[0].body) == ("GET", "/komga/api/v1/libraries", b"")
    assert base64.b64decode(wire.calls[0].headers["authorization"].removeprefix("Basic ")) == "reader:日本語-password".encode()


def test_qbit_login_cookie_survives_without_request_body_or_header_bleed(wire: WireServer) -> None:
    wire.reply(body=b"Ok.", headers=(("Set-Cookie", "SID=fictional-session; Path=/proxy/; HttpOnly"),))
    wire.reply(body=b'[{"hash":"observed"}]')
    with HttpClient(wire.url("/proxy")) as client:
        assert client.request("POST", "api/v2/auth/login", data={"username": "reader", "password": "fictional-password"}).text == "Ok."
        assert client.request("POST", "api/v2/torrents/info", data={"category": "manga"}, read_only=True).json() == [{"hash": "observed"}]
    assert [(call.method, call.target) for call in wire.calls] == [
        ("POST", "/proxy/api/v2/auth/login"), ("POST", "/proxy/api/v2/torrents/info"),
    ]
    assert parse_qs(wire.calls[0].body.decode()) == {"username": ["reader"], "password": ["fictional-password"]}
    assert parse_qs(wire.calls[1].body.decode()) == {"category": ["manga"]}
    assert _cookies(wire.calls[0]) == {}
    assert _cookies(wire.calls[1]) == {"SID": "fictional-session"}
    assert "authorization" not in wire.calls[1].headers


def test_safe_cookie_and_browser_headers_are_scoped(wire: WireServer) -> None:
    wire.reply(body=b"<html>permitted fixture</html>")
    wire.reply(body=b"separate session")
    with HttpClient(wire.url("/store")) as client:
        client.set_header("User-Agent", "fixture-reader/1")
        client.set_header("Accept-Language", "en-US")
        client.set_cookie("safeSearch", "false")
        assert client.request("GET", "search/?qcat=3", read_only=True).text == "<html>permitted fixture</html>"
        with pytest.raises(HttpError) as caught:
            client.set_header("Authorization", "fictional-secret")
        assert (caught.value.status, caught.value.reason) == ("invalid_input", "unsafe_http_header")
        with pytest.raises(HttpError) as caught:
            client.set_cookie("sid", "unsafe\r\nheader")
        assert (caught.value.status, caught.value.reason) == ("invalid_input", "unsafe_http_cookie")
    with HttpClient(wire.url("/store")) as other:
        assert other.request("GET", "search/?qcat=2", read_only=True).text == "separate session"
    assert (wire.calls[0].method, wire.calls[0].target, _cookies(wire.calls[0])) == ("GET", "/store/search/?qcat=3", {"safeSearch": "false"})
    assert wire.calls[0].headers["user-agent"] == "fixture-reader/1"
    assert wire.calls[0].headers["accept-language"] == "en-US"
    assert _cookies(wire.calls[1]) == {}
    assert "accept-language" not in wire.calls[1].headers


def test_server_cookie_replacement_expiry_and_deletion_preserve_safe_search(wire: WireServer) -> None:
    wire.reply(body=b"initial", headers=(
        ("Set-Cookie", "SID=first-session; Path=/proxy/; HttpOnly"),
        ("Set-Cookie", "temporary=short-session; Max-Age=1; Path=/proxy/"),
        ("Set-Cookie", "expired=private-expired; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/proxy/"),
        ("Set-Cookie", "narrow=only-library; Path=/proxy/libraries/"),
    ))
    wire.reply(body=b"replaced", headers=(("Set-Cookie", "SID=second-session; Path=/proxy/; HttpOnly"),))
    wire.reply(body=b"expired", headers=(("Set-Cookie", "SID=deleted-value; Max-Age=0; Path=/proxy/"),))
    wire.reply(body=b"deleted")
    with HttpClient(wire.url("/proxy")) as client:
        client.set_cookie("safeSearch", "false")
        assert client.request("GET", "login", read_only=True).text == "initial"
        assert client.request("GET", "libraries/info", read_only=True).text == "replaced"
        sleep(1.2)
        assert client.request("GET", "torrents/info", read_only=True).text == "expired"
        assert client.request("GET", "torrents/info", read_only=True).text == "deleted"
    assert [_cookies(call) for call in wire.calls] == [
        {"safeSearch": "false"},
        {"safeSearch": "false", "SID": "first-session", "temporary": "short-session", "narrow": "only-library"},
        {"safeSearch": "false", "SID": "second-session"},
        {"safeSearch": "false"},
    ]


@pytest.mark.parametrize("bound", ["bytes", "count"])
def test_server_cookie_overflow_is_typed_redacted_and_keeps_required_session(
    wire: WireServer, bound: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "private-response-cookie"
    values = [(f"c{index:03d}", f"{index:03d}-{secret}".ljust(1024, "x")) for index in range(15)] if bound == "bytes" else [
        (f"c{index:03d}", f"{index:03d}-{secret}") for index in range(63)
    ]
    overflow = secret.ljust(1024, "x") if bound == "bytes" else secret
    wire.reply(body=b"logged in", headers=(("Set-Cookie", "SID=requiredCookie; Path=/proxy/"),))
    wire.reply(body=b"bounded cookie jar", headers=tuple(("Set-Cookie", f"{name}={value}; Path=/proxy/") for name, value in values))
    wire.reply(body=b"oversized cookie jar", headers=(("Set-Cookie", f"overflow={overflow}; Path=/proxy/"),))
    wire.reply(body=b"session retained")
    with HttpClient(wire.url("/proxy")) as client:
        assert client.request("POST", "login").content == b"logged in"
        assert client.request("GET", "cookie-bound", read_only=True).content == b"bounded cookie jar"
        with pytest.raises(HttpError) as caught:
            client.request("GET", "cookie-overflow", read_only=True)
        assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("invalid_input", "http_cookie_limit", False)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert client.request("GET", "session", read_only=True).content == b"session retained"
    assert [(call.method, call.target) for call in wire.calls] == [
        ("POST", "/proxy/login"), ("GET", "/proxy/cookie-bound"), ("GET", "/proxy/cookie-overflow"), ("GET", "/proxy/session"),
    ]
    retained = {"SID": "requiredCookie", **dict(values)}
    assert [_cookies(call) for call in wire.calls] == [{}, {"SID": "requiredCookie"}, retained, retained]
    captured = capsys.readouterr()
    assert secret not in repr(caught.value) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("bound", ["bytes", "count"])
def test_outgoing_cookie_limit_preserves_the_last_valid_cookie_set(wire: WireServer, bound: str) -> None:
    values = [(f"c{index:03d}", f"session-{index:03d}".ljust(1024, "x")) for index in range(15)] if bound == "bytes" else [
        (f"c{index:03d}", f"session-{index:03d}") for index in range(63)
    ]
    wire.reply(body=b"bounded cookies accepted")
    wire.reply(body=b"bounded cookies retained")
    with HttpClient(wire.url("/proxy")) as client:
        client.set_cookie("SID", "requiredCookie")
        for name, value in values:
            client.set_cookie(name, value)
        assert client.request("GET", "before", read_only=True).content == b"bounded cookies accepted"
        with pytest.raises(HttpError) as caught:
            client.set_cookie("overflow", "one-more-session".ljust(1024, "x") if bound == "bytes" else "one-more-session")
        assert (caught.value.status, caught.value.reason) == ("invalid_input", "http_cookie_limit")
        assert client.request("GET", "after", read_only=True).content == b"bounded cookies retained"
    expected = {"SID": "requiredCookie", **dict(values)}
    assert [_cookies(call) for call in wire.calls] == [expected, expected]
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/proxy/before"), ("GET", "/proxy/after")]
    assert all(len(call.headers["cookie"].encode()) <= 16 * 1024 for call in wire.calls)


@pytest.mark.parametrize("relative", [
    "https://other.invalid/private", "//other.invalid/private", "../private", "%2e%2e/private",
    "%252e%252e/private", "safe%2f..%2fprivate", "safe\\private", "api\x00private", "api#fragment",
])
def test_unsafe_relative_paths_fail_before_wire_request(wire: WireServer, relative: str) -> None:
    wire.reply(body=b"authorized path")
    with HttpClient(wire.url("/prefix")) as client:
        with pytest.raises(HttpError) as caught:
            client.request("GET", relative, read_only=True)
        assert (caught.value.status, caught.value.reason) == ("invalid_input", "unsafe_http_path")
        assert wire.calls == []
        assert client.request("GET", "api", read_only=True).content == b"authorized path"
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/prefix/api")]


def test_read_retries_are_three_actual_attempts_with_one_and_two_second_backoff(wire: WireServer) -> None:
    wire.steps.append(_reset_connection)
    wire.reply(503, b"private diagnostic")
    wire.reply(body=b'{"accepted":true}')
    with HttpClient(wire.url("/service")) as client:
        assert client.request("POST", "api/read-only", data={"category": "manga"}, read_only=True).json() == {"accepted": True}
    assert [(call.method, call.target, parse_qs(call.body.decode())) for call in wire.calls] == [
        ("POST", "/service/api/read-only", {"category": ["manga"]}),
        ("POST", "/service/api/read-only", {"category": ["manga"]}),
        ("POST", "/service/api/read-only", {"category": ["manga"]}),
    ]
    assert wire.calls[1].received_at - wire.calls[0].received_at >= 0.9
    assert wire.calls[2].received_at - wire.calls[1].received_at >= 1.9


def test_exhausted_read_retries_are_a_failure_not_an_empty_library(wire: WireServer) -> None:
    for _ in range(3):
        wire.reply(503, b"private diagnostic")
    with HttpClient(wire.url("/komga")) as client:
        with pytest.raises(HttpError) as caught:
            get_komga_libraries(client)
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("delivery_deferred", "http_503", False)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/komga/api/v1/libraries")] * 3


@pytest.mark.skipif(sys.platform != "linux", reason="HTTP worker ownership uses Linux process supervision")
@pytest.mark.parametrize(("failure", "status", "reason"), [
    ("request-quota", "invalid_input", "http_worker_failed"),
    ("prestart-cancellation", "cancelled", "http_cancelled"),
    ("launch-failure", "failed", "http_worker_failed"),
    ("prestart-callback-error", "failed", "http_worker_failed"),
])
def test_valid_mutation_rejected_before_worker_start_is_definitely_unsent(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    failure: str, status: str, reason: str,
) -> None:
    checks = 0
    prior = _child_pids()

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            return False
        if failure == "prestart-callback-error":
            raise RuntimeError("private cancellation callback failure")
        return True

    with monkeypatch.context() as patch:
        cancellation = cancel if failure in {"prestart-cancellation", "prestart-callback-error"} else None
        if failure == "request-quota":
            patch.setattr(workers, "JOB_REQUEST_LIMITS", {**workers.JOB_REQUEST_LIMITS, "http": 128})
        elif failure == "launch-failure":
            patch.setattr(workers.sys, "executable", str(tmp_path / "missing-worker-executable"))
        with HttpClient(wire.url("/send"), cancel=cancellation) as client:
            with pytest.raises(HttpError) as caught:
                client.request("POST", "", json={"content": "Committed book"})
        assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == (status, reason, False)
        assert wire.calls == []
        assert _child_pids() == prior
    wire.reply(body=b'{"accepted":true}')
    with HttpClient(wire.url("/send")) as client:
        response = client.request("POST", "", json={"content": "Committed book"})
        assert response.json() == {"accepted": True}
        response.close()
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/send", {"content": "Committed book"}),
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="HTTP worker ownership uses Linux process supervision")
def test_poststart_cancellation_without_child_confirmation_remains_unknown(wire: WireServer) -> None:
    checks = 0
    prior = _child_pids()

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with HttpClient(wire.url(), cancel=cancel) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "send", json={"content": "Committed book"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("cancelled", "http_cancelled", True)
    assert wire.calls == []
    assert _child_pids() == prior


@pytest.mark.parametrize(("failure", "reason"), [
    ("disconnect", "http_transport_failed"), ("read-stall", "http_transport_failed"), ("503", "http_503"),
])
def test_mutations_never_retry_ambiguous_delivery_or_leak_server_diagnostics(
    wire: WireServer, failure: str, reason: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    if failure == "disconnect":
        wire.steps.append(_reset_connection)
    elif failure == "read-stall":
        wire.steps.append(_wait_for_peer_close)
    else:
        wire.reply(503, b"fictional-password private response diagnostic")
    with HttpClient(wire.url("/private-token"), read_timeout=0.3) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "", json={"book": "committed"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("delivery_deferred", reason, True)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/private-token", {"book": "committed"}),
    ]
    captured = capsys.readouterr()
    diagnostic = repr(caught.value) + caplog.text + captured.out + captured.err
    assert "fictional-password" not in diagnostic and "private-token" not in diagnostic and "private response" not in diagnostic


@pytest.mark.parametrize("status", [401, 403])
def test_auth_denial_is_permanent_and_not_an_empty_result(wire: WireServer, status: int) -> None:
    wire.reply(status, b"fictional-password private response")
    with HttpClient(wire.url()) as client:
        with pytest.raises(HttpError) as caught:
            get_komga_libraries(client)
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("permission_denied", f"http_{status}", False)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api/v1/libraries")]
    assert "fictional-password" not in repr(caught.value)


@pytest.mark.parametrize("source", ["header", "json"])
def test_explicit_rate_limit_rejection_reschedules_once_and_honors_delay(wire: WireServer, source: str) -> None:
    wire.reply(429, b'{"retry_after":0.2}' if source == "json" else b"rejected", headers=(("Retry-After", "0.2"),) if source == "header" else ())
    wire.reply(body=b'{"id":"123"}')
    with HttpClient(wire.url()) as client:
        assert client.request("POST", "send", json={"content": "committed"}).json() == {"id": "123"}
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/send", {"content": "committed"}), ("POST", "/send", {"content": "committed"}),
    ]
    assert wire.calls[1].received_at - wire.calls[0].received_at >= 0.19


def test_rate_limit_delay_beyond_budget_does_not_replay_the_rejected_post(wire: WireServer) -> None:
    wire.reply(429, b"private diagnostic", headers=(("Retry-After", "61"),))
    with HttpClient(wire.url()) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "send", json={"content": "committed"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("delivery_deferred", "http_retry_budget", False)
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [("POST", "/send", {"content": "committed"})]


def test_read_backoff_is_cancellable_after_the_actual_failed_request(wire: WireServer) -> None:
    wire.reply(503, b"rejected")
    started = monotonic()

    def cancel() -> bool:
        return bool(wire.calls) and monotonic() >= wire.calls[0].received_at + 0.2

    with HttpClient(wire.url(), cancel=cancel) as client:
        with pytest.raises(HttpError) as caught:
            client.request("GET", "api", read_only=True)
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("cancelled", "http_cancelled", False)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api")]
    assert monotonic() - started < 4


@pytest.mark.skipif(sys.platform != "linux", reason="HTTP worker ownership uses Linux process supervision")
@pytest.mark.parametrize(("cancellation", "status", "reason"), [
    ("event", "cancelled", "http_cancelled"),
    ("callback", "cancelled", "http_cancelled"),
    ("callback-error", "failed", "http_worker_failed"),
])
def test_cancellation_after_post_with_no_decoded_gzip_output_kills_and_reaps_transport(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, cancellation: str, status: str, reason: str,
) -> None:
    monkeypatch.setattr(http, "HTTP_DELIVERY_BUDGET_SECONDS", 2.0)
    cancel_event = Event()
    progress: list[float] = []
    pids: set[int] = set()
    prior = _child_pids()
    stopped = Event()

    def stream(handler: WireHandler) -> None:
        pids.update(_child_pids() - prior)
        handler.begin(200, (("Content-Encoding", "gzip"), ("Transfer-Encoding", "chunked")))
        try:
            while not handler.owner.stopping.is_set():
                handler.chunk(gzip.compress(b"", mtime=0))
                progress.append(monotonic())
                if cancellation == "event" and len(progress) == 8:
                    cancel_event.set()
                if handler.owner.stopping.wait(0.02):
                    break
        finally:
            stopped.set()

    def cancel_callback() -> bool:
        ready = bool(progress) and monotonic() >= progress[0] + 0.15
        if ready and cancellation == "callback-error":
            raise RuntimeError("private cancellation callback failure")
        return ready

    wire.steps.append(stream)
    started = monotonic()
    with HttpClient(wire.url(), connect_timeout=1, read_timeout=1.5,
                    cancel=cancel_event if cancellation == "event" else cancel_callback) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "send", json={"content": "already accepted"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == (status, reason, True)
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [("POST", "/send", {"content": "already accepted"})]
    assert len(progress) >= 2 and progress[-1] - progress[0] >= 0.1
    assert stopped.wait(2), "cancelled HTTP connection remained open"
    assert pids, "no transport process was observed after the server received the POST"
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids), "cancelled HTTP transport was not reaped"
    assert monotonic() - started < 4


@pytest.mark.skipif(sys.platform != "linux", reason="HTTP worker ownership uses Linux process supervision")
def test_progressing_headers_cannot_outrun_total_delivery_deadline(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "HTTP_DELIVERY_BUDGET_SECONDS", 2.0)
    progress: list[float] = []
    pids: set[int] = set()
    prior = _child_pids()
    stopped = Event()

    def headers(handler: WireHandler) -> None:
        pids.update(_child_pids() - prior)
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Progress: ")
        handler.wfile.flush()
        try:
            while not handler.owner.stopping.is_set():
                handler.wfile.write(b".")
                handler.wfile.flush()
                progress.append(monotonic())
                if handler.owner.stopping.wait(0.04):
                    break
        finally:
            stopped.set()

    wire.steps.append(headers)
    started = monotonic()
    with HttpClient(wire.url(), connect_timeout=1, read_timeout=1.5) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "scan", json={"library": "committed"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("delivery_deferred", "http_delivery_budget", True)
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [("POST", "/scan", {"library": "committed"})]
    assert len(progress) >= 3 and progress[-1] - progress[0] >= 0.08
    assert stopped.wait(2), "deadline did not close the progressing HTTP connection"
    assert pids and all(not Path(f"/proc/{pid}").exists() for pid in pids)
    assert 1.5 <= monotonic() - started < 5


def test_late_confirmation_body_is_not_reported_successful(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "HTTP_DELIVERY_BUDGET_SECONDS", 2.0)
    progress: list[float] = []

    def late_confirmation(handler: WireHandler) -> None:
        handler.begin(200, (("Transfer-Encoding", "chunked"),))
        handler.chunk(b'{"id":"123","padding":"')
        while not handler.owner.stopping.is_set():
            handler.chunk(b".")
            progress.append(monotonic())
            if handler.owner.stopping.wait(0.04):
                return
            if monotonic() - progress[0] > 3:
                handler.chunk(b'"}')
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
                return

    wire.steps.append(late_confirmation)
    with HttpClient(wire.url(), connect_timeout=1, read_timeout=1.5) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "send", json={"book": "committed"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("delivery_deferred", "http_delivery_budget", True)
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [("POST", "/send", {"book": "committed"})]
    assert len(progress) >= 3 and progress[-1] - progress[0] >= 0.08


def test_safe_read_redirect_preserves_prefix_without_reading_unfinished_redirect_body(wire: WireServer) -> None:
    closed = Event()

    def redirect(handler: WireHandler) -> None:
        handler.begin(302, (("Location", "/proxy/api/next"), ("Content-Length", str(32 * 1024 * 1024))))
        _wait_for_peer_close(handler)
        closed.set()

    wire.steps.append(redirect)
    wire.reply(body=b"fixture")
    with HttpClient(wire.url("/proxy"), auth=("reader", "fictional-password"), read_timeout=0.5) as client:
        assert client.request("GET", "api/first", read_only=True).content == b"fixture"
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/proxy/api/first"), ("GET", "/proxy/api/next")]
    assert closed.wait(2)
    assert all(base64.b64decode(call.headers["authorization"].removeprefix("Basic ")) == b"reader:fictional-password" for call in wire.calls)


@pytest.mark.parametrize("location_kind", ["other-origin", "other-scheme", "prefix-sibling", "outside-prefix", "userinfo"])
def test_authenticated_cross_origin_or_outside_prefix_redirect_is_refused(
    wire: WireServer, location_kind: str,
) -> None:
    with WireServer() as other:
        locations = {
            "other-origin": other.url("/proxy/api"),
            "other-scheme": wire.url("/proxy/api").replace("http://", "https://", 1),
            "prefix-sibling": "/proxy-old/api",
            "outside-prefix": "/api",
            "userinfo": wire.url("/proxy/api").replace("http://", "http://reader:fictional-password@", 1),
        }
        wire.reply(302, headers=(("Location", locations[location_kind]),))
        with HttpClient(wire.url("/proxy"), auth=("reader", "fictional-password")) as client:
            with pytest.raises(HttpError) as caught:
                client.request("GET", "api", read_only=True)
        expected = ("invalid_input", "invalid_http_endpoint") if location_kind == "userinfo" else ("permission_denied", "unsafe_http_redirect")
        assert (caught.value.status, caught.value.reason) == expected
        assert [(call.method, call.target) for call in wire.calls] == [("GET", "/proxy/api")]
        assert other.calls == []
        assert "fictional-password" not in repr(caught.value)


def test_mutation_redirect_is_not_followed_even_on_same_origin(wire: WireServer) -> None:
    wire.reply(307, headers=(("Location", "/proxy/next"),))
    with HttpClient(wire.url("/proxy")) as client:
        with pytest.raises(HttpError) as caught:
            client.request("POST", "send", json={"book": "committed"})
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("delivery_deferred", "http_mutation_redirect", True)
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [("POST", "/proxy/send", {"book": "committed"})]


@pytest.mark.parametrize(("body", "reason"), [(b"01234567", None), (b"012345678", "http_response_limit")])
def test_response_decoded_byte_limit_is_exact(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, body: bytes, reason: str | None,
) -> None:
    monkeypatch.setattr(http, "HTTP_MAX_RESPONSE_BYTES", 8)
    wire.reply(body=body, headers=(("Transfer-Encoding", "chunked"),))
    with HttpClient(wire.url()) as client:
        if reason is None:
            assert client.request("GET", "api", read_only=True).content == b"01234567"
        else:
            with pytest.raises(HttpError) as caught:
                client.request("GET", "api", read_only=True)
            assert (caught.value.status, caught.value.reason) == ("invalid_input", reason)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api")]


@pytest.mark.parametrize(("body", "reason"), [(b"01234567", None), (b"012345678", "http_response_limit")])
def test_real_gzip_wire_body_enforces_decoded_limit(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, body: bytes, reason: str | None,
) -> None:
    monkeypatch.setattr(http, "HTTP_MAX_RESPONSE_BYTES", 8)
    wire.reply(body=gzip.compress(body, mtime=0), headers=(("Content-Encoding", "gzip"), ("Transfer-Encoding", "chunked")))
    with HttpClient(wire.url()) as client:
        if reason is None:
            assert client.request("GET", "api", read_only=True).content == b"01234567"
        else:
            with pytest.raises(HttpError) as caught:
                client.request("GET", "api", read_only=True)
            assert (caught.value.status, caught.value.reason) == ("invalid_input", reason)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api")]


@pytest.mark.parametrize(("body", "reason"), [(b"12345", None), (b"123456", "http_response_limit")])
def test_decoded_budget_is_cumulative_across_rejected_read_attempts(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, body: bytes, reason: str | None,
) -> None:
    monkeypatch.setattr(http, "HTTP_MAX_RESPONSE_BYTES", 8)
    wire.reply(503, b"abc")
    wire.reply(body=gzip.compress(body, mtime=0), headers=(("Content-Encoding", "gzip"), ("Transfer-Encoding", "chunked")))
    with HttpClient(wire.url()) as client:
        if reason is None:
            assert client.request("GET", "api", read_only=True).content == b"12345"
        else:
            with pytest.raises(HttpError) as caught:
                client.request("GET", "api", read_only=True)
            assert (caught.value.status, caught.value.reason) == ("invalid_input", reason)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api"), ("GET", "/api")]


def test_partial_failed_read_bytes_are_not_reset_before_retry(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "HTTP_MAX_RESPONSE_BYTES", 8)
    wire.reply(body=b"abc", headers=(("Content-Length", "6"),))
    wire.reply(body=gzip.compress(b"01234567", mtime=0), headers=(("Content-Encoding", "gzip"), ("Transfer-Encoding", "chunked")))
    with HttpClient(wire.url()) as client:
        with pytest.raises(HttpError) as caught:
            client.request("GET", "api", read_only=True)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "http_response_limit")
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api"), ("GET", "/api")]


def test_declared_over_limit_response_is_refused_without_waiting_for_body(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "HTTP_MAX_RESPONSE_BYTES", 8)
    closed = Event()

    def over_limit(handler: WireHandler) -> None:
        handler.begin(200, (("Content-Length", "9"),))
        _wait_for_peer_close(handler)
        closed.set()

    wire.steps.append(over_limit)
    with HttpClient(wire.url(), read_timeout=0.5, max_attempts=1) as client:
        with pytest.raises(HttpError) as caught:
            client.request("GET", "api", read_only=True)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "http_response_limit")
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api")]
    assert closed.wait(2), "over-limit response left its transport connection open"


def test_invalid_compressed_body_is_not_reported_as_empty_success(wire: WireServer) -> None:
    wire.reply(body=b"not a gzip member", headers=(("Content-Encoding", "gzip"), ("Transfer-Encoding", "chunked")))
    with HttpClient(wire.url()) as client:
        with pytest.raises(HttpError) as caught:
            client.request("GET", "api", read_only=True)
    assert (caught.value.status, caught.value.reason, caught.value.pending_unknown) == ("invalid_input", "invalid_http_content_encoding", False)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api")]


@pytest.mark.parametrize(("has_port", "port"), [(False, "configured"), (True, "configured"), (True, "")])
def test_komga_port_is_inserted_before_prefix_without_duplication(wire: WireServer, has_port: bool, port: str) -> None:
    address = wire.url("/proxy") if has_port else "http://127.0.0.1/proxy"
    configured_port = str(wire.server.server_port) if port else ""
    wire.reply(body=b"[]")
    with HttpClient(komga_base_url(address, configured_port)) as client:
        assert get_komga_libraries(client) == ()
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/proxy/api/v1/libraries")]


def test_komga_ipv6_port_is_inserted_before_prefix() -> None:
    assert komga_base_url("http://[::1]/komga", "25600") == "http://[::1]:25600/komga"


def test_komga_library_schema_and_empty_success_are_distinct(wire: WireServer) -> None:
    wire.reply(body=b'[{"id":"library-1","name":"Manga","root":"/server/books","other":true}]')
    wire.reply(body=b"[]")
    wire.reply(body=b'{"error":"fictional-password"}')
    with HttpClient(wire.url("/komga")) as client:
        assert get_komga_libraries(client) == ({"id": "library-1", "name": "Manga", "root": "/server/books"},)
        assert get_komga_libraries(client) == ()
        with pytest.raises(HttpError) as caught:
            get_komga_libraries(client)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "invalid_komga_library_schema")
    assert caught.value.__context__ is None and "fictional-password" not in repr(caught.value)
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/komga/api/v1/libraries")] * 3


@pytest.mark.parametrize("body", [
    b"not JSON", b'[{"id":"../scan","name":"Unsafe","root":"/books"}]',
    b'[{"id":"one","name":"Manga","root":"books"}]',
    b'[{"id":"one","name":"Manga","root":"/books"},{"id":"one","name":"Novels","root":"/other"}]',
])
def test_malformed_komga_observation_never_becomes_empty_success(wire: WireServer, body: bytes) -> None:
    wire.reply(body=body)
    with HttpClient(wire.url()) as client:
        with pytest.raises(HttpError) as caught:
            get_komga_libraries(client)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "invalid_komga_library_schema")
    assert [(call.method, call.target) for call in wire.calls] == [("GET", "/api/v1/libraries")]


def test_component_mapping_coalesces_only_affected_libraries() -> None:
    libraries = (
        {"id": "manga", "name": "Manga", "root": "/server/books"},
        {"id": "novels", "name": "Novels", "root": "/server/novels"},
        {"id": "old", "name": "Old", "root": "/server/books-old"},
    )
    mappings = (PathMapping(Path("/local"), "/server/books"), PathMapping(Path("/local/novels"), "/server/novels"))
    actual = affected_libraries((Path("/local/Series/v1.cbz"), Path("/local/Series/v2.cbz"), Path("/local/novels/Series/v1.epub")), libraries, mappings=mappings)
    assert actual == ({"id": "manga", "name": "Manga", "root": "/server/books"}, {"id": "novels", "name": "Novels", "root": "/server/novels"})
    assert affected_libraries((Path("/server/books-old/v1.cbz"),), libraries) == ({"id": "old", "name": "Old", "root": "/server/books-old"},)


def test_scan_preview_and_verified_202_never_send_unauthorized_http(wire: WireServer) -> None:
    wire.reply(202, b"")
    with HttpClient(wire.url("/proxy"), auth=("reader", "fictional-password")) as client:
        assert scan_komga_library(client, "library-1", authorization=RunAuthorization()) == Decision("planned", "remote_not_authorized", candidates=("komga/library-1",))
        assert wire.calls == []
        assert scan_komga_library(client, "library-1", authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "komga_scan_accepted", candidates=("komga/library-1",))
    assert [(call.method, call.target, call.body) for call in wire.calls] == [("POST", "/proxy/api/v1/libraries/library-1/scan", b"")]
    assert base64.b64decode(wire.calls[0].headers["authorization"].removeprefix("Basic ")) == b"reader:fictional-password"


def test_scan_timeout_after_actual_post_is_review_required_without_replay(wire: WireServer) -> None:
    wire.steps.append(_wait_for_peer_close)
    with HttpClient(wire.url("/komga"), read_timeout=0.3) as client:
        actual = scan_komga_library(client, "library-1", authorization=RunAuthorization(allow_remote=True))
    assert actual == Decision("review_required", "komga_scan_pending_unknown", candidates=("komga/library-1",))
    assert [(call.method, call.target, call.body) for call in wire.calls] == [("POST", "/komga/api/v1/libraries/library-1/scan", b"")]


@pytest.mark.parametrize("status", [200, 204])
def test_scan_requires_komga_202_protocol_confirmation(wire: WireServer, status: int) -> None:
    wire.reply(status, b"")
    with HttpClient(wire.url("/komga")) as client:
        actual = scan_komga_library(client, "library-1", authorization=RunAuthorization(allow_remote=True))
    assert actual == Decision("invalid_input", f"http_{status}", candidates=("komga/library-1",))
    assert [(call.method, call.target, call.body) for call in wire.calls] == [("POST", "/komga/api/v1/libraries/library-1/scan", b"")]


def test_discord_round_robin_routes_are_independent_and_reach_the_correct_hooks(wire: WireServer) -> None:
    normal = (wire.url("/api/webhooks/1/fictional-a"), wire.url("/api/webhooks/2/fictional-b"))
    for _ in range(11):
        wire.reply(body=b'{"id":"123"}')
    payload, _ = prepare_discord_payload("Same committed message")
    with DiscordDelivery(normal, new_release_webhook=wire.url("/api/webhooks/3/fictional-new"),
                         bookwalker_webhooks=(wire.url("/api/webhooks/4/fictional-released"), wire.url("/api/webhooks/5/fictional-preorder"))) as delivery:
        for _ in range(5):
            key = delivery.select_channel()
            assert delivery.deliver(key, payload, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=(key,))
        for kind, key in (("new_release", "discord/new-release"), ("bookwalker_released", "discord/bookwalker/released"), ("bookwalker_preorder", "discord/bookwalker/preorder")):
            selected = delivery.select_channel(kind)
            assert selected == key
            assert delivery.deliver(selected, payload, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=(key,))
        with DiscordDelivery(normal) as other:
            for kind in ("normal", "new_release"):
                key = other.select_channel(kind)
                assert other.deliver(key, payload, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=(key,))
        key = delivery.select_channel()
        assert delivery.deliver(key, payload, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=("discord/normal/1",))
        assert "fictional" not in json.dumps(delivery.channel_keys)
    assert [call.target for call in wire.calls] == [
        "/api/webhooks/1/fictional-a?wait=true", "/api/webhooks/2/fictional-b?wait=true",
        "/api/webhooks/1/fictional-a?wait=true", "/api/webhooks/2/fictional-b?wait=true",
        "/api/webhooks/1/fictional-a?wait=true", "/api/webhooks/3/fictional-new?wait=true",
        "/api/webhooks/4/fictional-released?wait=true", "/api/webhooks/5/fictional-preorder?wait=true",
        "/api/webhooks/1/fictional-a?wait=true", "/api/webhooks/2/fictional-b?wait=true",
        "/api/webhooks/2/fictional-b?wait=true",
    ]
    assert all(call.method == "POST" and json.loads(call.body) == {"allowed_mentions": {"parse": []}, "content": "Same committed message"} for call in wire.calls)


def test_discord_attachments_use_actual_formats_and_fresh_multipart_payloads(wire: WireServer) -> None:
    png, webp = _image_bytes("PNG", "red"), _image_bytes("WEBP", "blue")
    first, files = prepare_discord_payload(embeds=(format_embed(title="First"), format_embed(title="Second")), attachments=(png, webp))
    second, _ = prepare_discord_payload("Separate message")
    wire.reply(body=b'{"id":"123"}')
    wire.reply(body=b'{"id":"124"}')
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token?thread_id=55&wait=false"),)) as delivery:
        key = delivery.select_channel()
        assert delivery.deliver(key, first, authorization=RunAuthorization(allow_remote=True), attachments=files) == Decision("committed", "discord_delivered", candidates=("discord/normal/0",))
        assert delivery.deliver(key, second, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=("discord/normal/0",))
    assert _multipart(wire.calls[0]) == {
        "files[0]": ("cover_1.png", "image/png", png),
        "files[1]": ("cover_2.webp", "image/webp", webp),
        "payload_json": {
            "allowed_mentions": {"parse": []},
            "embeds": [{"title": "First", "image": {"url": "attachment://cover_1.png"}},
                       {"title": "Second", "image": {"url": "attachment://cover_2.webp"}}],
            "attachments": [{"id": 0, "filename": "cover_1.png"}, {"id": 1, "filename": "cover_2.webp"}],
        },
    }
    assert json.loads(wire.calls[1].body) == {"allowed_mentions": {"parse": []}, "content": "Separate message"}
    assert [(call.method, call.target) for call in wire.calls] == [("POST", "/api/webhooks/1/fictional-token?thread_id=55&wait=true")] * 2
    assert wire.calls[0].headers["content-type"].startswith("multipart/form-data; boundary=")
    assert wire.calls[1].headers["content-type"] == "application/json"
    assert first["embeds"][0]["image"] == {"url": "attachment://cover_1.png"}


def test_discord_chapter_group_order_and_zero_survive_wire_serialization(wire: WireServer) -> None:
    parsing = ParsingPolicy(chapter_support_toggle=True)
    releases = tuple(parse_release(FileObservation(Path("/fixture") / name), parsing) for name in (
        "Series c003.cbz", "Other c002.cbz", "Series c001.cbz", "Series c000.cbz", "Series c002.01.cbz",
    ))
    payload, _ = prepare_discord_payload(embeds=group_chapter_notifications(releases))
    wire.reply(body=b'{"id":"123"}')
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),)) as delivery:
        assert delivery.deliver("discord/normal/0", payload, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=("discord/normal/0",))
    assert json.loads(wire.calls[0].body) == {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {"title": "New Chapter(s) Added", "color": 65280, "fields": [
                {"name": "Series Name", "value": "```Series```", "inline": False},
                {"name": "Chapter Number(s)", "value": "```0-1, 2.01, 3```", "inline": False},
                {"name": "Chapter Name(s)", "value": "```Series c000.cbz\nSeries c001.cbz\nSeries c002.01.cbz\nSeries c003.cbz```", "inline": False},
            ]},
            {"title": "New Chapter(s) Added", "color": 65280, "fields": [
                {"name": "Series Name", "value": "```Other```", "inline": False},
                {"name": "Chapter Number(s)", "value": "```2```", "inline": False},
                {"name": "Chapter Name(s)", "value": "```Other c002.cbz```", "inline": False},
            ]},
        ],
    }
    assert [(call.method, call.target) for call in wire.calls] == [("POST", "/api/webhooks/1/fictional-token?wait=true")]


def test_discord_batches_reach_wire_without_dropping_the_eleventh_embed(wire: WireServer) -> None:
    batches = batch_discord_payloads(tuple(format_embed(title=f"Volume {index}") for index in range(1, 12)))
    wire.reply(body=b'{"id":"123"}')
    wire.reply(body=b'{"id":"124"}')
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),)) as delivery:
        for batch in batches:
            assert delivery.deliver("discord/normal/0", batch, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=("discord/normal/0",))
    assert [[embed["title"] for embed in json.loads(call.body)["embeds"]] for call in wire.calls] == [
        ["Volume 1", "Volume 2", "Volume 3", "Volume 4", "Volume 5", "Volume 6", "Volume 7", "Volume 8", "Volume 9", "Volume 10"],
        ["Volume 11"],
    ]
    assert [(call.method, call.target) for call in wire.calls] == [("POST", "/api/webhooks/1/fictional-token?wait=true")] * 2


def test_discord_authorization_and_ambiguous_failure_preserve_payload_without_replay(wire: WireServer) -> None:
    payload, _ = prepare_discord_payload("Local commit happened")
    wire.steps.append(_wait_for_peer_close)
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),), read_timeout=0.3) as delivery:
        assert delivery.deliver("discord/normal/0", payload, authorization=RunAuthorization()) == Decision("planned", "remote_not_authorized", candidates=("discord/normal/0",))
        assert wire.calls == []
        actual = delivery.deliver("discord/normal/0", payload, authorization=RunAuthorization(allow_remote=True))
    assert actual == Decision("review_required", "discord_delivery_pending_unknown", candidates=("discord/normal/0",))
    assert payload == {"allowed_mentions": {"parse": []}, "content": "Local commit happened"}
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/api/webhooks/1/fictional-token?wait=true", {"allowed_mentions": {"parse": []}, "content": "Local commit happened"}),
    ]
    assert "fictional-token" not in json.dumps(actual.as_dict())


@pytest.mark.parametrize(("status", "body", "expected_status", "reason"), [
    (403, b"private token", "permission_denied", "http_403"),
    (429, b'{"retry_after":120}', "delivery_deferred", "http_retry_budget"),
    (200, b"not a confirmation", "review_required", "discord_delivery_pending_unknown"),
    (200, b"{}", "review_required", "discord_delivery_pending_unknown"),
    (200, b'{"id":123}', "review_required", "discord_delivery_pending_unknown"),
    (200, b'{"id":"not-a-message-id"}', "review_required", "discord_delivery_pending_unknown"),
])
def test_discord_delivery_requires_confirmation_and_returns_redacted_typed_outcomes(
    wire: WireServer, status: int, body: bytes, expected_status: str, reason: str,
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    wire.reply(status, body)
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),)) as delivery:
        payload, _ = prepare_discord_payload("Committed")
        actual = delivery.deliver("discord/normal/0", payload, authorization=RunAuthorization(allow_remote=True))
    assert actual.as_dict() == {"status": expected_status, "reason": reason, "path": None, "candidates": ["discord/normal/0"]}
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/api/webhooks/1/fictional-token?wait=true", {"allowed_mentions": {"parse": []}, "content": "Committed"}),
    ]
    assert payload == {"allowed_mentions": {"parse": []}, "content": "Committed"}
    captured = capsys.readouterr()
    assert "fictional-token" not in json.dumps(actual.as_dict()) + caplog.text + captured.out + captured.err


def test_discord_invalid_route_is_rejected_without_retaining_or_using_credentials(wire: WireServer) -> None:
    with pytest.raises(HttpError) as caught:
        DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"), wire.url("/hook").replace("http://", "http://reader:fictional-password@", 1)))
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "invalid_http_endpoint")
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "fictional-password" not in repr(caught.value) and "fictional-token" not in repr(caught.value)
    assert wire.calls == []
    wire.reply(body=b'{"id":"125"}')
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),)) as delivery:
        payload, _ = prepare_discord_payload("Valid configuration")
        assert delivery.deliver("discord/normal/0", payload, authorization=RunAuthorization(allow_remote=True)) == Decision("committed", "discord_delivered", candidates=("discord/normal/0",))
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/api/webhooks/1/fictional-token?wait=true", {"allowed_mentions": {"parse": []}, "content": "Valid configuration"}),
    ]


def test_failed_hook_does_not_reselect_or_contaminate_the_next_delivery(wire: WireServer) -> None:
    wire.steps.append(_reset_connection)
    wire.reply(body=b'{"id":"125"}')
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-one"), wire.url("/api/webhooks/2/fictional-two"))) as delivery:
        first, _ = prepare_discord_payload("First commit")
        failed = delivery.deliver(delivery.select_channel(), first, authorization=RunAuthorization(allow_remote=True))
        second, _ = prepare_discord_payload("Second commit")
        delivered = delivery.deliver(delivery.select_channel(), second, authorization=RunAuthorization(allow_remote=True))
    assert failed == Decision("review_required", "discord_delivery_pending_unknown", candidates=("discord/normal/0",))
    assert delivered == Decision("committed", "discord_delivered", candidates=("discord/normal/1",))
    assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
        ("POST", "/api/webhooks/1/fictional-one?wait=true", {"allowed_mentions": {"parse": []}, "content": "First commit"}),
        ("POST", "/api/webhooks/2/fictional-two?wait=true", {"allowed_mentions": {"parse": []}, "content": "Second commit"}),
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="HTTP worker ownership uses Linux process supervision")
@pytest.mark.parametrize(("failure", "status", "reason", "durable_state"), [
    ("request-quota", "invalid_input", "http_worker_failed", "failed"),
    ("prestart-cancellation", "cancelled", "http_cancelled", "pending"),
    ("launch-failure", "failed", "http_worker_failed", "failed"),
])
def test_unsent_http_failure_records_definite_durable_delivery(
    wire: WireServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    committed_notice: tuple[StateStore, tuple[LibraryRoot, ...], ReleaseSnapshot, RunResult],
    failure: str, status: str, reason: str, durable_state: str,
) -> None:
    state, configured, original, result = committed_notice
    published = fingerprint(result.releases[0].path, configured[0].path)
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks == 2

    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),),
                         cancel=cancel if failure == "prestart-cancellation" else None) as delivery:
        settle = partial(settle_committed_deliveries, state=state, roots=configured, parsing=ParsingPolicy(),
                         ranking=RankingPolicy(), cover_policy=CoverPolicy(ArchivePolicy()), discord=delivery,
                         komga=None, mappings=(), authorization=RunAuthorization(apply=True, allow_remote=True),
                         cancel=Event(), before=(original,))
        queued = settle(result, authorization=RunAuthorization(apply=True))
        stored, = [row for row in state.list_outbox() if row.event_type.startswith("delivery_discord_")]
        assert stored.state == "queued" and wire.calls == []
        local_events = tuple(row.event_id for row in state.list_outbox() if not row.event_type.startswith("delivery_"))
        wire.reply(body=b'{"id":"123"}')
        with monkeypatch.context() as patch:
            if failure == "request-quota":
                patch.setattr(workers, "JOB_REQUEST_LIMITS", {**workers.JOB_REQUEST_LIMITS, "http": 128})
            elif failure == "launch-failure":
                patch.setattr(workers.sys, "executable", str(tmp_path / "missing-worker-executable"))
            rejected = settle(queued)
        row, = [row for row in state.list_outbox() if row.event_type.startswith("delivery_discord_")]
        job = state.get_job("delivery-" + row.event_id)
        assert job is not None
        assert (rejected.decisions[-1].status, rejected.decisions[-1].reason) == (status, reason)
        assert (job.state, job.error, row.state, row.error) == (durable_state, reason, durable_state, reason)
        assert row.payload == stored.payload and rejected.deferred_deliveries == (row.event_id,)
        assert (rejected.committed, rejected.failed, rejected.skipped) == (1, 0, 0)
        assert wire.calls == []
        fresh = StateStore(state.state_dir)
        replay = settle(rejected, state=fresh)
        settled, = [row for row in fresh.list_outbox() if row.event_type.startswith("delivery_discord_")]
        if durable_state == "pending":
            assert (fresh.get_job(job.job_id).state, settled.state, replay.deferred_deliveries) == ("complete", "complete", ())
            assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
                ("POST", "/api/webhooks/1/fictional-token?wait=true", stored.payload["payload"]),
            ]
        else:
            assert (fresh.get_job(job.job_id).state, settled.state, replay.deferred_deliveries) == ("failed", "failed", (row.event_id,))
            assert wire.calls == []
        assert (replay.committed, replay.failed, replay.skipped) == (1, 0, 0)
        assert tuple(row.event_id for row in fresh.list_outbox() if not row.event_type.startswith("delivery_")) == local_events
    assert fingerprint(result.releases[0].path, configured[0].path) == published


@pytest.mark.skipif(sys.platform != "linux", reason="HTTP worker ownership uses Linux process supervision")
def test_postwire_unknown_delivery_remains_durable_and_nonreplayable(
    wire: WireServer,
    committed_notice: tuple[StateStore, tuple[LibraryRoot, ...], ReleaseSnapshot, RunResult],
) -> None:
    state, configured, original, result = committed_notice
    published = fingerprint(result.releases[0].path, configured[0].path)
    with DiscordDelivery((wire.url("/api/webhooks/1/fictional-token"),), read_timeout=0.3) as delivery:
        settle = partial(settle_committed_deliveries, state=state, roots=configured, parsing=ParsingPolicy(),
                         ranking=RankingPolicy(), cover_policy=CoverPolicy(ArchivePolicy()), discord=delivery,
                         komga=None, mappings=(), authorization=RunAuthorization(apply=True, allow_remote=True),
                         cancel=Event(), before=(original,))
        queued = settle(result, authorization=RunAuthorization(apply=True))
        stored, = [row for row in state.list_outbox() if row.event_type.startswith("delivery_discord_")]
        wire.steps.append(_wait_for_peer_close)
        unknown = settle(queued)
        row, = [row for row in state.list_outbox() if row.event_type.startswith("delivery_discord_")]
        job = state.get_job("delivery-" + row.event_id)
        assert job is not None
        assert (unknown.decisions[-1].status, unknown.decisions[-1].reason) == ("review_required", "discord_delivery_pending_unknown")
        assert (job.state, job.error, row.state, row.error, row.attempts) == (
            "needs_review", "discord_delivery_pending_unknown", "unknown", "discord_delivery_pending_unknown", 1,
        )
        assert row.payload == stored.payload and unknown.deferred_deliveries == (row.event_id,)
        assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
            ("POST", "/api/webhooks/1/fictional-token?wait=true", stored.payload["payload"]),
        ]
        fresh = StateStore(state.state_dir)
        replay = settle(unknown, state=fresh)
        unsettled, = [row for row in fresh.list_outbox() if row.event_type.startswith("delivery_discord_")]
        assert (fresh.get_job(job.job_id).state, unsettled.state, replay.deferred_deliveries) == ("needs_review", "unknown", (row.event_id,))
        assert (replay.committed, replay.failed, replay.skipped) == (1, 0, 0)
        assert [(call.method, call.target, json.loads(call.body)) for call in wire.calls] == [
            ("POST", "/api/webhooks/1/fictional-token?wait=true", stored.payload["payload"]),
        ]
    assert fingerprint(result.releases[0].path, configured[0].path) == published

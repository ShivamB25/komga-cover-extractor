"""Scoped, bounded Requests transport with explicit mutation ambiguity."""
from __future__ import annotations

import binascii
import math
import re
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http.cookiejar import CookieJar
from threading import Event
from time import monotonic, sleep
from types import TracebackType
from typing import TYPE_CHECKING, cast
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

from ..domain.decisions import Status

if TYPE_CHECKING:
    from http.client import HTTPResponse
    from urllib.request import Request

    import requests

Cancel = Callable[[], bool] | Event | None
HTTP_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
HTTP_DELIVERY_BUDGET_SECONDS = 60.0
HTTP_MAX_REDIRECTS = 5
HTTP_MAX_COOKIES = 64
HTTP_MAX_COOKIE_BYTES = 16 * 1024
HTTP_WORKER_OUTPUT_BYTES = 32 * 1024 * 1024
HTTP_READ_CHUNK_BYTES = 64 * 1024
_READ_RETRY_STATUSES = frozenset({408, 500, 502, 503, 504})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SAFE_HEADERS = frozenset({"user-agent", "accept", "accept-language"})
_TOKEN = re.compile(r"[a-zA-Z0-9!#$%&'*+.^_`|~-]+\Z")


class HttpError(Exception):
    """Only stable outcome codes cross the HTTP error boundary."""

    def __init__(self, status: Status, reason: str, *, pending_unknown: bool = False) -> None:
        self.status: Status = status
        self.reason = reason
        self.pending_unknown = pending_unknown
        super().__init__(f"{status}: {reason}")


def _url_parts(url: str) -> SplitResult:
    invalid = False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        invalid = True
        parts = urlsplit("")
        port = None
    if (invalid or parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.fragment
            or port is not None and not 1 <= port <= 65535
            or any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url):
        raise HttpError("invalid_input", "invalid_http_endpoint")
    _safe_path(parts.path)
    return parts


def _safe_path(path: str) -> None:
    decoded = path
    for _ in range(4):
        if ("\\" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                or any(part in {".", ".."} for part in decoded.split("/"))
                or decoded.startswith("//")
                or re.search(r"%(?![0-9a-fA-F]{2})", decoded)):
            raise HttpError("invalid_input", "unsafe_http_path")
        next_path = unquote(decoded)
        if next_path == decoded:
            return
        if next_path.count("/") != decoded.count("/"):
            raise HttpError("invalid_input", "unsafe_http_path")
        decoded = next_path
    raise HttpError("invalid_input", "unsafe_http_path")


def _origin(parts: SplitResult) -> tuple[str, str | None, int]:
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _is_cancelled(cancel: Cancel) -> bool:
    return cancel.is_set() if isinstance(cancel, Event) else bool(cancel and cancel())


def _wait(seconds: float, *, deadline: float, cancel: Cancel) -> None:
    until = monotonic() + seconds
    if until >= deadline:
        raise HttpError("delivery_deferred", "http_retry_budget")
    while monotonic() < until:
        if _is_cancelled(cancel):
            raise HttpError("cancelled", "http_cancelled")
        delay = min(0.1, until - monotonic())
        if delay > 0:
            if isinstance(cancel, Event):
                cancel.wait(delay)
            else:
                sleep(delay)
    if _is_cancelled(cancel):
        raise HttpError("cancelled", "http_cancelled")


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
        except ValueError:
            seconds = math.nan
            try:
                stamp = parsedate_to_datetime(value)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                seconds = (stamp - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
        if math.isfinite(seconds):
            return max(0.0, seconds)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        retry_after = payload.get("retry_after")
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and math.isfinite(retry_after):
            return max(0.0, float(retry_after))
    return None


def _status_error(code: int, *, read_only: bool) -> HttpError:
    if code in {401, 403}:
        return HttpError("permission_denied", f"http_{code}")
    if code == 429:
        return HttpError("delivery_deferred", "http_rate_limited")
    if code in _READ_RETRY_STATUSES or code >= 500:
        return HttpError("delivery_deferred", f"http_{code}", pending_unknown=not read_only)
    return HttpError("invalid_input", f"http_{code}")


def _cookies_within_budget(jar: CookieJar) -> bool:
    return len(jar) <= HTTP_MAX_COOKIES and sum(
        len(cookie.name.encode("utf-8")) + len((cookie.value or "").encode("utf-8")) + 4
        for cookie in jar
    ) <= HTTP_MAX_COOKIE_BYTES


def _wire_error(error: HttpError, consumed: int) -> dict[str, object]:
    return {"status": "valid", "bytes_read": consumed, "error": {
        "status": error.status, "reason": error.reason, "pending_unknown": error.pending_unknown,
    }}


def http_job(request: dict[str, object]) -> dict[str, object]:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.exceptions import DecodeError
    from urllib3.exceptions import HTTPError as UrllibError

    from ..infrastructure.seven_zip import require_archive_worker
    from ..infrastructure.workers import WorkerError

    require_archive_worker()
    if set(request) != {"method", "url", "headers", "body", "connect_timeout", "read_timeout", "byte_limit", "read_only"}:
        raise WorkerError("invalid_http_worker_request")
    method, url, headers, encoded = request["method"], request["url"], request["headers"], request["body"]
    limit, read_only = request["byte_limit"], request["read_only"]
    if (not isinstance(method, str) or method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(url, str) or not isinstance(headers, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items())
            or encoded is not None and not isinstance(encoded, str)
            or type(limit) is not int or not 0 <= limit <= HTTP_MAX_RESPONSE_BYTES or type(read_only) is not bool):
        raise WorkerError("invalid_http_worker_request")
    _url_parts(url)
    try:
        body = b64decode(encoded, validate=True) if isinstance(encoded, str) else None
    except (ValueError, binascii.Error):
        raise WorkerError("invalid_http_worker_request") from None
    timeouts = request["connect_timeout"], request["read_timeout"]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or not 0 < value <= HTTP_DELIVERY_BUDGET_SECONDS for value in timeouts):
        raise WorkerError("invalid_http_worker_request")
    consumed = 0
    sent = False
    response = None
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", HTTPAdapter(max_retries=0, pool_block=False))
        session.mount("http://", HTTPAdapter(max_retries=0, pool_block=False))
        try:
            prepared = session.prepare_request(requests.Request(method, url, headers=headers, data=body))
            sent = True
            response = session.get_adapter(url).send(
                prepared, stream=True, timeout=(cast(float, timeouts[0]), cast(float, timeouts[1])),
                verify=True, cert=None, proxies={},
            )
            pairs = list(response.raw.headers.items())
            chunks: list[bytes] = []
            if response.status_code not in _REDIRECT_STATUSES:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError:
                        declared = -1
                    if declared < 0:
                        return _wire_error(HttpError("invalid_input", "invalid_http_content_length", pending_unknown=not read_only), consumed)
                    if declared > limit:
                        return _wire_error(HttpError("invalid_input", "http_response_limit", pending_unknown=not read_only), consumed)
                while True:
                    chunk = response.raw.read1(min(HTTP_READ_CHUNK_BYTES, limit - consumed + 1), decode_content=True)
                    if not chunk:
                        break
                    consumed += len(chunk)
                    if consumed > limit:
                        return _wire_error(HttpError("invalid_input", "http_response_limit", pending_unknown=not read_only), consumed)
                    chunks.append(chunk)
            return {"status": "valid", "code": response.status_code, "headers": pairs,
                    "content": b64encode(b"".join(chunks)).decode("ascii"), "bytes_read": consumed}
        except requests.exceptions.SSLError:
            return _wire_error(HttpError("permission_denied", "http_tls_failed", pending_unknown=sent and not read_only), consumed)
        except (requests.exceptions.ContentDecodingError, DecodeError):
            return _wire_error(HttpError("invalid_input", "invalid_http_content_encoding", pending_unknown=sent and not read_only), consumed)
        except (requests.exceptions.InvalidURL, requests.exceptions.InvalidHeader, ValueError, TypeError):
            return _wire_error(HttpError("invalid_input", "invalid_http_request", pending_unknown=sent and not read_only), consumed)
        except (requests.exceptions.RequestException, UrllibError, OSError):
            return _wire_error(HttpError("delivery_deferred", "http_transport_failed", pending_unknown=sent and not read_only), consumed)
        finally:
            if response is not None:
                response.close()


class HttpClient:
    """One scoped session; fixed wire jobs are killed and reaped on cancellation."""

    def __init__(
        self, base_url: str, *, connect_timeout: float = 5, read_timeout: float = 15,
        max_attempts: int = 3, cancel: Cancel = None, auth: tuple[str, str] | None = None,
    ) -> None:
        parts = _url_parts(base_url)
        if parts.query:
            raise HttpError("invalid_input", "invalid_http_endpoint")
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= HTTP_DELIVERY_BUDGET_SECONDS
                for value in (connect_timeout, read_timeout))
                or type(max_attempts) is not int or not 1 <= max_attempts <= 3):
            raise HttpError("invalid_input", "invalid_http_limits")
        if auth is not None and (not isinstance(auth, tuple) or len(auth) != 2
                                 or any(not isinstance(value, str) for value in auth)):
            raise HttpError("invalid_input", "invalid_http_auth")
        try:
            import requests
            from requests.adapters import HTTPAdapter
        except ImportError:
            raise HttpError("missing_dependency", "http_requires_integrations") from None
        self._parts = parts
        self._prefix = parts.path.rstrip("/")
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._max_attempts = max_attempts
        self._cancel = cancel
        self._closed = False
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.verify = True
        self._session.auth = None
        self._session.headers.update({"Accept": "*/*", "Accept-Encoding": "identity"})
        if auth is not None:
            credentials = f"{auth[0]}:{auth[1]}".encode()
            self._session.headers["Authorization"] = "Basic " + b64encode(credentials).decode("ascii")
        try:
            self._session.mount("https://", HTTPAdapter(max_retries=0, pool_block=False))
            self._session.mount("http://", HTTPAdapter(max_retries=0, pool_block=False))
        except Exception:
            self._session.close()
            raise

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._session.close()

    @property
    def endpoint_key(self) -> str:
        scheme, host, port = _origin(self._parts)
        endpoint = f"{scheme}://{host}:{port}{self._prefix}"
        return sha256(endpoint.encode("utf-8")).hexdigest()

    def set_header(self, name: str, value: str) -> None:
        if name.lower() not in _SAFE_HEADERS or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HttpError("invalid_input", "unsafe_http_header")
        self._session.headers[name] = value

    def set_cookie(self, name: str, value: str, *, path: str = "/") -> None:
        if not _TOKEN.fullmatch(name) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HttpError("invalid_input", "unsafe_http_cookie")
        scope = self._prefix + "/" if path == "/" else path
        _safe_path(scope)
        if not scope.startswith("/") or not self._within_prefix(scope):
            raise HttpError("invalid_input", "unsafe_http_cookie")
        previous = self._session.cookies.copy()
        self._session.cookies.set(name, value, domain=cast(str, self._parts.hostname), path=scope,
                                  secure=self._parts.scheme == "https")
        self._session.cookies.clear_expired_cookies()
        if not _cookies_within_budget(self._session.cookies):
            self._session.cookies = previous
            raise HttpError("invalid_input", "http_cookie_limit")

    def _within_prefix(self, path: str) -> bool:
        return not self._prefix or path == self._prefix or path.startswith(self._prefix + "/")

    def _request_url(self, relative: str) -> str:
        invalid = False
        try:
            parts = urlsplit(relative)
        except ValueError:
            invalid = True
            parts = urlsplit("")
        if invalid or parts.scheme or parts.netloc or parts.fragment or relative.startswith("//"):
            raise HttpError("invalid_input", "unsafe_http_path")
        _safe_path(parts.path)
        if any(ord(char) < 32 or ord(char) == 127 for char in relative):
            raise HttpError("invalid_input", "unsafe_http_path")
        path = self._prefix + "/" + parts.path.lstrip("/") if parts.path else self._prefix or "/"
        return urlunsplit((self._parts.scheme, self._parts.netloc, path, parts.query, ""))

    def _redirect_url(self, current: str, location: str) -> str:
        if any(ord(char) < 33 or ord(char) == 127 for char in location) or "\\" in location:
            raise HttpError("permission_denied", "unsafe_http_redirect")
        target = urljoin(current, location)
        parts = _url_parts(target)
        if _origin(parts) != _origin(self._parts) or not self._within_prefix(parts.path):
            raise HttpError("permission_denied", "unsafe_http_redirect")
        return target

    def _perform(
        self, method: str, url: str, *, data: object, json: object,
        files: Mapping[str, tuple[str, bytes, str]] | None, read_only: bool,
        deadline: float, byte_budget: list[int],
    ) -> requests.Response | HttpError:
        import requests
        from requests.cookies import MockRequest, MockResponse
        from requests.structures import CaseInsensitiveDict

        from ..infrastructure.workers import WorkerError, run_worker

        remaining = deadline - monotonic()
        if remaining <= 0:
            return HttpError("delivery_deferred", "http_delivery_budget")
        sent = False
        try:
            self._session.cookies.clear_expired_cookies()
            body = cast(Mapping[str, object] | str | bytes | None, data)
            prepared = self._session.prepare_request(requests.Request(method, url, data=body, json=json, files=files))
            cookie_header = prepared.headers.get("Cookie", "")
            if len(cookie_header.encode("utf-8")) > HTTP_MAX_COOKIE_BYTES:
                return HttpError("invalid_input", "http_cookie_limit")
            wire_body = prepared.body.encode("utf-8") if isinstance(prepared.body, str) else prepared.body
            if wire_body is not None and not isinstance(wire_body, bytes):
                return HttpError("invalid_input", "invalid_http_request")
            response_data = run_worker("http", {
                "method": method, "url": url, "headers": dict(prepared.headers),
                "body": b64encode(wire_body).decode("ascii") if wire_body is not None else None,
                "connect_timeout": min(self._connect_timeout, remaining),
                "read_timeout": min(self._read_timeout, remaining),
                "byte_limit": byte_budget[0], "read_only": read_only,
            }, timeout=remaining, cancel=self._cancel, max_output_bytes=HTTP_WORKER_OUTPUT_BYTES)
            sent = True
            consumed = response_data.get("bytes_read")
            if type(consumed) is not int or not 0 <= consumed <= byte_budget[0] + 1:
                return HttpError("invalid_input", "invalid_http_worker_result", pending_unknown=not read_only)
            byte_budget[0] -= consumed
            error = response_data.get("error")
            if error is not None:
                if not isinstance(error, dict) or not isinstance(error.get("reason"), str) or type(error.get("pending_unknown")) is not bool:
                    return HttpError("invalid_input", "invalid_http_worker_result", pending_unknown=not read_only)
                status = error.get("status")
                if status not in {"invalid_input", "unsupported", "missing_dependency", "cancelled", "failed", "permission_denied", "delivery_deferred", "review_required"}:
                    return HttpError("invalid_input", "invalid_http_worker_result", pending_unknown=not read_only)
                return HttpError(cast(Status, status), error["reason"], pending_unknown=error["pending_unknown"])
            code, pairs, encoded = response_data.get("code"), response_data.get("headers"), response_data.get("content")
            if (type(code) is not int or not 100 <= code <= 599 or not isinstance(pairs, list)
                    or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(value, str) for value in pair) for pair in pairs)
                    or not isinstance(encoded, str)):
                return HttpError("invalid_input", "invalid_http_worker_result", pending_unknown=not read_only)
            content = b64decode(encoded, validate=True)
            if len(content) != consumed:
                return HttpError("invalid_input", "invalid_http_worker_result", pending_unknown=not read_only)
            message = Message()
            for key, value in pairs:
                message[key] = value
            previous = self._session.cookies.copy()
            self._session.cookies.extract_cookies(
                cast("HTTPResponse", MockResponse(message)), cast("Request", MockRequest(prepared)),
            )
            self._session.cookies.clear_expired_cookies()
            if not _cookies_within_budget(self._session.cookies):
                self._session.cookies = previous
                return HttpError("invalid_input", "http_cookie_limit", pending_unknown=not read_only)
            response = requests.Response()
            response.status_code = code
            response.url = url
            response.request = prepared
            response.headers = CaseInsensitiveDict(pairs)
            response.encoding = requests.utils.get_encoding_from_headers(response.headers)
            response._content = content
            _ = response.content
            return response
        except WorkerError as error:
            pending_unknown = error.started and not read_only
            if error.status == "cancelled":
                return HttpError("cancelled", "http_cancelled", pending_unknown=pending_unknown)
            if error.reason == "worker_deadline":
                return HttpError("delivery_deferred", "http_delivery_budget", pending_unknown=pending_unknown)
            return HttpError(cast(Status, error.status), "http_worker_failed", pending_unknown=pending_unknown)
        except (requests.exceptions.RequestException, ValueError, TypeError, binascii.Error):
            return HttpError("invalid_input", "invalid_http_request", pending_unknown=sent and not read_only)

    def request(
        self, method: str, relative: str, *, data: object = None, json: object = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        read_only: bool = False, expected_statuses: Sequence[int] = (200,),
    ) -> requests.Response:
        if self._closed:
            raise HttpError("invalid_input", "http_client_closed")
        method = method.upper()
        if method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}:
            raise HttpError("invalid_input", "invalid_http_method")
        if data is not None and not isinstance(data, (Mapping, str, bytes)):
            raise HttpError("invalid_input", "invalid_http_request")
        if not expected_statuses or any(type(code) is not int or not 100 <= code <= 599 for code in expected_statuses):
            raise HttpError("invalid_input", "invalid_http_statuses")
        url = self._request_url(relative)
        deadline = monotonic() + HTTP_DELIVERY_BUDGET_SECONDS
        attempts = 0
        redirects = 0
        byte_budget = [HTTP_MAX_RESPONSE_BYTES]
        while attempts < self._max_attempts:
            if _is_cancelled(self._cancel):
                raise HttpError("cancelled", "http_cancelled")
            attempts += 1
            response = self._perform(method, url, data=data, json=json, files=files,
                                     read_only=read_only, deadline=deadline,
                                     byte_budget=byte_budget)
            if isinstance(response, HttpError):
                if read_only and response.reason == "http_transport_failed" and attempts < self._max_attempts:
                    _wait(float(attempts), deadline=deadline, cancel=self._cancel)
                    continue
                raise response
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                response.close()
                if not read_only:
                    raise HttpError("delivery_deferred", "http_mutation_redirect", pending_unknown=True)
                if not location or redirects >= HTTP_MAX_REDIRECTS:
                    raise HttpError("invalid_input", "http_redirect_limit")
                url = self._redirect_url(url, location)
                redirects += 1
                attempts -= 1
                continue
            if response.status_code in expected_statuses:
                return response
            error = _status_error(response.status_code, read_only=read_only)
            if response.status_code == 429 and attempts < self._max_attempts:
                delay = _retry_after(response)
                if delay is not None:
                    _wait(delay, deadline=deadline, cancel=self._cancel)
                    continue
            if read_only and response.status_code in _READ_RETRY_STATUSES and attempts < self._max_attempts:
                _wait(float(attempts), deadline=deadline, cancel=self._cancel)
                continue
            raise error
        raise HttpError("delivery_deferred", "http_retry_budget")

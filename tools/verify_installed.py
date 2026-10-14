"""Exercise an actual installed wheel outside the checkout on disposable inputs.

The existing --source-root invocation remains the full deployment proof. Use
--capabilities-only in each clean core or individual-extra wheel environment.
--expect-extra declares the selected extra. --dependency-lock checks the installed
inventory against that environment's hashed lock export without installing it.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_PACKAGE = "komga_cover_extractor"
_EXTRAS = {
    "covers": ("PIL", "lxml"),
    "watch": ("watchdog",),
    "image-similarity": ("numpy", "cv2", "skimage"),
    "conversion": ("filetype", "py7zr", "rarfile"),
    "integrations": ("requests", "bs4", "lxml"),
}
_OPTIONAL_MODULES = tuple(sorted({name for names in _EXTRAS.values() for name in names}))
_CAPABILITY_MODULES = {
    **_EXTRAS,
    "image-similarity": ("PIL", *_EXTRAS["image-similarity"]),
    "conversion": ("py7zr",),
}

_COLD_IMPORT = r'''
import argparse
import builtins
import importlib
import importlib.util
import io
import json
import multiprocessing.process
import os
import sys
import threading
from pathlib import Path

module_name = sys.argv[1]
violations = []
stdout, stderr = io.StringIO(), io.StringIO()
threads_before = {thread.ident for thread in threading.enumerate()}
fixture_root = Path.cwd()

def reject(kind):
    violations.append(kind)
    raise RuntimeError("forbidden import effect: " + kind)

def audit(event, arguments):
    if event == "open":
        path, mode, flags = arguments
        if isinstance(path, (str, bytes)) and Path(os.fsdecode(path)).name == "settings.py":
            reject("settings-source-read")
        if isinstance(path, (str, bytes)) and Path(os.fsdecode(path)).absolute().is_relative_to(fixture_root):
            reject("fixture-read")
        if isinstance(mode, str) and any(char in mode for char in "wax+"):
            reject("file-write")
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            reject("file-write")
    elif event in {"os.listdir", "os.scandir"}:
        if isinstance(arguments[0], (str, bytes)) and Path(os.fsdecode(arguments[0])).absolute().is_relative_to(fixture_root):
            reject("fixture-scan")
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.truncate", "os.chmod", "os.chown", "os.utime", "os.chdir"}:
        reject(event)
    elif event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.fork", "os.posix_spawn", "os.exec", "sqlite3.connect"}:
        reject(event)
    elif event == "import" and arguments[0] == "settings":
        reject("settings-import")

def profile(frame, event, argument):
    if event == "call" and (frame.f_globals.get("__name__"), frame.f_code.co_qualname) in {
        ("requests.sessions", "Session.__init__"),
        ("urllib3.poolmanager", "PoolManager.__init__"),
        ("urllib3.poolmanager", "ProxyManager.__init__"),
        ("aiohttp.client", "ClientSession.__init__"),
        ("qbittorrentapi.client", "Client.__init__"),
        ("discord_webhook.webhook", "DiscordWebhook.__init__"),
        ("komga_cover_extractor.integrations.http", "HttpClient.__init__"),
    }:
        reject("client-construction")

argparse.ArgumentParser.parse_args = lambda *args, **kwargs: reject("argument-parsing")
argparse.ArgumentParser.parse_known_args = lambda *args, **kwargs: reject("argument-parsing")
builtins.input = lambda *args, **kwargs: reject("stdin-read")
threading.Thread.start = lambda *args, **kwargs: reject("thread-start")
multiprocessing.process.BaseProcess.start = lambda *args, **kwargs: reject("process-start")
sys.addaudithook(audit)
sys.setprofile(profile)
previous_stdout, previous_stderr = sys.stdout, sys.stderr
failure = None
try:
    sys.stdout, sys.stderr = stdout, stderr
    if len(sys.argv) == 3:
        spec = importlib.util.spec_from_file_location(module_name, sys.argv[2])
        assert spec is not None and spec.loader is not None
        imported = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(imported)
    else:
        imported = importlib.import_module(module_name)
except BaseException as error:
    failure = {"type": type(error).__name__, "message": str(error)[:4096]}
finally:
    sys.setprofile(None)
    sys.stdout, sys.stderr = previous_stdout, previous_stderr
remaining_threads = [thread.name for thread in threading.enumerate() if thread.ident not in threads_before]
if "settings" in sys.modules:
    violations.append("settings-module-loaded")
print(json.dumps({
    "module": module_name, "status": "pass" if failure is None and not violations and not remaining_threads and not stdout.getvalue() and not stderr.getvalue() else "fail",
    "violations": violations, "exception": failure, "remaining_threads": remaining_threads,
    "stdout": stdout.getvalue()[:4096], "stderr": stderr.getvalue()[:4096],
    "origin": getattr(imported, "__file__", None) if failure is None else None,
}, sort_keys=True))
'''

_PURE_PARSE = r'''
import json
import sys
from pathlib import Path
from komga_cover_extractor.config import load_config
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release
from komga_cover_extractor.parsing.numbers import detect_missing_releases

config = load_config(environ={}, cwd=Path.cwd())
policy = ParsingPolicy()
first = parse_release(FileObservation(Path("/observations/Series v01.cbz")), policy)
third = parse_release(FileObservation(Path("/observations/Series v03.cbz")), policy)
zero = parse_release(FileObservation(Path("/observations/Series c000.cbz")), policy)
assert (first.display_series, first.kind, first.number.values) == ("Series", "volume", (1,))
assert (zero.kind, zero.number.kind, zero.number.values) == ("chapter", "scalar", (0,))
assert detect_missing_releases((first.number, third.number)).missing == (2,)
heavy = [name for name in sys.modules if name.split(".")[0] in {"PIL", "lxml", "numpy", "cv2", "skimage", "py7zr", "rarfile", "requests", "bs4", "watchdog", "filetype"}]
assert not heavy, heavy
print(json.dumps({"status": "pass", "series": first.display_series, "known_chapter_zero": True, "missing_volumes": [2], "optional_modules_loaded": heavy}))
'''

_CAPABILITY_ACTION = r'''
import importlib.util
import json
import sys
import threading
from pathlib import Path

name, directory = sys.argv[1], Path(sys.argv[2])
result = {}
try:
    if name == "covers":
        import zipfile
        from komga_cover_extractor.application.extract_covers import extract_cover
        from komga_cover_extractor.covers.selection import CoverPolicy
        from komga_cover_extractor.infrastructure.images import ByteOutputPolicy, validate_image
        from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
        book = directory / "Capability v01.epub"
        with zipfile.ZipFile(book, "w") as archive:
            archive.writestr("mimetype", b"application/epub+zip")
            archive.writestr("META-INF/container.xml", b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr("OPS/package.opf", b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/><manifest><item id="art" href="images/Front%20Cover.png" media-type="image/png" properties="cover-image"/></manifest><spine/></package>')
            archive.writestr("OPS/images/Front Cover.png", (directory / "red.png").read_bytes())
            archive.writestr("decoy/Front Cover.png", b"not the authoritative image")
        before = book.read_bytes()
        cover = extract_cover(book, policy=CoverPolicy(ArchivePolicy(conversion_timeout_seconds=30)),
                              output=ByteOutputPolicy(), root=directory)
        result = {"status": cover.status, "reason": cover.reason, "selected_member": cover.selected_member,
                  "format": cover.format, "entrypoint": "extract_cover"}
        if cover.status == "valid":
            assert (cover.selected_member, cover.format, cover.media_type, cover.suffix) == ("OPS/images/Front Cover.png", "JPEG", "image/jpeg", ".jpg"), cover
            assert cover.data is not None and validate_image(cover.data, max_pixels=256) == "JPEG"
            assert book.read_bytes() == before and {path.name for path in directory.iterdir()} == {"red.png", book.name}
            result.update({"status": "available", "source_retained": True, "sidecar_created": False})
    elif name == "image-similarity":
        from komga_cover_extractor.matching.cover_similarity import compare_cover_bytes
        data = (directory / "red.png").read_bytes()
        comparison = compare_cover_bytes(data, data, timeout=30)
        result = {"status": comparison.status, "reason": comparison.reason, "score": comparison.score, "entrypoint": "compare_cover_bytes"}
        if comparison.status == "matched":
            assert comparison.score == 1.0, comparison
            result["status"] = "available"
    elif name == "integrations":
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from komga_cover_extractor.integrations.bookwalker import parse_search_page
        from komga_cover_extractor.integrations.http import HttpClient
        content = b'<div class="book-list-area book-result-area book-result-area-1"><ul class="o-tile-list"><li class="o-tile"><div class="o-tile-book-info"><h2 class="a-tile-ttl">Fixture Volume 1</h2><a class="a-tile-thumb-img" href="https://global.bookwalker.jp/fixture/"></a><div class="a-tag-manga"></div></div></li></ul></div>'
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return
            def do_GET(self):
                calls.append((self.command, self.path))
                self.send_response(200 if self.path == "/prefix/search" else 404)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with HttpClient(f"http://127.0.0.1:{server.server_port}/prefix/", connect_timeout=1,
                            read_timeout=1, max_attempts=1) as client:
                response = client.request("GET", "search", read_only=True)
                try:
                    rows, pages = parse_search_page(response.content)
                finally:
                    response.close()
            assert rows == ({"original_title": "Fixture Volume 1", "url": "https://global.bookwalker.jp/fixture/",
                             "thumbnail": None, "book_type": "manga", "chapter": False},) and pages == 1, (rows, pages)
            assert calls == [("GET", "/prefix/search")], calls
            result = {"status": "available", "entrypoint": "HttpClient.request and parse_search_page",
                      "transport": "real HTTP to generated loopback server", "fixture": "synthetic HTML",
                      "calls": calls, "releases": list(rows), "pages": pages}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive(), "integration probe server was not reaped"
    elif name == "watch":
        from komga_cover_extractor.infrastructure.watching import close_observers, start_observer
        before = {thread.ident for thread in threading.enumerate()}
        observed = threading.Event()
        target = directory / "watch-observation.txt"
        observer = start_observer(directory, lambda event: observed.set() if event[1] == target else None, polling=True, interval=0.05)
        try:
            target.write_text("generated observation", encoding="utf-8")
            assert observed.wait(5), "polling observer did not emit the generated event"
        finally:
            failures = close_observers((observer,), timeout=5)
        assert not failures and not observer.is_alive()
        assert not [thread.name for thread in threading.enumerate() if thread.ident not in before]
        result = {"status": "available", "entrypoint": "start_observer", "generated_event_observed": True, "observer_reaped": True, "backend": "polling"}
    else:
        from komga_cover_extractor.application.convert import convert_book
        from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
        source = directory / "Capability v01.7z"
        if importlib.util.find_spec("py7zr") is None:
            source.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
        else:
            import py7zr
            with py7zr.SevenZipFile(source, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
                archive.writestr(b"generated conversion capability", "nested/content.txt")
        conversion = convert_book(source, root=directory, scratch_dir=directory, policy=ArchivePolicy(conversion_timeout_seconds=30))
        result = {"status": conversion.status, "reason": conversion.reason, "entrypoint": "convert_book", "member_manifest": list(conversion.manifest)}
        if conversion.status == "valid":
            import hashlib
            assert conversion.manifest == (("nested/content.txt", hashlib.sha256(b"generated conversion capability").hexdigest()),)
            assert source.exists() and conversion.artifact_path is not None
            import zipfile
            with zipfile.ZipFile(conversion.artifact_path) as archive:
                assert archive.testzip() is None
                assert {name: archive.read(name) for name in archive.namelist()} == {"nested/content.txt": b"generated conversion capability"}
            result["status"] = "available"
except Exception as error:
    status = getattr(error, "status", "missing_dependency" if isinstance(error, ImportError) else "failed")
    result = {"status": status, "reason": getattr(error, "reason", "optional_import_unavailable" if isinstance(error, ImportError) else type(error).__name__), "exception_type": type(error).__name__}
print(json.dumps(result, sort_keys=True))
'''

_RECOVERY_CRASH = r'''
import os
import sys
from pathlib import Path
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

root, state, book, image = map(Path, sys.argv[1:5])
plan = OperationPlan("installed-recovery-proof", "installed-interruption", (
    Operation("book-art", "publish", book.with_suffix(".png"), source=fingerprint(book, root),
              artifact=ArtifactSpec(content=image.read_bytes(), media_type="image/png")),
), (RootPermission(root),), "book_artwork")
def checkpoint(phase, member):
    if phase == "published" and member == "book-art":
        os._exit(86)
result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
raise SystemExit(result.exit_code or 1)
'''


def _manifest(root: Path) -> dict[str, tuple[int, str]]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<directory>",
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def _file_manifest(root: Path) -> dict[str, tuple[int, int, int, int, str]]:
    manifest = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            observed = path.stat()
            data = path.read_bytes()
            mtime = observed.st_mtime_ns
            if path == root / ".kce-transaction.lock":
                lock = json.loads(data)
                if (
                    set(lock) != {"format", "store_id", "owner"}
                    or lock["format"] != "kce-lock-v1"
                    or not isinstance(lock["store_id"], str)
                    or lock["owner"] is not None
                ):
                    raise ValueError("repeat proof encountered an unsettled or invalid root lock")
                mtime = 0
            manifest[str(path.relative_to(root))] = (
                observed.st_dev, observed.st_ino, observed.st_size, mtime,
                hashlib.sha256(data).hexdigest(),
            )
    return manifest


def _run(command: Sequence[str], *, cwd: Path, environ: dict[str, str], expected: int | None = 0) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command, cwd=cwd, env=environ, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=180)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if expected is not None and result.returncode != expected:
        raise ValueError(f"{Path(command[0]).name} returned {result.returncode}, expected {expected}\n{result.stdout[:8192]}\n{result.stderr[:8192]}")
    return result


def _json_run(command: Sequence[str], *, cwd: Path, environ: dict[str, str], expected: int = 0) -> dict[str, Any]:
    output = _run(command, cwd=cwd, environ=environ, expected=expected)
    payload = json.loads(output.stdout)
    if not isinstance(payload, dict) or payload.get("ok") is not (expected == 0):
        raise ValueError("installed CLI returned an unsuccessful or malformed JSON result")
    if "exit_code" in payload and payload["exit_code"] != expected:
        raise ValueError("installed CLI JSON exit status disagrees with its actual process exit")
    return payload


def _png(color: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = b"".join(b"\x00" + bytes(color) * 16 for _ in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def _book(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rgb in (("01.png", color), ("cover.png", (0, 0, 255))):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            archive.writestr(info, _png(rgb))
        metadata = zipfile.ZipInfo("ComicInfo.xml", date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(metadata, b"<ComicInfo><Publisher>Fixture Press</Publisher></ComicInfo>")


def _webp(color: tuple[int, int, int]) -> bytes:
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    with Image.new("RGB", (16, 16), color) as image:
        image.save(output, format="WEBP", quality=90)
    return output.getvalue()


def _check_color(path: Path, channel: int) -> None:
    from PIL import Image

    with Image.open(path) as image:
        if image.format != "WEBP" or image.size != (16, 16):
            raise ValueError("published artwork is not the requested decoded WebP image")
        rgb = image.convert("RGB").getpixel((8, 8))
        if not isinstance(rgb, tuple) or rgb[channel] < 180 or any(value > 40 for index, value in enumerate(rgb) if index != channel):
            raise ValueError("published artwork did not retain the lexical-first page's expected color")


def _environment(home: Path, interpreter: Path) -> dict[str, str]:
    environ = {
        "PATH": str(interpreter.parent) + os.pathsep + os.environ.get("PATH", os.defpath),
        "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "LANG": "C.UTF-8", "TZ": "UTC", "PYTHONUNBUFFERED": "1",
    }
    if "LD_LIBRARY_PATH" in os.environ:
        environ["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    return environ


def _installed(source_root: Path | None) -> tuple[Path, importlib.metadata.Distribution]:
    if sys.version_info[:2] != (3, 14):
        raise ValueError("this deployment proof requires the declared Python 3.14 interpreter")
    spec = importlib.util.find_spec(_PACKAGE)
    if spec is None or spec.origin is None:
        raise ValueError("the application wheel is not installed in this interpreter")
    origin = Path(spec.origin).resolve()
    if source_root is not None and origin.is_relative_to(source_root.resolve()):
        raise ValueError("package resolution came from the checkout instead of the installed wheel")
    distribution = importlib.metadata.distribution("komga-cover-extractor")
    direct = distribution.read_text("direct_url.json")
    if direct and json.loads(direct).get("dir_info", {}).get("editable") is True:
        raise ValueError("installed proof refuses an editable installation")
    owned_paths: set[Path] = set()
    for item in distribution.files or ():
        located = distribution.locate_file(item)
        if not isinstance(located, Path):
            raise ValueError("installed proof requires filesystem-backed distribution paths")
        owned_paths.add(located.resolve())
    if origin not in owned_paths:
        raise ValueError("resolved package is not owned by the installed distribution's file manifest")
    return origin, distribution


def _dependency_inventory(lock: Path, distribution: importlib.metadata.Distribution) -> dict[str, object]:
    def canonical_name(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    text = lock.read_text(encoding="utf-8")
    pins: dict[str, set[str]] = {}
    for line in text.splitlines():
        line = line.removesuffix("\\").strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;.+)?", line)
        if match is None:
            raise ValueError("dependency inventory requires a version-pinned uv requirements export")
        pins.setdefault(canonical_name(match[1]), set()).add(match[2])
    if not pins or "--hash=sha256:" not in text:
        raise ValueError("dependency inventory requires the hashed selected lock export")
    pins[canonical_name(distribution.metadata["Name"])] = {distribution.version}
    actual: dict[str, str] = {}
    for dependency in importlib.metadata.distributions():
        name = canonical_name(dependency.metadata["Name"])
        if name in actual:
            raise ValueError(f"isolated environment has duplicate installed distribution {name}")
        if dependency.version not in pins.get(name, set()):
            raise ValueError(f"installed dependency {name}=={dependency.version} is outside this selected lock export")
        actual[name] = dependency.version
    return {"status": "pass", "lock_export_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "installed_versions": dict(sorted(actual.items())),
            "meaning": "Every installed distribution is version-pinned in this selected lock export or is the tested application wheel. Inactive platform pins may remain uninstalled."}


def _module_sources(installed: Path) -> dict[str, Path]:
    modules = {}
    for path in sorted(installed.parent.rglob("*.py")):
        relative = path.relative_to(installed.parent).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        name = ".".join((_PACKAGE, *parts))
        modules[name] = path
    if _PACKAGE not in modules or _PACKAGE + ".cli" not in modules:
        raise ValueError("installed package lacks its expected Python module inventory")
    return modules


def _architecture_audit(modules: dict[str, Path]) -> dict[str, object]:
    graph: dict[str, set[str]] = {name: set() for name in modules}
    dependencies: dict[str, list[dict[str, object]]] = {name: [] for name in modules}
    pure_modules = {
        name for name in modules
        if any(name == _PACKAGE + "." + area or name.startswith(_PACKAGE + "." + area + ".")
               for area in ("domain", "parsing"))
    }
    imports: list[dict[str, object]] = []
    forbidden: list[dict[str, object]] = []
    dynamic_gaps: list[dict[str, object]] = []
    forbidden_external = {"requests", "urllib3", "http", "socket", "aiohttp", "httpx", "sqlite3"}
    forbidden_internal = tuple(_PACKAGE + "." + suffix for suffix in ("config", "legacy_settings", "bootstrap", "application", "infrastructure", "integrations", "cli", "compat"))

    def forbidden_dependency(target: str) -> bool:
        return target.split(".")[0] in forbidden_external or any(
            target == prefix or target.startswith(prefix + ".") for prefix in forbidden_internal
        )

    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        pure = name in pure_modules

        def record(target: str, node: ast.AST, deferred: bool) -> None:
            row: dict[str, object] = {"module": name, "dependency": target, "line": getattr(node, "lineno", None),
                                      "execution": "deferred" if deferred else "import-time"}
            imports.append(row)
            dependencies[name].append(row)
            if pure and forbidden_dependency(target):
                forbidden.append(row)
            if not deferred and target in modules and target != name:
                graph[name].add(target)

        def walk(node: ast.AST, deferred: bool = False) -> None:
            if isinstance(node, ast.If):
                condition = node.test
                if isinstance(condition, ast.Name) and condition.id == "TYPE_CHECKING" or isinstance(condition, ast.Attribute) and condition.attr == "TYPE_CHECKING":
                    for child in node.orelse:
                        walk(child, deferred)
                    return
                if isinstance(condition, ast.Compare) and isinstance(condition.left, ast.Name) and condition.left.id == "__name__" and any(isinstance(item, ast.Constant) and item.value == "__main__" for item in condition.comparators):
                    for child in node.orelse:
                        walk(child, deferred)
                    return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
                    if expression is not None:
                        walk(expression, deferred)
                for statement in node.body:
                    walk(statement, True)
                return
            if isinstance(node, ast.Lambda):
                for expression in (*node.args.defaults, *node.args.kw_defaults):
                    if expression is not None:
                        walk(expression, deferred)
                walk(node.body, True)
                return
            if isinstance(node, ast.Import):
                for alias in node.names:
                    record(alias.name, node, deferred)
                return
            if isinstance(node, ast.ImportFrom):
                target = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
                if target:
                    record(target, node, deferred)
                for alias in node.names:
                    candidate = target + "." + alias.name
                    if candidate in modules:
                        record(candidate, node, deferred)
                return
            if isinstance(node, ast.Call):
                function = node.func
                is_dynamic = isinstance(function, ast.Name) and function.id in {"__import__", "import_module"} or isinstance(function, ast.Attribute) and function.attr == "import_module"
                if is_dynamic:
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        target = node.args[0].value
                        record(importlib.util.resolve_name(target, package) if target.startswith(".") else target, node, deferred)
                    else:
                        row = {"module": name, "line": node.lineno, "reason": "nonliteral_dynamic_import"}
                        dynamic_gaps.append(row)
                        if pure:
                            forbidden.append(row)
            for child_node in ast.iter_child_nodes(node):
                walk(child_node, deferred)

        walk(tree)
    visited: set[str] = set()
    active: list[str] = []
    cycles: list[list[str]] = []

    def visit(name: str) -> None:
        if name in active:
            cycle = active[active.index(name):] + [name]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if name in visited:
            return
        active.append(name)
        for target in sorted(graph[name]):
            visit(target)
        active.pop()
        visited.add(name)

    for name in sorted(graph):
        visit(name)
    for origin in sorted(pure_modules):
        pending: list[tuple[str, tuple[str, ...]]] = [(origin, (origin,))]
        traversed: set[str] = set()
        reported: set[str] = set()
        while pending:
            current, chain = pending.pop()
            if current in traversed:
                continue
            traversed.add(current)
            for row in dependencies[current]:
                target = str(row["dependency"])
                if len(chain) > 1 and forbidden_dependency(target) and target not in reported:
                    forbidden.append({"module": origin, "dependency": target,
                                      "path": [*chain, target], "execution": "transitive"})
                    reported.add(target)
                if target in modules:
                    pending.append((target, (*chain, target)))
    return {"acceptance_id": "B08", "status": "pass" if not forbidden and not cycles else "fail",
            "modules": len(modules), "import_edges": imports, "forbidden_pure_dependencies": forbidden,
            "import_time_cycles": cycles, "nonliteral_dynamic_imports": dynamic_gaps,
            "method": "AST of installed wheel modules, excluding TYPE_CHECKING and executable main guards; direct/transitive deferred dependencies checked for pure boundaries, only import-time edges checked for cycles"}


def _cold_imports(
    modules: dict[str, Path], interpreter: str, root: Path, environ: dict[str, str], *,
    from_paths: bool = False,
) -> dict[str, object]:
    results = []
    for index, name in enumerate(modules):
        directory = root / f"cold-{index}"
        home = directory / "home"
        home.mkdir(parents=True)
        environment = {**environ, **_environment(home, Path(interpreter))}
        before = _manifest(directory)
        try:
            command = [interpreter, "-I", "-B", "-c", _COLD_IMPORT, name]
            if from_paths:
                command.append(str(modules[name]))
            output = _run(command, cwd=directory, environ=environment, expected=None)
            try:
                result = json.loads(output.stdout)
                if not isinstance(result, dict) or result.get("module") != name:
                    raise ValueError("import child returned the wrong module or shape")
                if result.get("status") == "pass" and Path(result["origin"]).resolve() != modules[name].resolve():
                    raise ValueError("import child loaded a different source origin")
            except ValueError:
                result = {"module": name, "status": "fail", "reason": "unstructured_import_output",
                          "stdout": output.stdout[:4096]}
            if output.returncode != 0 or output.stderr or _manifest(directory) != before:
                result.update({"status": "fail", "child_exit_code": output.returncode,
                               "external_stderr": output.stderr[:4096],
                               "directory_changed": _manifest(directory) != before})
        except (OSError, subprocess.TimeoutExpired) as error:
            result = {"module": name, "status": "fail", "reason": "import_process_failed",
                      "exception_type": type(error).__name__}
        results.append(result)
    return {"acceptance_id": "N13" if from_paths else "B05",
            "status": "pass" if all(row["status"] == "pass" for row in results) else "fail",
            "modules": len(results), "results": results,
            "guards": ["write audit events", "fixture reads/scans", "settings import/read", "argument parsing", "stdin", "client constructor profiling", "socket connect/bind/DNS", "thread/process start", "exec", "SQLite connect", "captured output", "filesystem manifest", "exact source origin"]}


def _configure(root: Path, **overrides: Any) -> Path:
    import tomli_w

    for name in ("home", "library", "downloads"):
        (root / name).mkdir(parents=True, exist_ok=True)
    values = {
        "paths": [str(root / "library")], "download_folders": [str(root / "downloads")],
        "state_dir": str(root / "state"), "log_dir": str(root / "logs"),
        "output_covers_as_webp": True, "compress_image_option": False,
        "compare_detected_cover_to_blank_images": False, "match_through_image_similarity": False,
        "match_through_identifiers": False, "log_to_file": False, "manual_rename": False,
        "manual_delete": False, "restructure_when_renaming": False,
        "move_lone_files_to_similar_folder": False,
        "replace_series_name_in_file_name_with_similar_folder_name": False,
        "add_volume_one_number_to_one_shots": False, "add_issue_number_to_manga_file_name": False,
        "search_and_add_premium_to_file_name": False, "add_publisher_name_to_file_name_when_renaming": False,
        "move_release_group_to_end_of_file_name": False, "preferred_volume_renaming_format": "v",
        "preferred_chapter_renaming_format": "c", "library_types": [], "ignored_folder_names": [],
        "unacceptable_keywords": [], "ranked_keywords": [{"name": "Digital", "score": 3, "file_type": "both"}],
        "required_similarity_score": 1.0, "chapter_support_toggle": True,
        "correct_file_extensions_toggle": False, "convert_to_cbz_toggle": False,
        "delete_unacceptable_files_toggle": False, "delete_chapters_from_downloads_toggle": False,
        "rename_files_in_download_folders_toggle": False,
        "create_folders_for_items_in_download_folder_toggle": False,
        "rename_dirs_in_download_folder_toggle": False, "check_for_duplicate_volumes_toggle": False,
        "check_for_existing_series_toggle": False, "extract_covers_toggle": False,
        "check_for_missing_volumes_toggle": False, "generate_release_group_list_toggle": False,
        "move_series_to_correct_library_toggle": False, "move_new_series_to_library_toggle": False,
        "copy_existing_volume_covers_toggle": False, "watchdog_toggle": False,
        "auto_classify_watchdog_paths": False, "uncheck_non_qbit_upgrades_toggle": False,
        "bookwalker_check": False, "send_scan_request_to_komga_libraries_toggle": False,
        "discord_webhook_url": [], "bookwalker_webhook_urls": [], "new_volume_webhook": "",
    }
    values.update(overrides)
    path = root / "config.toml"
    path.write_text(tomli_w.dumps(values), encoding="utf-8")
    return path


def _journal(state: Path, operation_id: str | None = None) -> dict[str, Any]:
    database = state / "state.sqlite3"
    if not database.exists():
        return {"operations": [], "members": [], "events": [], "jobs": []}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        where, parameters = (" WHERE operation_id=?", (operation_id,)) if operation_id else ("", ())
        return {
            "operations": [dict(row) for row in connection.execute("SELECT operation_id,phase FROM operations" + where + " ORDER BY operation_id", parameters)],
            "members": [dict(row) for row in connection.execute("SELECT operation_id,member_id,kind,status,destination FROM members" + where + " ORDER BY operation_id,ordinal", parameters)],
            "events": [dict(row) for row in connection.execute("SELECT event_id,operation_id,member_id,event_type,state FROM outbox" + where + " ORDER BY event_id", parameters)],
            "jobs": [dict(row) for row in connection.execute("SELECT job_id,kind,state FROM jobs ORDER BY job_id")],
        }


def _receipt(command: str, payload: dict[str, Any], root: Path) -> dict[str, Any]:
    return {"command": command, "exit_code": payload.get("exit_code", 0), "counts": payload.get("counts"),
            "decisions": payload.get("decisions", []), "plans": payload.get("plans", []),
            "missing_releases": payload.get("missing_releases", []), "journal": _journal(root / "state")}


def _assert_commit(payload: dict[str, Any], root: Path, groups: int) -> None:
    if payload["counts"]["committed"] != groups or payload["counts"]["failed"] != 0:
        raise ValueError("installed commit counts do not match the planned verified groups")
    journal = _journal(root / "state")
    if len(journal["operations"]) != groups or any(row["phase"] != "complete" for row in journal["operations"]):
        raise ValueError("installed workflow did not settle each operation in the actual journal")
    terminal = {"publish": "committed", "mkdir": "committed", "move": "complete",
                "quarantine": "complete", "rmdir": "complete"}
    operation_ids = {row["operation_id"] for row in journal["operations"]}
    members = journal["members"]
    member_keys = {(row["operation_id"], row["member_id"]) for row in members}
    if (
        {row["operation_id"] for row in members} != operation_ids
        or len(member_keys) != len(members)
        or any(row["status"] != terminal.get(row["kind"]) for row in members)
    ):
        raise ValueError("completed operations have missing, duplicate or unsettled journal members")
    local = [row for row in journal["events"] if row["event_type"] == "local_commit"]
    if (
        {(row["operation_id"], row["member_id"]) for row in local} != member_keys
        or len(local) != len(member_keys)
        or len({row["event_id"] for row in local}) != len(local)
    ):
        raise ValueError("verified local commit events are missing or duplicated")


def _capabilities(
    root: Path, interpreter: str, environ: dict[str, str], expected: Sequence[str], *,
    isolated: bool = False,
) -> dict[str, Any]:
    states = {name: importlib.util.find_spec(name) is not None for name in _OPTIONAL_MODULES}
    required = set(_EXTRAS) if "all" in expected else set(expected)
    if isolated:
        allowed = {name for extra in required for name in _EXTRAS[extra]}
        if "image-similarity" in required:
            allowed.add("PIL")
        leaked = [name for name, available in states.items() if available and name not in allowed]
        if leaked:
            raise ValueError(f"selected core/extra environment leaked unrelated optional modules {leaked}")
    for extra in required:
        missing = [name for name in _EXTRAS[extra] if not states[name]]
        if missing:
            raise ValueError(f"expected extra {extra} is missing its declared modules {missing}")
    parsed = _run([interpreter, "-I", "-B", "-c", _PURE_PARSE], cwd=root, environ=environ)
    pure = json.loads(parsed.stdout)
    if parsed.stderr:
        raise ValueError("pure installed parser emitted diagnostics")
    results = {}
    for extra in _EXTRAS:
        directory = root / extra
        directory.mkdir()
        (directory / "red.png").write_bytes(_png((255, 0, 0)))
        output = _run([interpreter, "-I", "-B", "-c", _CAPABILITY_ACTION, extra, str(directory)], cwd=root, environ=environ)
        result = json.loads(output.stdout)
        result["declared_modules"] = {name: states[name] for name in _EXTRAS[extra]}
        wanted = "available" if all(states[name] for name in _CAPABILITY_MODULES[extra]) else "missing_dependency"
        if output.stderr or result.get("status") != wanted:
            raise ValueError(f"{extra} capability proof expected {wanted}: {result}; {output.stderr[:4096]}")
        if extra in required and result["status"] != "available":
            raise ValueError(f"the selected extra {extra} did not provide its owning capability: {result}")
        results[extra] = result
    return {"acceptance_id": "D04", "status": "pass", "core_parser": pure,
            "selected_extras": sorted(required), "module_availability": states, "extras": results,
            "meaning": "Concrete owning-capability outcomes and targeted missing-dependency results for this interpreter. Clean environment isolation is checked against its selected lock export when supplied."}


def _normalize(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(root), "$FIXTURE")
    if isinstance(value, dict):
        return {key: _normalize(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, root) for item in value]
    return value


def _core_commands(root: Path, command: list[str], environ: dict[str, str]) -> list[dict[str, Any]]:
    receipts: list[dict[str, object]] = []
    before = _manifest(root)
    doctor = _json_run([*command, "doctor"], cwd=root, environ=environ)
    resources = doctor.get("resources", {})
    for resource in ("blank_black.png", "blank_white.jpg"):
        if resources.get(resource, {}).get("present") is not True:
            raise ValueError("doctor did not find an installed blank-reference resource")
    if doctor.get("remote_probes", {}).get("status") != "not-probed":
        raise ValueError("doctor unexpectedly performed a remote probe")
    inspected = _json_run([*command, "inspect", "--json"], cwd=root, environ=environ)
    if inspected["releases"] != [] or inspected["counts"]["committed"] != 0:
        raise ValueError("installed empty-root inspection fabricated a release or commit")
    if _manifest(root) != before:
        raise ValueError("doctor or empty inspection created persistent state")
    for number in (1, 3):
        _book(root / "library/Series" / f"Series v{number:02}.cbz", (255, 0, 0))
    before = _manifest(root)
    gaps = _json_run([*command, "missing-releases", "--json"], cwd=root, environ=environ)
    reports = gaps["missing_releases"]
    if len(reports) != 1 or reports[0]["missing_volumes"] != [2] or reports[0]["observed_volumes"] != [1, 3] or reports[0]["series"] != "Series":
        raise ValueError("installed local-gap command did not report the literal volume-two gap")
    if _manifest(root) != before:
        raise ValueError("local gap reporting mutated the fixture")
    receipts.extend((_receipt("inspect-empty", inspected, root), _receipt("missing-releases", gaps, root)))
    receipts.append({"command": "doctor", "resources": resources, "remote_probes": doctor["remote_probes"]})
    has_covers = all(importlib.util.find_spec(name) is not None for name in _EXTRAS["covers"])
    book = root / "library/Series/Series v01.cbz"
    archive = _json_run([*command, "inspect", str(book), "--json"], cwd=root, environ=environ,
                        expected=0 if has_covers else 2)
    if has_covers:
        if [(row["series"], row["number"]["values"]) for row in archive["releases"]] != [("Series", ["1"])]:
            raise ValueError("installed archive inspection did not report the selected concrete release")
    elif not any(row["status"] == "missing_dependency" for row in archive["decisions"]):
        raise ValueError("missing archive capability was not reported as a targeted prerequisite")
    if _manifest(root) != before:
        raise ValueError("archive-inspection capability reporting mutated the fixture")
    receipts.append(_receipt("inspect-archive-capability", archive, root))
    return receipts


def _extract_commands(root: Path, command: list[str], environ: dict[str, str]) -> list[dict[str, Any]]:
    book = root / "library/Series/Series v01.cbz"
    inspected = _json_run([*command, "inspect", str(book), "--json"], cwd=root, environ=environ)
    if [(row["series"], row["number"]["values"]) for row in inspected["releases"]] != [("Series", ["1"])]:
        raise ValueError("installed inspection did not report the selected observed volume-one release")
    receipts = [_receipt("inspect-book", inspected, root)]
    before = _manifest(root)
    preview = _json_run([*command, "extract", str(book), "--json"], cwd=root, environ=environ)
    if preview["counts"]["committed"] != 0 or preview["counts"]["planned"] != 1:
        raise ValueError("extract preview did not describe its single unapplied artwork group")
    if _manifest(root) != before or (root / "state").exists() or (root / "logs").exists():
        raise ValueError("read-only extraction created state, logs or changed bytes")
    receipts.append(_receipt("extract-preview", preview, root))
    applied = _json_run([*command, "extract", str(book), "--apply", "--json"], cwd=root, environ=environ)
    _assert_commit(applied, root, 1)
    artwork = book.with_suffix(".webp")
    _check_color(artwork, 0)
    if hashlib.sha256(book.read_bytes()).hexdigest() != before[str(book.relative_to(root))][1]:
        raise ValueError("extract apply changed or removed its source archive")
    receipts.append(_receipt("extract-apply", applied, root))
    after = _file_manifest(root / "library")
    journal = _journal(root / "state")
    repeated = _json_run([*command, "extract", str(book), "--apply", "--json"], cwd=root, environ=environ)
    if repeated["counts"]["committed"] != 0 or _file_manifest(root / "library") != after or _journal(root / "state")["events"] != journal["events"]:
        raise ValueError("unchanged input was rewritten or emitted another event")
    receipts.append(_receipt("extract-repeat", repeated, root))
    parent_mtime = book.parent.stat().st_mtime_ns
    _book(book, (0, 255, 0))
    if book.parent.stat().st_mtime_ns != parent_mtime:
        raise ValueError("the in-place replacement fixture unexpectedly changed directory mtime")
    updated = _json_run([*command, "extract", str(book), "--apply", "--json"], cwd=root, environ=environ)
    if updated["counts"]["committed"] != 1:
        raise ValueError("in-place changed input was not re-evaluated")
    _check_color(artwork, 1)
    receipts.append(_receipt("extract-in-place-change", updated, root))
    return receipts


def _organizer_commands(
    root: Path, interpreter: str, environ: dict[str, str], *, command_name: str,
    launcher: Path | None = None,
) -> list[dict[str, Any]]:
    library_roots = [str(root / "library")]
    if command_name == "run":
        library_roots.append(str(root / "downloads"))
    config = _configure(root, rename_files_in_download_folders_toggle=True,
                        create_folders_for_items_in_download_folder_toggle=True,
                        extract_covers_toggle=command_name == "run", check_for_missing_volumes_toggle=command_name == "run",
                        paths=library_roots if launcher is None else [],
                        download_folders=[str(root / "downloads")] if launcher is None else [],
                        output_covers_as_webp=launcher is None)
    source = root / "downloads/Series Volume 1.cbz"
    _book(source, (255, 0, 0))
    original = source.read_bytes()
    sidecars = {".png": _png((255, 0, 0)), ".webp": _webp((0, 0, 255))}
    if command_name == "organize":
        for suffix, data in sidecars.items():
            source.with_suffix(suffix).write_bytes(data)
    custom = root / "library/Custom/poster.png"
    custom.parent.mkdir()
    custom.write_bytes(_png((0, 255, 0)))
    note = custom.parent / ".notes"
    note.write_bytes(b"unrelated user note")
    label = command_name if launcher is None else "legacy-rootapp"
    if launcher is None:
        command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), command_name, "--json"]
    else:
        command = [interpreter, str(launcher), "--config", str(config),
                   "-p", str(root / "library"), "-p", str(root / "downloads"), "-df", str(root / "downloads"),
                   "--output_covers_as_webp", "true", "-c", "false", "-cq", "40"]
    before = _manifest(root)
    preview = _json_run(command, cwd=root, environ=environ)
    if preview["counts"]["committed"] != 0 or preview["counts"]["planned"] != 1 or _manifest(root) != before:
        raise ValueError(f"{label} preview did not preserve its concrete single-group fixture")
    target = root / "downloads/Series/Series v01.cbz"
    operations = [member for plan in preview["plans"] for member in plan["operations"]]
    if not any(member["kind"] == "move" and member["destination"] == str(target) for member in operations):
        raise ValueError("canonical workflow did not plan the expected rename and grouping destination")
    receipts = [_receipt(label + "-preview", preview, root)]
    applied = _json_run([*command, "--apply"], cwd=root, environ=environ)
    _assert_commit(applied, root, 1)
    if target.read_bytes() != original or source.exists() or custom.read_bytes() != _png((0, 255, 0)) or note.read_bytes() != b"unrelated user note":
        raise ValueError("canonical organizer lost book bytes or unrelated custom artwork")
    if command_name == "organize":
        for suffix, data in sidecars.items():
            if target.with_suffix(suffix).read_bytes() != data or source.with_suffix(suffix).exists():
                raise ValueError("organizer did not preserve both associated sidecars through the grouped move")
    else:
        _check_color(target.with_suffix(".webp"), 0)
    receipts.append(_receipt(label + "-apply", applied, root))
    after = _file_manifest(root / "downloads")
    events = _journal(root / "state")["events"]
    repeated = _json_run([*command, "--apply"], cwd=root, environ=environ)
    if repeated["counts"]["committed"] != 0 or _file_manifest(root / "downloads") != after or _journal(root / "state")["events"] != events:
        raise ValueError("canonical repeated workflow republished or duplicated events")
    receipts.append(_receipt(label + "-repeat", repeated, root))
    return receipts


def _collision_commands(root: Path, interpreter: str, environ: dict[str, str]) -> list[dict[str, Any]]:
    config = _configure(root, rename_files_in_download_folders_toggle=True)
    source = root / "downloads/Series Volume 1.cbz"
    target = source.with_name("Series v01.cbz")
    _book(source, (255, 0, 0))
    _book(target, (0, 255, 0))
    before = _manifest(root)
    command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "organize"]
    receipts = []
    for flags in ((), ("--apply",)):
        result = _json_run([*command, *flags, "--json"], cwd=root, environ=environ, expected=3)
        if result["counts"]["committed"] != 0 or not any(row["status"] in {"conflict", "review_required", "ambiguous"} for row in result["decisions"]):
            raise ValueError("different-byte output collision was not an explicit unresolved decision")
        if _manifest(root) != before:
            raise ValueError("organizer collision changed either original or initialized persistent state")
        receipts.append(_receipt("organize-collision-apply" if flags else "organize-collision-preview", result, root))
    return receipts


def _rename_list_commands(root: Path, interpreter: str, environ: dict[str, str]) -> list[dict[str, Any]]:
    config = _configure(root)
    source = root / "legacy-logs"
    source.mkdir()
    contents = {"release_groups.txt": b"Team Z\nTeam A\n", "publishers.txt": "Éditions Soleil\nAcme\n".encode(),
                "skipped_release_group_files.txt": b"Other v01.cbz\n", "skipped_publisher_files.txt": b"Other v01.epub\n"}
    for name, data in contents.items():
        (source / name).write_bytes(data)
    (source / "cached_paths.txt").write_bytes(b"/untrusted/library\n")
    command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "rename-lists", "--import-from", str(source)]
    before = _manifest(root)
    preview = _json_run([*command, "--json"], cwd=root, environ=environ)
    if preview["counts"]["committed"] != 0 or preview["counts"]["planned"] != 1 or _manifest(root) != before:
        raise ValueError("rename-list preview did not preserve the four bounded input files")
    receipts = [_receipt("rename-lists-preview", preview, root)]
    applied = _json_run([*command, "--apply", "--json"], cwd=root, environ=environ)
    _assert_commit(applied, root, 1)
    if any((root / "state" / name).read_bytes() != data for name, data in contents.items()) or (root / "state/cached_paths.txt").exists():
        raise ValueError("rename-list import changed literal order or trusted the obsolete path cache")
    if any((source / name).read_bytes() != data for name, data in contents.items()):
        raise ValueError("rename-list import modified its legacy input")
    receipts.append(_receipt("rename-lists-apply", applied, root))
    return receipts


def _recovery_commands(root: Path, interpreter: str, environ: dict[str, str]) -> list[dict[str, Any]]:
    config = _configure(root)
    book = root / "library/Series/Series v01.cbz"
    _book(book, (255, 0, 0))
    source_bytes = book.read_bytes()
    image = root / "red.png"
    image.write_bytes(_png((255, 0, 0)))
    crashed = _run([interpreter, "-I", "-c", _RECOVERY_CRASH, str(root / "library"), str(root / "state"), str(book), str(image)],
                   cwd=root, environ=environ, expected=86)
    if crashed.stdout or crashed.stderr or book.with_suffix(".png").read_bytes() != image.read_bytes():
        raise ValueError("real recovery child did not stop at the observed published checkpoint")
    interrupted = _journal(root / "state", "installed-interruption")
    if interrupted["events"] or interrupted["operations"][0]["phase"] != "publishing":
        raise ValueError("interrupted pre-commit operation fabricated a committed event")
    receipts = [{"command": "executor-real-interruption", "child_exit_code": crashed.returncode, "journal": interrupted}]
    command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "recover", "installed-interruption"]
    before = _manifest(root)
    preview = _json_run([*command, "--json"], cwd=root, environ=environ)
    if preview["counts"]["committed"] != 0 or _manifest(root) != before:
        raise ValueError("recovery preview changed the interrupted filesystem or journal")
    receipts.append(_receipt("recover-preview", preview, root))
    applied = _json_run([*command, "--apply", "--json"], cwd=root, environ=environ)
    _assert_commit(applied, root, 1)
    if book.read_bytes() != source_bytes or book.with_suffix(".png").read_bytes() != image.read_bytes():
        raise ValueError("fresh installed recovery lost the book or published artwork bytes")
    receipts.append(_receipt("recover-apply", applied, root))
    after = _file_manifest(root / "library")
    events = _journal(root / "state")["events"]
    repeated = _json_run([*command, "--apply", "--json"], cwd=root, environ=environ)
    if repeated["counts"]["committed"] != 0:
        raise ValueError("repeated fresh-process recovery reported another actual commit")
    if _file_manifest(root / "library") != after:
        raise ValueError("repeated fresh-process recovery changed published or retained file identities or bytes")
    if _journal(root / "state")["events"] != events:
        raise ValueError("repeated fresh-process recovery changed or duplicated a commit event")
    if book.read_bytes() != source_bytes or book.with_suffix(".png").read_bytes() != image.read_bytes():
        raise ValueError("repeated fresh-process recovery lost the original book or published artwork")
    receipts.append(_receipt("recover-repeat", repeated, root))
    return receipts


def _sevenzip(path: Path, entries: dict[str, bytes]) -> None:
    import py7zr

    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        for name, data in entries.items():
            archive.writestr(data, name)


def _convert_commands(root: Path, interpreter: str, environ: dict[str, str]) -> list[dict[str, Any]]:
    config = _configure(root)
    source = root / "library/Converted/Converted v01.7z"
    entries = {"nested/pages/01.png": _png((255, 0, 0)), "nested/ComicInfo.xml": b"<ComicInfo><Title>Converted</Title></ComicInfo>",
               "assets/fixture.txt": b"synthetic installed conversion fixture"}
    _sevenzip(source, entries)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "convert", str(source)]
    before = _manifest(root)
    preview = _json_run([*command, "--json"], cwd=root, environ=environ)
    if preview["counts"]["committed"] != 0 or preview["counts"]["planned"] != 1 or _manifest(root) != before:
        raise ValueError("conversion preview changed input or initialized persistent state")
    receipts = [_receipt("convert-preview", preview, root)]
    applied = _json_run([*command, "--apply", "--json"], cwd=root, environ=environ)
    _assert_commit(applied, root, 1)
    target = source.with_suffix(".cbz")
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None or {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")} != entries:
            raise ValueError("installed 7z conversion did not preserve the complete nested member bytes")
    retained = [path for path in (root / "library").rglob("*") if path.is_file() and path != target and hashlib.sha256(path.read_bytes()).hexdigest() == source_hash]
    if not retained:
        raise ValueError("installed conversion did not retain its original source bytes")
    receipts.append(_receipt("convert-apply", applied, root))
    after = _file_manifest(root / "library")
    events = _journal(root / "state")["events"]
    repeated = _json_run([interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "convert", str(target), "--apply", "--json"], cwd=root, environ=environ)
    if repeated["counts"]["committed"] != 0 or _file_manifest(root / "library") != after or _journal(root / "state")["events"] != events:
        raise ValueError("installed repeat conversion rewrote an already-converted book")
    receipts.extend((_receipt("convert-repeat", repeated, root),
                     {"command": "convert-content-proof", "source_sha256": source_hash,
                      "members": {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}}))
    return receipts


def _conversion_collision(root: Path, interpreter: str, environ: dict[str, str]) -> list[dict[str, Any]]:
    config = _configure(root)
    source = root / "library/Series/Series v01.7z"
    _sevenzip(source, {"nested/01.png": _png((255, 0, 0))})
    _book(source.with_suffix(".cbz"), (0, 255, 0))
    before = _manifest(root)
    command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "convert", str(source)]
    receipts = []
    for flags in ((), ("--apply",)):
        result = _json_run([*command, *flags, "--json"], cwd=root, environ=environ, expected=3)
        if result["counts"]["committed"] != 0 or not any(
            row["status"] == "conflict" and row["reason"] == "conversion_destination_exists"
            for row in result["decisions"]
        ):
            raise ValueError("native conversion collision was not the expected explicit conflict")
        if _manifest(root) != before:
            raise ValueError("native conversion collision changed either original or initialized journal")
        receipts.append(_receipt("convert-collision-apply" if flags else "convert-collision-preview", result, root))
    return receipts


def _loopback_qbit(
    root: Path, interpreter: str, environ: dict[str, str], *,
    launcher: Path | None = None, host: str = "http://127.0.0.1/prefix",
) -> list[dict[str, Any]]:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    prefix = urlsplit(host).path.rstrip("/") if "://" in host else ""
    torrent_hash = "a" * 40
    files = [{"name": "first/v01.cbz", "index": 4, "priority": 1, "size": 100, "progress": 0.0},
             {"name": "second/v01.cbz", "index": 9, "priority": 1, "size": 100, "progress": 0.0}]
    torrent = {"hash": torrent_hash, "name": "Fixture", "category": "books", "state": "pausedDL", "progress": 0.0,
               "added_on": 100, "total_size": 200, "save_path": "/remote", "content_path": "/remote/Fixture"}
    calls: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            self.handle_request()

        def do_POST(self) -> None:
            self.handle_request()

        def handle_request(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16384:
                self.send_error(413)
                return
            fields = {key: values[0] for key, values in parse_qs(self.rfile.read(length).decode(), keep_blank_values=True).items()}
            path = urlsplit(self.path).path
            endpoint = path.rsplit("/", 1)[-1]
            calls.append({"method": self.command, "path": path, "fields": fields})
            payload: Any
            status = 200
            if not path.startswith(f"{prefix}/api/v2/"):
                status, payload = 404, "wrong base path"
            elif endpoint == "login":
                payload = "Ok."
            elif self.headers.get("Cookie") != "SID=installed-proof":
                status, payload = 403, "authentication required"
            elif endpoint == "webapiVersion":
                payload = "2.11.0"
            elif endpoint == "info":
                payload = [dict(torrent)]
            elif endpoint == "files":
                payload = files
            elif endpoint == "filePrio" and fields == {"hash": torrent_hash, "id": "4", "priority": "0"}:
                files[0]["priority"] = 0
                payload = ""
            elif endpoint == "start" and fields == {"hashes": torrent_hash}:
                torrent["state"] = "downloading"
                payload = ""
            else:
                status, payload = 400, "unexpected or forbidden endpoint"
            body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            if endpoint == "login":
                self.send_header("Set-Cookie", f"SID=installed-proof; Path={prefix}/")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _configure(root, uncheck_non_qbit_upgrades_toggle=True, check_for_existing_series_toggle=True,
                            qbittorrent_ip=host, qbittorrent_port=str(server.server_port),
                            qbittorrent_username="fixture-user", qbittorrent_password="fixture-password",
                            qbittorrent_target_category="books", qbit_poll_interval_seconds=1,
                            qbit_reconnect_interval_seconds=1, http_connect_timeout_seconds=1, http_read_timeout_seconds=1,
                            paths=[str(root / "library")] if launcher is None else [],
                            download_folders=[str(root / "downloads")] if launcher is None else [])
        _book(root / "library/first/first v01 (Digital).cbz", (255, 0, 0))
        label = "legacy-qbit" if launcher is not None else "qbit" if prefix else "qbit-bare-host"
        if launcher is None:
            command = [interpreter, "-I", "-m", _PACKAGE, "--config", str(config), "qbit", "--json"]
        else:
            command = [interpreter, str(launcher), "--config", str(config),
                       "-p", str(root / "library"), "-df", str(root / "downloads")]
        before = _manifest(root)
        unsafe = _json_run(
            [*command, "--once", "--set", 'qbittorrent_ip="http://user:secret@127.0.0.1"'],
            cwd=root, environ=environ, expected=2,
        )
        if calls or _manifest(root) != before:
            raise ValueError("unsafe torrent endpoint contacted the server or changed persistent state")
        preview = _json_run([*command, "--once"], cwd=root, environ=environ)
        mutations = {"filePrio", "start", "resume", "delete"}
        planned = [row for row in preview["decisions"] if row["status"] == "planned"]
        if (len(planned) != 2 or preview["counts"]["committed"] != 0
                or not any(row["reason"] == "qbit_start_retained" for row in planned)
                or not any(row["candidates"] == [torrent_hash, "first/v01.cbz", "4"] for row in planned)):
            raise ValueError("installed torrent preview did not plan the concrete priority and retained-start decisions")
        if any(row["path"].rsplit("/", 1)[-1] in mutations for row in calls) or _manifest(root) != before:
            raise ValueError("installed torrent preview changed priority/state or initialized journal")
        receipts = [_receipt(label + "-unsafe-endpoint-refusal", unsafe, root),
                    _receipt(label + "-loopback-preview", preview, root)]
        applied = _json_run([*command, "--once", "--allow-remote"], cwd=root, environ=environ)
        actual = [row for row in calls if row["path"].rsplit("/", 1)[-1] in mutations]
        expected = [{"method": "POST", "path": f"{prefix}/api/v2/torrents/filePrio", "fields": {"hash": torrent_hash, "id": "4", "priority": "0"}},
                    {"method": "POST", "path": f"{prefix}/api/v2/torrents/start", "fields": {"hashes": torrent_hash}}]
        if actual != expected or [item["priority"] for item in files] != [0, 1] or torrent["state"] != "downloading":
            raise ValueError("installed torrent service changed the wrong equal-basename index or sent a forbidden mutation")
        jobs = _journal(root / "state")["jobs"]
        if applied["counts"]["committed"] != 2 or len(jobs) != 2 or {(row["kind"], row["state"]) for row in jobs} != {("qbit_action", "complete")}:
            raise ValueError("installed torrent remote actions were not verified and durably settled")
        receipts.append(_receipt(label + "-loopback-authorized", applied, root))
        repeated = _json_run([*command, "--once", "--allow-remote"], cwd=root, environ=environ)
        if (repeated["counts"]["committed"] != 0 or _journal(root / "state")["jobs"] != jobs
                or [row for row in calls if row["path"].rsplit("/", 1)[-1] in mutations] != expected):
            raise ValueError("installed torrent repeat replayed a settled action")
        receipts.append(_receipt(label + "-loopback-repeat", repeated, root))
        lifecycle = _lifecycle(command, root, environ, "kce qbit ready")
        lifecycle["command"] = label + "-readiness-shutdown"
        if [row for row in calls if row["path"].rsplit("/", 1)[-1] in mutations] != expected:
            raise ValueError("preview helper lifecycle sent another remote mutation")
        safe_calls = [row for row in calls if not row["path"].endswith("/login")]
        receipts.extend(({"command": label + "-loopback-wire", "transport": "real HTTP to generated loopback server", "calls": safe_calls}, lifecycle))
        return receipts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise ValueError("owned loopback server was not reaped")


def _lifecycle(command: list[str], root: Path, environ: dict[str, str], readiness: str) -> dict[str, Any]:
    import selectors

    process = subprocess.Popen(command, cwd=root, env=environ, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    stderr = bytearray()
    stdout = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stderr is not None and process.stdout is not None
    selector.register(process.stderr, selectors.EVENT_READ, stderr)
    selector.register(process.stdout, selectors.EVENT_READ, stdout)
    ready = False
    shutdown_started = 0.0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if len(key.data) > 4 * 1024 * 1024:
                    raise ValueError("lifecycle output exceeded the proof's bound")
            if readiness.encode() in stderr:
                ready = True
                break
        if not ready:
            raise ValueError(f"actual installed service did not become ready: {stderr.decode(errors='replace')[:4096]}")
        shutdown_started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        remaining_stdout, remaining_stderr = process.communicate(timeout=30)
        stdout.extend(remaining_stdout)
        stderr.extend(remaining_stderr)
        if process.returncode not in {0, 143}:
            raise ValueError(f"installed service returned unexpected shutdown status {process.returncode}")
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("installed service left a process in its owned process group")
        return {"command": "qbit-readiness-shutdown" if "qbit" in command else "watch-readiness-shutdown",
                "readiness": readiness, "child_exit_code": process.returncode,
                "shutdown_seconds": time.monotonic() - shutdown_started, "owned_process_group_reaped": True,
                "stderr": stderr.decode(errors="replace")[:4096],
                "stdout_records": [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]}
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        process.stdout.close()
        process.stderr.close()


def smoke_installed(
    source_root: Path | None = None, *, capabilities_only: bool = False,
    expected_extras: Sequence[str] = (), dependency_lock: Path | None = None,
) -> dict[str, object]:
    if source_root is not None:
        source_root = source_root.resolve()
    installed, distribution = _installed(source_root)
    inventory = _dependency_inventory(dependency_lock, distribution) if dependency_lock is not None else {"status": "not-requested"}
    direct = distribution.read_text("direct_url.json")
    wheel_sha256 = json.loads(direct).get("archive_info", {}).get("hashes", {}).get("sha256") if direct else None
    interpreter = str(Path(sys.executable).absolute())
    kce = Path(sys.executable).parent / "kce"
    if not kce.is_file():
        raise ValueError("the installed wheel did not provide its kce console entry point")
    modules = _module_sources(installed)
    architecture = _architecture_audit(modules)
    checks: list[str] = []
    receipts: list[dict[str, Any]] = []
    launchers: dict[str, Path] = {}
    launcher_audit: dict[str, object] = {"status": "not-requested"}
    limitations = ["No live libraries, credentials, webhooks or servers are contacted.",
                   "This proof checks only this installed interpreter and wheel. Separate clean environments are required to prove each published extra.",
                   "AST cycle analysis covers statically resolvable import-time edges. Nonliteral dynamic imports are disclosed separately and each module is cold imported independently.",
                   "The conversion fixture is actual generated 7z. Licensed native RAR and NAS/filesystem-platform proof belong to their separate release gates."]
    if dependency_lock is None:
        limitations.append("No selected dependency-lock inventory was supplied, so this invocation alone does not prove clean core/extra dependency isolation.")
    else:
        checks.append("installed-dependencies-match-selected-hashed-lock-export")
    with tempfile.TemporaryDirectory(prefix="kce-installed-proof-") as temporary:
        root = Path(temporary)
        if source_root is not None and root.is_relative_to(source_root.resolve()):
            raise ValueError("installed proof temporary directory must be outside the checkout")
        home = root / "home"
        home.mkdir()
        environ = _environment(home, Path(interpreter))
        cold = _cold_imports(modules, interpreter, root, environ)
        if source_root is not None and not capabilities_only:
            launchers = {
                "legacy_rootapp": source_root / "komga_cover_extractor.py",
                "legacy_qbit": source_root / "addons/qbit_torrent_unchecker/qbit_torrent_unchecker.py",
            }
            launcher_root = root / "launcher-imports"
            launcher_root.mkdir()
            launcher_audit = _cold_imports(launchers, interpreter, launcher_root, environ, from_paths=True)
            if launcher_audit["status"] != "pass":
                return {"schema_version": 1, "status": "fail", "python": sys.version.split()[0],
                        "installed_origin": str(installed), "launcher_import_audit": launcher_audit}
            checks.append("both-legacy-launchers-inert-under-filesystem-network-state-and-exec-guards")
        if architecture["status"] != "pass" or cold["status"] != "pass":
            return {"status": "fail", "python": sys.version.split()[0], "installed_origin": str(installed),
                    "architecture_audit": architecture, "cold_import_audit": cold, "checks": checks}
        checks.extend(("all-installed-modules-cold-imported", "installed-AST-pure-boundaries-and-cycle-audit"))
        capability_root = root / "capabilities"
        capability_root.mkdir()
        capabilities = _capabilities(capability_root, interpreter, environ, expected_extras,
                                     isolated=dependency_lock is not None)
        checks.append("core-and-owning-extra-capability-probes")
        module = [interpreter, "-I", "-m", _PACKAGE]
        _run([*module, "--help"], cwd=root, environ=environ)
        _run([str(kce), "--help"], cwd=root, environ=environ)
        checks.append("isolated-module-and-console-entrypoints")
        workspace = root / "core"
        config = _configure(workspace)
        command = [str(kce), "--config", str(config)]
        receipts.extend(_core_commands(workspace, command, environ))
        lists_root = root / "rename-lists"
        receipts.extend(_rename_list_commands(lists_root, interpreter, environ))
        checks.extend(("installed-resources", "core-preview-without-heavy-dependencies-or-state", "rename-list-safe-import", "local-gap-report"))
        available = capabilities["module_availability"]
        has_covers = all(available[name] for name in _EXTRAS["covers"])
        if not capabilities_only and has_covers:
            config = _configure(workspace, log_to_file=True)
            receipts.extend(_extract_commands(workspace, command, environ))
            receipts.extend(_organizer_commands(root / "organize", interpreter, environ, command_name="organize"))
            receipts.extend(_organizer_commands(root / "run", interpreter, environ, command_name="run"))
            receipts.extend(_collision_commands(root / "collision", interpreter, environ))
            receipts.extend(_recovery_commands(root / "recovery", interpreter, environ))
            checks.extend(("installed-inspect-extract-organize-fullrun-and-recover", "lexical-first-page-and-actual-webp-publication",
                           "source-retained", "unchanged-input-no-republication", "in-place-input-change-with-stable-directory-mtime",
                           "canonical-group-receipts-and-journal-events", "different-byte-collision-preserves-both",
                           "fresh-process-interruption-recovery-and-repeat"))
            if all(available[name] for name in _EXTRAS["conversion"]):
                receipts.extend(_convert_commands(root / "convert", interpreter, environ))
                receipts.extend(_conversion_collision(root / "convert-collision", interpreter, environ))
                checks.extend(("actual-installed-7z-to-cbz-nested-member-publication", "native-conversion-collision-preserves-both"))
            else:
                limitations.append("Canonical conversion commands were not run because the conversion extra is unavailable in this interpreter.")
            if all(available[name] for name in _EXTRAS["integrations"]):
                receipts.extend(_loopback_qbit(root / "qbit", interpreter, environ))
                receipts.extend(_loopback_qbit(root / "qbit-bare-host", interpreter, environ, host="127.0.0.1"))
                checks.append("actual-loopback-qbit-wire-identity-authorization-and-helper-lifecycle")
            else:
                limitations.append("Loopback qBittorrent CLI commands were not run because integrations are unavailable in this interpreter.")
        elif not capabilities_only:
            limitations.append("Archive-observing inspection/extraction/organizer/fullrun/recovery commands were not run because covers are unavailable. Core parsing and local gaps remain independently exercised.")
        else:
            limitations.append("Capabilities-only mode intentionally omits the canonical archive-mutating workflow and loopback helper proofs.")
        if not capabilities_only and available["watchdog"]:
            watch_root = root / "watch"
            config = _configure(watch_root, watchdog_file_transferred_check_interval=1,
                                watchdog_discover_new_files_check_interval=1, watch_reconcile_interval_seconds=1)
            before = _manifest(watch_root)
            receipts.append(_lifecycle([*module, "--config", str(config), "watch", "--polling", "--json"],
                                       watch_root, environ, "kce watch ready"))
            if _manifest(watch_root) != before:
                raise ValueError("preview watcher readiness or shutdown wrote persistent state")
            checks.append("actual-preview-watch-readiness-and-bounded-shutdown")
            limitations.append("Installed watcher readiness uses the explicit polling backend on empty disposable roots, not a native/NAS event-parity claim.")
        if launchers:
            for wrapper in launchers.values():
                output = _run([interpreter, str(wrapper), "--help", "-p", str(workspace / "library")], cwd=root, environ=environ)
                if "usage:" not in output.stdout.lower():
                    raise ValueError("legacy launcher did not reach its installed argument adapter")
            checks.append("both-legacy-launchers-help-without-source-shadowing")
            if has_covers:
                receipts.extend(_organizer_commands(root / "legacy-rootapp", interpreter, environ,
                                                    command_name="run", launcher=launchers["legacy_rootapp"]))
                checks.append("legacy-rootapp-actual-preview-apply-repeat-with-forwarded-roots-and-webp")
                if all(available[name] for name in _EXTRAS["integrations"]):
                    receipts.extend(_loopback_qbit(root / "legacy-qbit", interpreter, environ,
                                                  launcher=launchers["legacy_qbit"]))
                    checks.append("both-operational-legacy-launchers-without-source-shadowing")
                else:
                    limitations.append("Operational add-on proof requires covers and integrations. It was not exercised in this interpreter.")
            else:
                limitations.append("Operational rootapp and add-on proof requires covers. Their guarded imports and help were exercised, not their operations.")
        receipts = _normalize(receipts, root)
    acceptance_ids = ["B05", "B08", "D02", "D04"]
    if "both-operational-legacy-launchers-without-source-shadowing" in checks:
        acceptance_ids.append("N13")
    return {"schema_version": 1, "status": "pass", "python": sys.version.split()[0],
            "package_version": distribution.version, "installed_origin": str(installed),
            "environment_prefix": sys.prefix, "wheel_sha256": wheel_sha256, "dependency_inventory": inventory,
            "package_source_sha256": hashlib.sha256(b"".join(path.read_bytes() for path in modules.values())).hexdigest(),
            "acceptance_ids": acceptance_ids, "checks": checks,
            "cold_import_audit": cold, "launcher_import_audit": launcher_audit,
            "architecture_audit": architecture, "capabilities": capabilities,
            "workflow_receipts": receipts, "limitations": limitations}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--capabilities-only", action="store_true")
    parser.add_argument("--expect-extra", choices=(*_EXTRAS, "all"), action="append", default=[])
    parser.add_argument("--dependency-lock", type=Path, help="hashed uv export for this environment's selected core/extra dependencies")
    args = parser.parse_args(argv)
    try:
        report = smoke_installed(args.source_root, capabilities_only=args.capabilities_only,
                                 expected_extras=args.expect_extra, dependency_lock=args.dependency_lock)
    except (OSError, ValueError, subprocess.TimeoutExpired, ImportError, sqlite3.Error) as error:
        print(json.dumps({"schema_version": 1, "status": "fail", "error": str(error)}))
        print(f"installed deployment proof failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

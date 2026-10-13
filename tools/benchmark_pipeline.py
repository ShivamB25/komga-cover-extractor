#!/usr/bin/env python3
"""Benchmark actual artwork-only full runs on generated, MIT-licensed ZIP books.

Run with an installed all+legacy CPython 3.14.6 interpreter, using -I. Each
counterbalanced pair gets fresh copies of the same books in the reviewed
characterization sandbox. The frozen main() and installed `kce run --apply`
perform discovery, decoding, sidecar publication and first-volume series art.
Other workflows and all remote integrations are disabled on both sides.

The identical read-only sitecustomize overlay observes every Python process.
It never replaces application work. Counts cover successful input ZipFile
constructions, bytes returned by ZipExtFile.read, first successful loads of
Image.open objects, and directory enumeration calls beneath /work/library.
Decoded bytes mean RGB pixel-plane bytes, not compressed input or allocation.
Imports, instrumentation and the modern journal/worker safety costs remain
part of the measurement. Fixture setup and independent validation are outside
the application timer. RSS is explicitly per-process, never tree-total RSS.

This is a reproducible workload comparison, not a whole-application speedup
claim. The JSON includes every trial, output proofs, measurement definitions,
provenance, policy, spread, failures and limitations. The tool fails rather
than reporting fast skipped work, stale installations or partial outputs.
"""
from __future__ import annotations

import argparse
import ast
import atexit
import base64
import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
import platform
import re
import resource
import shutil
import sqlite3
import stat
import statistics
import sys
import sysconfig
import tempfile
import time
import tomllib
import weakref
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Never, TypedDict, cast

_FROZEN_HASHES = {
    "komga_cover_extractor.py": "00e507f11af2b6078f3c374ac0be95908ca44d89d5b93b77c5e299a84208c23e",
    "settings.py": "b4d40983a89046fec3a48f2ac7694b402baaba4622fdbf513b88497c1a71c454",
    "tests.py": "a4c1f06298a932e0e67a901a23d03c7c70aff8c1c779c2892419fe11dc1d8de1",
    "manga_novel_dataset.csv": "a8c196f729286940dbbb6ff8efe4edfb3267187fbc71a1023973de65d8dcc7e7",
    "blank_black.png": "9817ef06dd330c18d32d278fcbab6d15a71c0987e5533344c806653dc8854e68",
    "blank_white.jpg": "0618418a1b5048b63778f7b452ee15fbb77e391752daa761608263b77389655a",
    "LICENSE.md": "33525226db1a18a3abca8f36e2d407812f08a71046ff76cf969c57a37840a40e",
}
_SOURCE_POLICY_KEYS = (
    "volume_keywords", "chapter_keywords", "exclusion_keywords", "subtitle_exclusion_keywords",
    "cover_patterns", "series_cover_file_names", "average_chapter_image_count",
    "blank_cover_required_similarity_score", "publisher_similarity_score",
    "release_group_similarity_score", "required_matching_percentage", "short_word_filter_percentage",
    "match_through_image_similarity", "required_image_similarity_score", "manual_delete",
    "copy_existing_volume_covers_toggle", "move_series_to_correct_library_toggle",
    "move_new_series_to_library_toggle", "zfill_volume_int_value", "zfill_volume_float_value",
    "zfill_chapter_int_value", "zfill_chapter_float_value",
)
_FEATURE_KEYS = (
    "correct_file_extensions_toggle", "convert_to_cbz_toggle", "delete_unacceptable_files_toggle",
    "delete_chapters_from_downloads_toggle", "rename_files_in_download_folders_toggle",
    "create_folders_for_items_in_download_folder_toggle", "rename_dirs_in_download_folder_toggle",
    "check_for_duplicate_volumes_toggle", "extract_covers_toggle", "check_for_existing_series_toggle",
    "check_for_missing_volumes_toggle", "cache_each_root_for_each_path_in_paths_at_beginning_toggle",
    "send_scan_request_to_komga_libraries_toggle", "uncheck_non_qbit_upgrades_toggle",
    "generate_release_group_list_toggle", "bookwalker_check", "watchdog_toggle",
)
_SETTING_POLICY_KEYS = (
    "chapter_support_toggle", "extract_chapter_covers", "compare_detected_cover_to_blank_images",
    "use_latest_volume_cover_as_series_cover", "ignored_folder_names", "ranked_keywords",
    "exception_keywords", "required_similarity_score", "manual_rename", "log_to_file",
    "compress_image_option", "image_quality", "output_covers_as_webp",
)
_LEGACY_ARGV = [
    "/repo/komga_cover_extractor.py", "-p", "/work/library", "-c", "true", "-cq", "40",
    "--output_covers_as_webp", "false", "-ltf", "false", "-bwc", "false",
]
_MODERN_ARGV = ["--config", "/work/config.toml", "run", "--apply", "--json"]
_METRICS = Path("/work/benchmark-metrics")
_ACTIVE = _METRICS / "active"
_CAPTURE_LIMIT = 32 * 1024 * 1024
_WIDTH, _HEIGHT = 128, 192
_finish_metrics: Callable[[], None] | None = None


class BookFixture(TypedDict):
    path: str
    archive_sha256: str
    archive_bytes: int
    members: dict[str, str]
    selected_member: str
    first_pixel: list[int]
    source_pixels_sha256: str


class OutputFixture(TypedDict):
    path: str
    sha256: str
    pixels_sha256: str
    bytes: int


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _load_tool(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load reviewed tool {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _assert_runtime() -> None:
    if (sys.platform != "linux" or platform.python_implementation() != "CPython"
            or sys.version_info[:3] != (3, 14, 6) or platform.machine() != "x86_64"):
        raise ValueError("this benchmark requires Linux x86_64 CPython 3.14.6")
    if getattr(sys, "_is_gil_enabled", lambda: False)() is not True:
        raise ValueError("this benchmark requires the ordinary GIL build")
    if not sys.flags.isolated or sys.prefix == sys.base_prefix:
        raise ValueError("run the benchmark with -I in a clean installed all+legacy virtual environment")


def _runtime() -> dict[str, object]:
    return {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: None)(),
        "machine": platform.machine(), "kernel": platform.release(), "isolated": bool(sys.flags.isolated),
        "cpu_count": os.cpu_count(),
        "dependencies": dict(sorted((distribution.metadata["Name"], distribution.version)
                                    for distribution in importlib.metadata.distributions()
                                    if distribution.metadata["Name"])),
    }


def _installed_package(source_root: Path | None) -> dict[str, Any]:
    spec = importlib.util.find_spec("komga_cover_extractor")
    if spec is None or spec.origin is None:
        raise ValueError("install the built application wheel into this interpreter")
    origin = Path(spec.origin).resolve(strict=True)
    purelib = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    if not origin.is_relative_to(purelib) or origin.is_relative_to(Path("/repo")):
        raise ValueError("the application did not resolve inside the installed environment")
    if source_root is not None and origin.is_relative_to(source_root):
        raise ValueError("source-tree application imports are forbidden")
    distribution = importlib.metadata.distribution("komga-cover-extractor")
    direct = distribution.read_text("direct_url.json")
    if direct:
        provenance = json.loads(direct)
        if provenance.get("dir_info") is not None or not provenance.get("url", "").endswith(".whl"):
            raise ValueError("editable or directory installations are forbidden; install a wheel")
    wheel_metadata = distribution.read_text("WHEEL")
    if not wheel_metadata or "Root-Is-Purelib: true" not in wheel_metadata:
        raise ValueError("the installed application lacks the supported wheel metadata")
    files = distribution.files
    if files is None:
        raise ValueError("the installed application lacks its wheel RECORD")
    hashes: dict[str, str] = {}
    for item in files:
        relative = PurePosixPath(str(item))
        if not relative.parts or relative.parts[0] != "komga_cover_extractor":
            continue
        if relative.suffix == ".pyc":
            continue
        path = Path(str(distribution.locate_file(item))).resolve(strict=True)
        if not path.is_relative_to(purelib) or not path.is_file() or item.hash is None or item.hash.mode != "sha256":
            raise ValueError(f"unsupported wheel file provenance for {relative}")
        digest = _file_sha(path)
        encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode("ascii").rstrip("=")
        if encoded != item.hash.value:
            raise ValueError(f"installed wheel RECORD mismatch for {relative}")
        hashes[str(relative)] = digest
        if source_root is not None:
            source = source_root / "src" / str(relative)
            if not source.is_file() or _file_sha(source) != digest:
                raise ValueError(f"installed application is stale or differs from the selected source for {relative}")
    if "komga_cover_extractor/__init__.py" not in hashes or origin != Path(str(distribution.locate_file("komga_cover_extractor/__init__.py"))).resolve():
        raise ValueError("resolved application is not owned by the installed wheel")
    if source_root is not None:
        expected = {path.relative_to(source_root / "src").as_posix()
                    for path in (source_root / "src/komga_cover_extractor").rglob("*.py")}
        if expected != {name for name in hashes if name.endswith(".py")}:
            raise ValueError("installed wheel does not contain the complete current application")
    for name in ("blank_black.png", "blank_white.jpg"):
        if hashes.get(f"komga_cover_extractor/resources/{name}") != _FROZEN_HASHES[name]:
            raise ValueError(f"installed blank resource provenance differs for {name}")
    return {"origin": str(origin), "version": distribution.version, "files": hashes,
            "files_sha256": _sha(_canonical(hashes)), "wheel_metadata_sha256": _sha(wheel_metadata.encode()),
            "direct_url": json.loads(direct) if direct else None}


def _frozen_hashes(root: Path) -> dict[str, str]:
    result = {name: _file_sha(root / name) for name in _FROZEN_HASHES}
    if result != _FROZEN_HASHES:
        raise ValueError("frozen source, settings, corpus, test, resource or license provenance mismatch")
    return result


def _dependencies(source_root: Path) -> dict[str, str]:
    project = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((source_root / "uv.lock").read_text(encoding="utf-8"))
    def normalize(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    requirements = (project["project"]["dependencies"]
                    + project["project"]["optional-dependencies"]["all"]
                    + project["dependency-groups"]["legacy"])
    locked: dict[str, set[str]] = {}
    for package in lock["package"]:
        locked.setdefault(normalize(package["name"]), set()).add(package["version"])
    versions: dict[str, str] = {}
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if match is None:
            raise ValueError("unsupported dependency declaration")
        name = normalize(match.group())
        candidates = locked.get(name, set())
        version = importlib.metadata.version(name)
        if candidates != {version}:
            raise ValueError(f"all+legacy dependency {name} differs from the selected lock")
        versions[name] = version
    if project["project"]["version"] != importlib.metadata.version("komga-cover-extractor"):
        raise ValueError("installed application version differs from pyproject.toml")
    return versions


def _policy(source_root: Path) -> dict[str, Any]:
    fixture_root = source_root / "tests/fixtures/legacy"
    probe = _load_tool(source_root / "tools/legacy_probe.py", "_pipeline_benchmark_safe_settings")
    safe = probe.extract_safe_settings(fixture_root / "settings.py")
    if not safe.ok:
        raise ValueError("the frozen settings cannot be decoded by the reviewed AST boundary")
    migration = importlib.import_module("komga_cover_extractor.legacy_settings")
    configuration = importlib.import_module("komga_cover_extractor.config")
    overrides = migration.migrate_settings((fixture_root / "settings.py").read_text(encoding="utf-8"))
    literals: dict[str, Any] = {}
    for node in ast.parse((fixture_root / "komga_cover_extractor.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _SOURCE_POLICY_KEYS:
                    value = ast.literal_eval(node.value)
                    if target.id in literals and literals[target.id] != value:
                        raise ValueError(f"ambiguous frozen policy assignment for {target.id}")
                    literals[target.id] = value
    if set(literals) != set(_SOURCE_POLICY_KEYS):
        raise ValueError("the frozen source lacks the complete selected policy")
    retired = {
        "short_word_filter_percentage": literals.pop("short_word_filter_percentage"),
        "cache_each_root_for_each_path_in_paths_at_beginning_toggle":
            safe.values["cache_each_root_for_each_path_in_paths_at_beginning_toggle"],
    }
    overrides.update(literals)
    overrides.update({
        "paths": ["/work/library"], "download_folders": [], "state_dir": "/work/state",
        "log_dir": "/work/logs", "compress_image_option": True, "image_quality": 40,
        "output_covers_as_webp": False, "bookwalker_check": False, "watchdog_toggle": False,
        "discord_webhook_url": [], "new_volume_webhook": "", "bookwalker_webhook_urls": [],
    })
    for name in _FEATURE_KEYS:
        if name in {"bookwalker_check", "watchdog_toggle"}:
            continue
        if safe.values.get(name) is not (name == "extract_covers_toggle"):
            raise ValueError(f"the frozen benchmark feature selection differs for {name}")
    config = configuration.validate_config(overrides, base_dir=Path("/work"))
    values = config.as_dict(redact=False)
    common = {name: values[name] for name in (*_SOURCE_POLICY_KEYS, *_FEATURE_KEYS, *_SETTING_POLICY_KEYS)
              if name not in retired}
    tomli_w = importlib.import_module("tomli_w")
    return {"shared": common, "sha256": _sha(_canonical(common)), "modern_toml": tomli_w.dumps(overrides),
            "legacy_argv": _LEGACY_ARGV, "modern_argv": _MODERN_ARGV, "legacy_retired": retired,
            "enabled_workflows": ["extract_covers_toggle"], "generated_series_art": ["cover.jpg"]}


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _fixtures(size: int, bundle: Path) -> dict[str, Any]:
    image_module = importlib.import_module("PIL.Image")
    books: list[BookFixture] = []
    outputs: list[OutputFixture] = []
    source_bytes = 0
    with zipfile.ZipFile(bundle, "x") as outer:
        for index in range(size):
            series_index, volume = divmod(index, 10)
            label = "".join(chr(65 + series_index // (26 ** power) % 26) for power in (3, 2, 1, 0))
            series = f"Benchmark {label}"
            relative = f"{series}/{series} v{volume + 1:02}.cbz"
            inner_buffer = io.BytesIO()
            member_hashes: dict[str, str] = {}
            selected_pixels = b""
            jpeg = b""
            jpeg_pixels = b""
            with zipfile.ZipFile(inner_buffer, "w") as inner:
                for member, phase in (("pages/01.png", 64), ("pages/02.png", 128), ("pages/cover.png", 224)):
                    pixels = bytes(channel for y in range(_HEIGHT) for x in range(_WIDTH)
                                   for channel in ((index % 256 + x) % 256, (index // 256 + y) % 256,
                                                   (phase + (x ^ y)) % 256))
                    with image_module.frombytes("RGB", (_WIDTH, _HEIGHT), pixels) as image:
                        png_buffer = io.BytesIO()
                        image.save(png_buffer, format="PNG")
                        png = png_buffer.getvalue()
                        if member == "pages/01.png":
                            selected_pixels = pixels
                            jpeg_buffer = io.BytesIO()
                            image.save(jpeg_buffer, format="JPEG", quality=40, optimize=True)
                            jpeg = jpeg_buffer.getvalue()
                            with image_module.open(io.BytesIO(jpeg)) as decoded:
                                decoded.load()
                                jpeg_pixels = decoded.tobytes()
                    member_hashes[member] = _sha(png)
                    inner.writestr(_zip_info(member), png)
                metadata = (f"<ComicInfo><Series>{series}</Series><Number>{volume + 1}</Number>"
                            "</ComicInfo>").encode()
                member_hashes["ComicInfo.xml"] = _sha(metadata)
                inner.writestr(_zip_info("ComicInfo.xml"), metadata)
                inner.comment = b"Synthetic raster and metadata generated by benchmark_pipeline.py. MIT license."
            archive = inner_buffer.getvalue()
            source_bytes += len(archive)
            outer.writestr(_zip_info(relative), archive)
            books.append({"path": relative, "archive_sha256": _sha(archive), "archive_bytes": len(archive),
                          "members": member_hashes, "selected_member": "pages/01.png",
                          "first_pixel": [index % 256, index // 256, 64],
                          "source_pixels_sha256": _sha(selected_pixels)})
            expected: OutputFixture = {"path": str(PurePosixPath(relative).with_suffix(".jpg")),
                                       "sha256": _sha(jpeg), "pixels_sha256": _sha(jpeg_pixels), "bytes": len(jpeg)}
            outputs.append(expected)
            if volume == 0:
                outputs.append({**expected, "path": f"{series}/cover.jpg"})
    return {"size": size, "books": books, "outputs": outputs, "archive_bytes": source_bytes,
            "series": (size + 9) // 10, "bundle_sha256": _file_sha(bundle),
            "fixture_sha256": _sha(_canonical(books)), "expected_outputs_sha256": _sha(_canonical(outputs)),
            "raster": {"width": _WIDTH, "height": _HEIGHT, "mode": "RGB", "pages_per_book": 3},
            "license": "MIT. All raster pixels and XML are generated by this tool; no third-party media."}


def _library_path(value: object) -> str | None:
    if isinstance(value, int):
        try:
            value = os.readlink(f"/proc/self/fd/{value}")
        except OSError:
            return None
    if not isinstance(value, (str, bytes, os.PathLike)):
        value = getattr(value, "name", None)
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    path = Path(os.fsdecode(value)).absolute()
    if not path.is_relative_to(Path("/work/library")):
        return None
    return path.relative_to("/work/library").as_posix()


def _install_metrics() -> None:
    global _finish_metrics
    if os.environ.get("BENCHMARK_PIPELINE") != "1" or not Path("/repo/tools/benchmark_pipeline.py").is_file():
        raise ValueError("benchmark instrumentation may load only in its fixed sandbox")
    _assert_runtime()
    sys.dont_write_bytecode = True
    _METRICS.mkdir(exist_ok=True)
    event_path = _METRICS / f"{os.getpid()}-{time.monotonic_ns()}.jsonl"
    descriptor = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    finished = False

    def emit(event: str, **fields: object) -> None:
        payload = _canonical({"event": event, **fields}) + b"\n"
        if len(payload) > 8192 or os.write(descriptor, payload) != len(payload):
            raise RuntimeError("benchmark event capture failed")

    def active() -> bool:
        return _ACTIVE.is_file()

    def finish() -> None:
        nonlocal finished
        if not finished:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            emit("end", peak_rss_bytes=usage.ru_maxrss * 1024,
                 user_cpu_seconds=usage.ru_utime, system_cpu_seconds=usage.ru_stime,
                 argv=sys.argv, pid=os.getpid())
            finished = True
            os.close(descriptor)

    _finish_metrics = finish
    atexit.register(finish)

    def fork_child() -> None:
        nonlocal descriptor, finished
        os.close(descriptor)
        child_path = _METRICS / f"{os.getpid()}-{time.monotonic_ns()}.jsonl"
        descriptor = os.open(child_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        finished = False
        emit("ready", pid=os.getpid(), python=platform.python_version(), gil_enabled=True,
             instrumentation_sha256=_file_sha(Path(__file__)), forked=True)

    original_exit = os._exit

    def exit_with_metrics(code: int) -> Never:
        finish()
        original_exit(code)

    os.register_at_fork(after_in_child=fork_child)
    setattr(os, "_exit", exit_with_metrics)
    image_module = importlib.import_module("PIL.Image")
    scandir_module = importlib.import_module("scandir")
    os_module = importlib.import_module("os")
    zip_module = importlib.import_module("zipfile")
    original_image_open = image_module.open
    original_zip_init = zip_module.ZipFile.__init__
    original_member_open = zip_module.ZipFile.open
    original_member_read = zip_module.ZipExtFile.read

    def image_open(*args: Any, **kwargs: Any) -> Any:
        image = original_image_open(*args, **kwargs)
        image_ref = weakref.ref(image)
        load_function = image.load.__func__
        counted = False

        def load(*load_args: Any, **load_kwargs: Any) -> Any:
            nonlocal counted
            loaded = image_ref()
            if loaded is None:
                raise RuntimeError("benchmark raster owner disappeared")
            result = load_function(loaded, *load_args, **load_kwargs)
            if not counted and active():
                counted = True
                if loaded.mode != "RGB" or loaded.format not in {"PNG", "JPEG"}:
                    emit("instrumentation_error", reason="unexpected_decode_mode_or_format")
                    raise RuntimeError("benchmark decoded an unexpected raster mode or format")
                emit("decode", format=loaded.format, mode=loaded.mode, width=loaded.width, height=loaded.height,
                     decoded_bytes=loaded.width * loaded.height * 3, first_pixel=list(loaded.getpixel((0, 0))))
            return result

        image.load = load
        return image

    def zip_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_zip_init(self, *args, **kwargs)
        source = _library_path(self.filename)
        self._pipeline_benchmark_source = source if source and source.endswith(".cbz") and self.mode == "r" else None
        if self._pipeline_benchmark_source is not None and active():
            emit("archive_open", path=self._pipeline_benchmark_source)

    def member_open(self: Any, *args: Any, **kwargs: Any) -> Any:
        stream = original_member_open(self, *args, **kwargs)
        source = getattr(self, "_pipeline_benchmark_source", None)
        if source is not None and isinstance(stream, zip_module.ZipExtFile):
            stream._pipeline_benchmark_source = source
        return stream

    def member_read(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_member_read(self, *args, **kwargs)
        source = getattr(self, "_pipeline_benchmark_source", None)
        if source is not None and result and active():
            emit("member_read", path=source, member=self.name, bytes=len(result))
        return result

    def enumeration(function: Callable[..., Any], name: str) -> Callable[..., Any]:
        def call(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            result = function(path, *args, **kwargs)
            relative = _library_path(path)
            if relative is not None and active():
                emit("directory_scan", api=name, path=relative)
            return result
        return call

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "socket.connect":
            emit("network_attempt")
            raise RuntimeError("benchmark attempted a forbidden network connection")
        if event == "subprocess.Popen" and active():
            executable = str(arguments[0])
            python_child = Path(executable).is_absolute() and Path(executable).resolve() == Path(sys.executable).resolve()
            emit("subprocess_attempt", executable=executable, argv=str(arguments[1]), python=python_child)
        if event == "os.fork" and active():
            emit("fork_attempt", pid=os.getpid())
        if event == "open" and active() and arguments:
            relative = _library_path(arguments[0])
            if relative is not None and relative.endswith(".cbz"):
                emit("archive_file_open", path=relative)

    setattr(image_module, "open", image_open)
    setattr(zip_module.ZipFile, "__init__", zip_init)
    setattr(zip_module.ZipFile, "open", member_open)
    setattr(zip_module.ZipExtFile, "read", member_read)
    setattr(os_module, "scandir", enumeration(getattr(os_module, "scandir"), "os.scandir"))
    setattr(os_module, "listdir", enumeration(getattr(os_module, "listdir"), "os.listdir"))
    setattr(scandir_module, "scandir", enumeration(getattr(scandir_module, "scandir"), "scandir.scandir"))
    sys.addaudithook(audit)
    emit("ready", pid=os.getpid(), python=platform.python_version(), gil_enabled=True,
         instrumentation_sha256=_file_sha(Path(__file__)))


def _collect_metrics(books: list[BookFixture]) -> dict[str, Any]:
    counters: Counter[str] = Counter()
    scans: Counter[str] = Counter()
    opened: Counter[str] = Counter()
    selected_reads: set[str] = set()
    source_pixels: set[tuple[int, ...]] = set()
    endings: list[dict[str, Any]] = []
    spawned: list[dict[str, Any]] = []
    events_hash = hashlib.sha256()
    total_bytes = 0
    instrumentation_sha256 = _file_sha(Path("/repo/tools/benchmark_pipeline.py"))
    files = sorted(_METRICS.glob("*.jsonl"))
    if not files or len(files) > len(books) * 12 + 100:
        raise ValueError("instrumentation process count is missing or exceeds its bound")
    for path in files:
        ready = ended = 0
        with path.open("rb") as stream:
            for line in stream:
                total_bytes += len(line)
                if len(line) > 8192 or total_bytes > 64 * 1024 * 1024:
                    raise ValueError("instrumentation capture exceeded its bound")
                events_hash.update(line)
                event = json.loads(line)
                kind = event["event"]
                counters[kind] += 1
                if kind == "ready":
                    ready += 1
                    if (event["python"] != "3.14.6" or event["gil_enabled"] is not True
                            or event["instrumentation_sha256"] != instrumentation_sha256):
                        raise ValueError("a measured process has different runtime or instrumentation")
                elif kind == "end":
                    ended += 1
                    endings.append(event)
                elif kind == "archive_open":
                    opened[event["path"]] += 1
                elif kind == "member_read":
                    counters["member_bytes"] += event["bytes"]
                    if event["member"] == "pages/01.png" and event["bytes"] > 0:
                        selected_reads.add(event["path"])
                elif kind == "decode":
                    counters["decoded_bytes"] += event["decoded_bytes"]
                    if event["format"] == "PNG" and (event["width"], event["height"]) == (_WIDTH, _HEIGHT):
                        source_pixels.add(tuple(event["first_pixel"]))
                elif kind == "directory_scan":
                    scans[event["api"]] += 1
                elif kind == "subprocess_attempt":
                    spawned.append(event)
                    counters["python_subprocess_attempts"] += int(event["python"])
                elif kind == "fork_attempt":
                    counters["python_fork_attempts"] += 1
                elif kind in {"network_attempt", "instrumentation_error"}:
                    raise ValueError(f"measured process reported {kind}")
                elif kind not in {"subprocess_attempt", "archive_file_open"}:
                    raise ValueError(f"unrecognized instrumentation event {kind}")
        if ready != 1 or ended != 1:
            raise ValueError("an instrumented process did not initialize and terminate cleanly")
    expected_paths = {row["path"] for row in books}
    if set(opened) != expected_paths or selected_reads != expected_paths:
        raise ValueError("the timed workflow did not open and read the selected member of every book")
    if not {tuple(row["first_pixel"]) for row in books} <= source_pixels:
        raise ValueError("the timed workflow did not actually decode every distinct selected raster")
    if counters["python_subprocess_attempts"] + counters["python_fork_attempts"] != len(endings) - 1:
        raise ValueError(f"Python-process instrumentation mismatch: {len(endings)} endings; observed {spawned!r}")
    return {
        "archive_opens": counters["archive_open"], "unique_archives_opened": len(opened),
        "archive_file_open_calls": counters["archive_file_open"],
        "archive_member_read_calls": counters["member_read"], "archive_member_bytes_returned": counters["member_bytes"],
        "decoded_rasters": counters["decode"], "decoded_bytes": counters["decoded_bytes"],
        "unique_selected_rasters_observed": len(expected_paths), "directory_scans": counters["directory_scan"],
        "directory_scans_by_api": dict(scans), "instrumented_processes": len(endings),
        "subprocess_spawn_attempts": counters["subprocess_attempt"], "network_attempts": counters["network_attempt"],
        "python_subprocess_attempts": counters["python_subprocess_attempts"],
        "python_fork_attempts": counters["python_fork_attempts"],
        "native_subprocess_attempts": counters["subprocess_attempt"] - counters["python_subprocess_attempts"],
        "subprocess_executables": dict(Counter(event["executable"] for event in spawned)),
        "max_individual_process_peak_rss_bytes": max(event["peak_rss_bytes"] for event in endings),
        "process_lifetime_cpu_seconds_sum": sum(event["user_cpu_seconds"] + event["system_cpu_seconds"] for event in endings),
        "events_sha256": events_hash.hexdigest(), "event_capture_bytes": total_bytes,
    }


def _prepare_fixtures(data: dict[str, Any]) -> dict[str, tuple[int, int, int, int]]:
    workload = data["workload"]
    books = cast(list[BookFixture], workload["books"])
    if (_sha(_canonical(books)) != workload["fixture_sha256"] or len(books) != workload["size"]
            or _sha(_canonical(workload["outputs"])) != workload["expected_outputs_sha256"]
            or _file_sha(Path("/repo/benchmark-books.zip")) != workload["bundle_sha256"]):
        raise ValueError("generated fixture manifest or bundle provenance mismatch")
    Path("/work/library").mkdir()
    Path("/work/home").mkdir()
    Path("/work/config.toml").write_text(data["policy"]["modern_toml"], encoding="utf-8")
    identities: dict[str, tuple[int, int, int, int]] = {}
    with zipfile.ZipFile("/repo/benchmark-books.zip") as bundle:
        if len(bundle.infolist()) != len(books) or set(bundle.namelist()) != {row["path"] for row in books}:
            raise ValueError("fixture bundle members differ from its manifest")
        for row in books:
            relative = PurePosixPath(row["path"])
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in row["path"].split("/")):
                raise ValueError("noncanonical generated fixture path")
            if not 0 < row["archive_bytes"] <= 1024 * 1024:
                raise ValueError("generated fixture archive exceeds its bound")
            raw = bundle.read(row["path"])
            if len(raw) != row["archive_bytes"] or _sha(raw) != row["archive_sha256"]:
                raise ValueError("generated book bytes differ from the manifest")
            path = Path("/work/library") / row["path"]
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as stream:
                stream.write(raw)
            os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
            info = path.stat()
            identities[row["path"]] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return identities


def _legacy_run(policy: dict[str, Any]) -> dict[str, Any]:
    probe = _load_tool(Path("/repo/tools/legacy_probe.py"), "_pipeline_benchmark_legacy_probe")
    safe = probe.extract_safe_settings("/repo/settings.py")
    if not safe.ok:
        raise ValueError("frozen safe settings could not be decoded")
    previous = sys.argv
    try:
        sys.argv = list(policy["legacy_argv"])
        module, detail = probe._load_main(Path("/repo"), safe, Path("/repo/settings.py"))
        if module is None:
            raise ValueError(f"frozen application import failed: {detail}")
        module.parse_my_args()
        module.check_required_settings()
        actual = {name: getattr(module, name) for name in policy["shared"]}
        actual = {name: list(value) if isinstance(value, tuple) else value for name, value in actual.items()}
        actual["ranked_keywords"] = [
            {"name": keyword.name, "score": keyword.score, "file_type": keyword.file_type}
            for keyword in module.ranked_keywords
        ]
        if actual != policy["shared"]:
            raise ValueError("effective frozen application policy differs from the paired policy")
        if {name: getattr(module, name) for name in policy["legacy_retired"]} != policy["legacy_retired"]:
            raise ValueError("retired frozen controls differ from their recorded inactive policy")
        if module.paths != ["/work/library"] or module.download_folders or module.profile_code:
            raise ValueError("frozen application roots or profiling differ from the benchmark contract")
        module.main()
        return {"entrypoint": "frozen parse_my_args/check_required_settings/main", "reported_image_count": module.image_count}
    finally:
        sys.argv = previous


def _validate_outputs(data: dict[str, Any], identities: dict[str, tuple[int, int, int, int]], side: str) -> dict[str, Any]:
    workload = data["workload"]
    image_module = importlib.import_module("PIL.Image")
    originals: dict[str, str] = {}
    published: dict[str, str] = {}
    pixels: dict[str, str] = {}
    for row in cast(list[BookFixture], workload["books"]):
        path = Path("/work/library") / row["path"]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != identities[row["path"]]
                or _file_sha(path) != row["archive_sha256"]):
            raise ValueError(f"original archive was removed, replaced or modified: {row['path']}")
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(row["members"]):
                raise ValueError(f"original archive integrity failed: {row['path']}")
            if {name: _sha(archive.read(name)) for name in row["members"]} != row["members"]:
                raise ValueError(f"original archive member bytes changed: {row['path']}")
            with image_module.open(io.BytesIO(archive.read(row["selected_member"]))) as image:
                image.load()
                if _sha(image.tobytes()) != row["source_pixels_sha256"]:
                    raise ValueError("original raster pixels differ from the generated fixture")
        originals[row["path"]] = row["archive_sha256"]
    for output in cast(list[OutputFixture], workload["outputs"]):
        path = Path("/work/library") / output["path"]
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size != output["bytes"] or _file_sha(path) != output["sha256"]:
            raise ValueError(f"published artwork does not match the expected lexical-first JPEG: {output['path']}")
        with image_module.open(path) as image:
            image.load()
            if image.format != "JPEG" or image.mode != "RGB" or image.size != (_WIDTH, _HEIGHT):
                raise ValueError(f"published artwork has false format or dimensions: {output['path']}")
            digest = _sha(image.tobytes())
            if digest != output["pixels_sha256"]:
                raise ValueError(f"published artwork has incorrect decoded pixels: {output['path']}")
        published[output["path"]] = output["sha256"]
        pixels[output["path"]] = digest
    visible = {path.relative_to("/work/library").as_posix() for path in Path("/work/library").rglob("*")
               if path.is_file() and not any(part.startswith(".") for part in path.relative_to("/work/library").parts)}
    if visible != set(originals) | set(published):
        raise ValueError("unexpected visible files were published or expected files are missing")
    journal: dict[str, Any] | None = None
    if side == "modern":
        database = Path("/work/state/state.sqlite3")
        if not stat.S_ISREG(database.lstat().st_mode):
            raise ValueError("modern apply did not publish its real journal")
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            phases = dict(connection.execute("SELECT phase, COUNT(*) FROM operations GROUP BY phase"))
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            members = dict(connection.execute("SELECT kind, COUNT(*) FROM members GROUP BY kind"))
        if integrity != ("ok",) or not phases or set(phases) != {"complete"}:
            raise ValueError(f"modern publication did not settle all journal operations: {phases}")
        if members.get("publish", 0) != len(published):
            raise ValueError("modern journal does not account for every published artwork file")
        journal = {"phases": phases, "members_by_kind": members, "integrity": "ok", "bytes": database.stat().st_size}
    elif Path("/work/state").exists():
        raise ValueError("legacy run unexpectedly used the modern state directory")
    return {"originals_retained_unmodified": len(originals), "published_artwork_files": len(published),
            "published_book_sidecars": workload["size"], "published_series_artwork": workload["series"],
            "originals_sha256": _sha(_canonical(originals)), "outputs_sha256": _sha(_canonical(published)),
            "decoded_output_pixels_sha256": _sha(_canonical(pixels)), "failed_outputs": 0, "journal": journal}


def _sandbox_child(side: str) -> dict[str, Any]:
    _assert_runtime()
    instrumentation = sys.modules.get("sitecustomize")
    if (os.environ.get("BENCHMARK_PIPELINE") != "1" or Path.cwd() != Path("/work")
            or instrumentation is None or not callable(getattr(instrumentation, "_finish_metrics", None))):
        raise ValueError("the benchmark child requires its fixed instrumented bubblewrap sandbox")
    data = json.loads(Path("/repo/benchmark.json").read_text(encoding="utf-8"))
    if data["tool_sha256"] != _file_sha(Path(__file__)):
        raise ValueError("child benchmark tool provenance mismatch")
    _frozen_hashes(Path("/repo"))
    installed = _installed_package(None)
    if installed["files_sha256"] != data["installed"]["files_sha256"] or _runtime() != data["runtime"]:
        raise ValueError("sandbox installed application or runtime differs from the supervisor")
    policy = data["policy"]
    if _sha(_canonical(policy["shared"])) != policy["sha256"]:
        raise ValueError("shared policy manifest provenance mismatch")
    setup_started = time.perf_counter_ns()
    identities = _prepare_fixtures(data)
    setup_seconds = (time.perf_counter_ns() - setup_started) / 1e9
    probe = _load_tool(Path("/repo/tools/legacy_probe.py"), "_pipeline_benchmark_capture")
    if os.environ.pop(probe.SANDBOX_MARKER, None) != "1":
        raise ValueError("the reviewed characterization setup marker is missing")
    output = probe._BoundedCapture(limit=_CAPTURE_LIMIT)
    diagnostics = probe._BoundedCapture(limit=_CAPTURE_LIMIT)
    _ACTIVE.touch(exist_ok=False)
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    entry_result: dict[str, Any]
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            if side == "legacy":
                entry_result = _legacy_run(policy)
            else:
                cli = importlib.import_module("komga_cover_extractor.cli")
                code = cli.main(list(policy["modern_argv"]))
                if code != 0:
                    raise ValueError(f"installed full-run CLI failed with exit {code}: {output.getvalue()[:4096]}")
                entry_result = {"entrypoint": "installed komga_cover_extractor.cli.main run --apply"}
    finally:
        elapsed = (time.perf_counter_ns() - started) / 1e9
        cpu_seconds = (time.process_time_ns() - cpu_started) / 1e9
        entry_peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        _ACTIVE.unlink()
        cast(Any, instrumentation)._finish_metrics()
        os.chdir("/work")
    if output.dropped or diagnostics.dropped:
        raise ValueError("application output capture exceeded its bound")
    if side == "modern":
        result = json.loads(output.getvalue())
        if (result.get("ok") is not True or result.get("exit_code") != 0 or result["counts"]["failed"]
                or not result["counts"]["committed"] or result["deferred_deliveries"]
                or len(result["releases"]) != data["workload"]["size"]
                or any(item["status"] not in {"observed", "planned", "committed", "skipped", "no_match"}
                       for item in result["decisions"])):
            raise ValueError("installed full run returned incomplete, skipped-only or unresolved work")
        entry_result["counts"] = result["counts"]
        entry_result["decision_reasons"] = dict(Counter(item["reason"] for item in result["decisions"]))
        configuration = importlib.import_module("komga_cover_extractor.config")
        values = configuration.load_config(Path("/work/config.toml"), environ={}, cwd=Path("/work")).as_dict(redact=False)
        if _sha(_canonical({name: values[name] for name in policy["shared"]})) != policy["sha256"]:
            raise ValueError("installed CLI policy differs from the effective frozen policy")
    elif entry_result["reported_image_count"] != data["workload"]["size"] or "ERROR" in output.getvalue():
        raise ValueError("frozen full run reported a failure or did not extract every book")
    metrics = _collect_metrics(cast(list[BookFixture], data["workload"]["books"]))
    validation_started = time.perf_counter_ns()
    validation = _validate_outputs(data, identities, side)
    _frozen_hashes(Path("/repo"))
    if _installed_package(None)["files_sha256"] != installed["files_sha256"]:
        raise ValueError("installed application bytes changed during the measured workflow")
    return {"status": "pass", "side": side, "fixture_sha256": data["workload"]["fixture_sha256"],
            "policy_sha256": policy["sha256"], "installed_files_sha256": installed["files_sha256"],
            "application_started_monotonic_ns": started, "application_wall_seconds": elapsed,
            "entry_process_application_cpu_seconds": cpu_seconds, "entry_process_peak_rss_bytes": entry_peak_rss,
            "fixture_setup_wall_seconds": setup_seconds,
            "validation_wall_seconds": (time.perf_counter_ns() - validation_started) / 1e9,
            "work_counts": metrics, "validation": validation, "application": entry_result,
            "application_stdout_bytes": len(output.getvalue().encode("utf-8")),
            "application_stdout_sha256": _sha(output.getvalue().encode("utf-8")),
            "application_stderr": diagnostics.getvalue(), "load_average": list(os.getloadavg())}


def _instrumented_command(isolation: Any, config: Any, layout: Any, bwrap: str) -> list[str]:
    command, error = isolation._sandbox_command(config, layout, bwrap)
    if error is not None or command is None:
        raise ValueError(error or "cannot construct the reviewed sandbox")
    if command[-3:] != [str(layout.command), "-B", str(config.probe_target)]:
        raise ValueError("reviewed sandbox command shape changed")
    purelib = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    platlib = Path(sysconfig.get_path("platlib")).resolve(strict=True)
    target = isolation._safe_target_for_host(purelib, layout)
    if not layout.venv or purelib != platlib or target is None or not purelib.is_relative_to(layout.prefix):
        raise ValueError("unsupported virtual environment layout for identical child instrumentation")
    entries = sorted(purelib.iterdir())
    if not entries or len(entries) > 512 or any(entry.name == "sitecustomize.py" for entry in entries):
        raise ValueError("site-packages contains an existing startup hook or exceeds the supported overlay")
    overlay = ["--tmpfs", str(target)]
    for entry in entries:
        overlay.extend(("--ro-bind", str(entry), str(target / entry.name)))
    overlay.extend(("--ro-bind", str(Path(__file__).resolve()), str(target / "sitecustomize.py")))
    environment = {"BENCHMARK_PIPELINE": "1", "TZ": "UTC", "PYTHONHASHSEED": "0",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    for key, value in environment.items():
        overlay.extend(("--setenv", key, value))
    return [*command[:-3], *overlay, str(layout.command), "-I", "-B", str(config.probe_target)]


def _spread(values: Sequence[int | float]) -> dict[str, int | float]:
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def _summaries(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for size in sorted({sample["size"] for sample in measurements}):
        for side in ("legacy", "modern"):
            samples = [sample for sample in measurements if sample["size"] == size and sample["side"] == side and sample.get("status") == "pass"]
            if not samples:
                continue
            summary: dict[str, Any] = {"size": size, "side": side, "completed_trials": len(samples)}
            for key in ("application_wall_seconds", "sandbox_trial_wall_seconds", "entry_process_peak_rss_bytes",
                        "entry_process_application_cpu_seconds", "fixture_setup_wall_seconds", "validation_wall_seconds"):
                summary[key] = _spread([sample[key] for sample in samples])
            summary["work_counts"] = {
                key: _spread([sample["work_counts"][key] for sample in samples])
                for key, value in samples[0]["work_counts"].items() if isinstance(value, (int, float))
            }
            result.append(summary)
    return result


def benchmark(source_root: Path, *, sizes: Sequence[int], trials: int, timeout: float) -> dict[str, Any]:
    _assert_runtime()
    source_root = source_root.resolve(strict=True)
    if source_root != Path(__file__).resolve().parents[1]:
        raise ValueError("source-root must be this tool's checkout with its fixed frozen allowlist")
    installed = _installed_package(source_root)
    frozen = _frozen_hashes(source_root / "tests/fixtures/legacy")
    dependencies = _dependencies(source_root)
    isolation = _load_tool(source_root / "tools/characterize_legacy.py", "_pipeline_benchmark_isolation")
    config = isolation._production_config()
    error = isolation._validate_config(config)
    layout, layout_error = isolation._interpreter_layout()
    bwrap = shutil.which("bwrap")
    if error is not None or layout_error is not None or layout is None or bwrap is None:
        raise ValueError(error or layout_error or "the reviewed bubblewrap/interpreter mapping is unavailable")
    policy = _policy(source_root)
    tools = {name: _file_sha(source_root / "tools" / name)
             for name in ("benchmark_pipeline.py", "characterize_legacy.py", "legacy_probe.py")}
    runtime = _runtime()
    measurements: list[dict[str, Any]] = []
    workloads: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 1, "status": "pass", "acceptance_ids": ["D10"],
        "performance_verdict": "inconclusive; no limiter profile or full-application speedup claim",
        "workload": "actual artwork-only full-run discovery, raster decoding, book sidecars and first-volume series art",
        "requested_sizes": list(sizes), "requested_paired_trials": trials, "trial_timeout_seconds": timeout,
        "runtime": runtime, "start_load_average": list(os.getloadavg()), "required_dependency_versions": dependencies,
        "installed": installed, "frozen_sha256": frozen, "tool_sha256": tools,
        "policy": policy, "workloads": workloads, "measurements": measurements,
        "measurement_definitions": {
            "application_wall_seconds": "monotonic time from application import/entrypoint through CLI/main return and resource teardown; includes policy proof and bounded stdout capture; excludes fixture copying and independent output validation",
            "sandbox_trial_wall_seconds": "supervisor monotonic time around the entire fresh bwrap process; includes interpreter/sitecustomize startup, provenance checks, fixture copying, application and independent validation",
            "entry_process_peak_rss_bytes": "Linux RUSAGE_SELF.ru_maxrss * 1024 sampled immediately after application return, before independent validation; lifetime high-water, not incremental allocation",
            "max_individual_process_peak_rss_bytes": "largest process-lifetime RUSAGE_SELF peak across the entry process and all instrumented Python workers; not the sum and not concurrent process-tree RSS",
            "decoded_bytes": "sum of width * height * 3 after the first successful load of each RGB PNG/JPEG object returned by PIL.Image.open during the application timer; includes the application's repeated decodes and validation, excludes transformed/new image allocations and tool validation",
            "archive_opens": "successful read-mode zipfile.ZipFile constructions for input .cbz books beneath /work/library, including workers; does not count is_zipfile/header checks as ZipFile constructions",
            "archive_file_open_calls": "CPython open audit events targeting an input .cbz, including header, fingerprint and ZipFile reads; counts calls, not bytes or unique descriptors",
            "archive_member_bytes_returned": "actual bytes returned by instrumented ZipExtFile.read calls on input books; repeated application reads count again; read1/readinto bypasses are not claimed",
            "directory_scans": "successful enumeration API calls beneath /work/library through os.scandir, os.listdir or legacy scandir.scandir; counts enumeration starts, not entries; excludes state and import scans",
            "process_lifetime_cpu_seconds_sum": "sum of user/system RUSAGE_SELF CPU at instrumented Python process completion; includes instrumentation imports; not application-timer-only CPU or native-helper CPU",
            "python_fork_attempts": "actual CPython os.fork audit events during application work; forked decoder children register independent process lifecycle and pixel/archive counters, distinct from fresh interpreter Popen attempts",
            "publication": "post-timer exact JPEG bytes, decoded RGB pixel hashes, original inode/size/mtime/hash, ZIP member hashes and modern completed journal rows",
        },
        "environment": {"sandbox": "characterize_legacy fixed allowlist and interpreter mapping",
                        "network": "fresh unshared network namespace plus rejecting Python connect audit hook",
                        "storage": "fresh /work and /tmp tmpfs; interpreter, tools, frozen originals and bundles read-only",
                        "python_flags": ["-I", "-B"], "HOME": "/work/home", "cwd": "/work",
                        "TZ": "UTC", "LANG": "C.UTF-8", "native_math_threads": 1,
                        "hash_seed": "-I ignores PYTHONHASHSEED; both sides use normal randomized interpreter hashes",
                        "instrumentation": "identical read-only sitecustomize overlay in the sandbox only; host environment remains untouched",
                        "configuration_environment": "tool-only characterization marker verified then removed before both application entries; namespace isolation remains",
                        "order": "fresh paired trials, counterbalanced legacy/modern then modern/legacy"},
        "limitations": [
            "Only configured local artwork work is selected. Organizer mutations, conversion, watcher, gap reporting and remote integrations are not benchmarked. Both real full-run entrypoints are used, not scalar parser loops.",
            "The frozen program publishes directly in one process. Modern work additionally fingerprints inputs, supervises decoder processes, validates encoded/staged artifacts and performs locked, fsynced SQLite publication. These safety costs are intentionally retained, not equalized away.",
            "RSS and CPU are scoped to instrumented Python processes. Native helper attempts, including the observed legacy import-time file executable probe, are recorded separately and remain in wall time. Native-helper RSS/CPU and concurrent tree-total memory are not measured.",
            "Identical instrumentation eagerly imports Pillow/scandir in each interpreter and adds observer calls, event writes, one sample pixel per decode and marker stats. It changes absolute timing and is not zero-overhead production instrumentation.",
            "Decoded bytes are actual successfully loaded RGB raster planes, not unique book bytes, codec temporary memory or all native decoder allocations. Repeated modern validation decodes are counted separately.",
            "The fixture is synthetic, 128x192 RGB PNG pages in unencrypted ZIP comics, ten volumes per series. EPUB, native RAR/7z, large photos, alpha/ICC/orientation and real user media are outside this measurement.",
            "Fresh books/state/processes prevent output and application-cache reuse. Host filesystem page caches, CPU scheduling and thermal state are not flushed or controlled; per-trial load average and counterbalanced order expose some noise.",
            "Publication uses tmpfs in the existing sandbox. Real journal/fsync paths execute, but this is not a physical-storage durability or disk-throughput benchmark.",
            "Independent exact-output and original-retention validation occurs after the application timer. Entire sandbox wall time also reports that validation cost.",
            "No uninstrumented profiler identifies a bottleneck. The tool reports median/range and concrete work, never a speedup, scalability or full-parity conclusion.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="kce-pipeline-benchmark-") as temporary:
        root = Path(temporary)
        manifest_path = root / "benchmark.json"
        bundle_path = root / "books.zip"
        child_target = PurePosixPath("/repo/tools/benchmark_pipeline.py")
        child_config = replace(config, probe_target=child_target, files=(*config.files,
            isolation._AllowlistedFile(Path(__file__).resolve(), child_target),
            isolation._AllowlistedFile(manifest_path, PurePosixPath("/repo/benchmark.json")),
            isolation._AllowlistedFile(bundle_path, PurePosixPath("/repo/benchmark-books.zip")),
            isolation._AllowlistedFile(source_root / "tests/fixtures/legacy/LICENSE.md", PurePosixPath("/repo/LICENSE.md")),
        ))
        for size in sizes:
            if bundle_path.exists():
                bundle_path.unlink()
            generation_started = time.perf_counter_ns()
            workload = _fixtures(size, bundle_path)
            workload["generation_wall_seconds"] = (time.perf_counter_ns() - generation_started) / 1e9
            workloads.append(workload)
            manifest_path.write_bytes(_canonical({"workload": workload, "policy": policy, "installed": installed,
                                                  "runtime": runtime, "tool_sha256": tools["benchmark_pipeline.py"]}))
            error = isolation._validate_config(child_config)
            if error is not None:
                raise ValueError(error)
            command = _instrumented_command(isolation, child_config, layout, bwrap)
            for trial in range(trials):
                pair: dict[str, dict[str, Any]] = {}
                order = ("legacy", "modern") if trial % 2 == 0 else ("modern", "legacy")
                for side in order:
                    started = time.perf_counter_ns()
                    code, stdout, stderr, timed_out, failure = isolation._run_process([*command, "--sandbox-child", side], timeout)
                    elapsed = (time.perf_counter_ns() - started) / 1e9
                    attempt = {"size": size, "side": side, "trial": trial + 1, "sequence": len(measurements) + 1,
                               "sandbox_trial_wall_seconds": elapsed, "exit_code": code, "timed_out": timed_out}
                    try:
                        if failure is not None or timed_out:
                            raise ValueError(f"{side} measured sandbox failed: {failure or f'total_deadline_exceeded:{timeout:g}s'}")
                        result = json.loads(stdout)
                        if not isinstance(result, dict):
                            raise ValueError("child did not emit a JSON object")
                        if failure is not None or timed_out or code != 0 or stderr or result.get("status") != "pass":
                            raise ValueError(f"{side} measured sandbox failed: {failure or result.get('error') or stderr.decode('utf-8', errors='replace')[:4096]}")
                        if (result["fixture_sha256"] != workload["fixture_sha256"] or result["policy_sha256"] != policy["sha256"]
                                or result["installed_files_sha256"] != installed["files_sha256"]):
                            raise ValueError("paired measured provenance differs")
                        startup = (result["application_started_monotonic_ns"] - started) / 1e9
                        if not 0 <= startup <= elapsed or not 0 < result["application_wall_seconds"] <= elapsed:
                            raise ValueError("measured sandbox clock or application interval is invalid")
                        result.update(attempt)
                        result["pre_application_wall_seconds"] = startup
                        measurements.append(result)
                        pair[side] = result
                    except (ValueError, KeyError, TypeError) as error:
                        measurements.append({**attempt, "status": "failed", "error": str(error),
                                             "stdout_prefix": stdout[:8192].decode("utf-8", errors="replace"),
                                             "stderr_prefix": stderr[:8192].decode("utf-8", errors="replace")})
                        report.update({"status": "failed", "error": str(error), "summaries": _summaries(measurements)})
                        return report
                for key in ("originals_sha256", "outputs_sha256", "decoded_output_pixels_sha256",
                            "originals_retained_unmodified", "published_artwork_files", "failed_outputs"):
                    if pair["legacy"]["validation"][key] != pair["modern"]["validation"][key]:
                        report.update({"status": "failed", "error": f"paired end-to-end validation differs for {key}",
                                       "summaries": _summaries(measurements)})
                        return report
    _frozen_hashes(source_root / "tests/fixtures/legacy")
    if _installed_package(source_root)["files_sha256"] != installed["files_sha256"] or tools != {
        name: _file_sha(source_root / "tools" / name) for name in tools
    }:
        raise ValueError("benchmark application or reviewed tools changed during the paired trials")
    report["summaries"] = _summaries(measurements)
    report["end_load_average"] = list(os.getloadavg())
    report["completed_paired_trials"] = len(measurements) // 2
    return report


def _write_exclusive(path: Path, report: dict[str, Any]) -> None:
    path = path.absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        created = os.fstat(descriptor)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None
        with stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created is not None:
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(path.name, dir_fd=directory)
        raise
    finally:
        os.close(directory)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 1000])
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=900, help="finite deadline per fresh sandbox, at most 3600 seconds")
    parser.add_argument("--output", type=Path, help="new exclusive JSON receipt; never overwrites a file or follows symlink parents")
    parser.add_argument("--sandbox-child", choices=("legacy", "modern"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.sandbox_child is not None:
            report = _sandbox_child(args.sandbox_child)
        else:
            if not 5 <= args.trials <= 100 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
                raise ValueError("use 5 to 100 paired trials and a finite timeout in (0, 3600]")
            if not args.sizes or len(set(args.sizes)) != len(args.sizes) or any(not 1 <= size <= 1000 for size in args.sizes):
                raise ValueError("sizes must be distinct positive book counts no greater than 1000")
            if args.output is not None and (args.output.exists() or args.output.is_symlink()):
                raise ValueError("output must be a new explicitly named file")
            report = benchmark(args.source_root, sizes=args.sizes, trials=args.trials, timeout=args.timeout)
    except (OSError, ValueError, ImportError, RuntimeError, KeyError, TypeError, sqlite3.Error) as error:
        report = {"schema_version": 1, "status": "blocked", "error": str(error)}
    if args.output is not None:
        try:
            _write_exclusive(args.output, report)
        except OSError as error:
            print(json.dumps({"schema_version": 1, "status": "blocked", "error": f"exclusive output publication failed: {error}"}))
            return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1 if report["status"] == "failed" else 2


if __name__ == "sitecustomize":
    try:
        _install_metrics()
    except BaseException as error:
        os.write(2, f"benchmark instrumentation startup failed: {type(error).__name__}: {error}\n".encode())
        os._exit(2)
elif __name__ == "__main__":
    raise SystemExit(main())

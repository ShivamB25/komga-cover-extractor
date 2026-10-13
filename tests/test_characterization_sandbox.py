from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

_LAUNCHER_PATH = Path(__file__).parents[1] / "tools" / "characterize_legacy.py"
_LAUNCHER_SPEC = importlib.util.spec_from_file_location(
    "characterize_legacy_test_module", _LAUNCHER_PATH
)
assert _LAUNCHER_SPEC is not None and _LAUNCHER_SPEC.loader is not None
_launcher = importlib.util.module_from_spec(_LAUNCHER_SPEC)
sys.modules[_LAUNCHER_SPEC.name] = _launcher
_LAUNCHER_SPEC.loader.exec_module(_launcher)

_AllowlistedFile = _launcher._AllowlistedFile
_SandboxConfig = _launcher._SandboxConfig
_run_sandbox = _launcher._run_sandbox
_timeout_value = _launcher._timeout_value


_REPORT_TEMPLATE = """\
import json
import socket
from pathlib import Path

source = Path('/repo/source.txt')
sentinel = Path({sentinel!r})
checks = []
try:
    sentinel.read_text(encoding='utf-8')
except FileNotFoundError:
    checks.append(('host-sentinel-inaccessible', True))
except OSError:
    checks.append(('host-sentinel-inaccessible', True))
else:
    checks.append(('host-sentinel-inaccessible', False))

try:
    source.write_text('mutated', encoding='utf-8')
except OSError:
    checks.append(('source-read-only', True))
else:
    checks.append(('source-read-only', False))

try:
    connection = socket.create_connection(('1.1.1.1', 80), timeout=0.25)
except OSError:
    checks.append(('network-denied', True))
else:
    connection.close()
    checks.append(('network-denied', False))

try:
    Path('/work/scratch.txt').write_text('scratch', encoding='utf-8')
    scratch_writable = Path('/work/scratch.txt').read_text(encoding='utf-8') == 'scratch'
except OSError:
    scratch_writable = False
checks.append(('scratch-writable', scratch_writable))

report = {{
    'schema_version': 1,
    'mode': 'fast',
    'python': 'fixture',
    'source_hashes': {{}},
    'tests': [
        {{'name': name, 'status': 'pass' if passed else 'fail', 'detail': ''}}
        for name, passed in checks
    ],
    'corpus': None,
    'adaptations': [],
}}
print(json.dumps(report, separators=(',', ':')))
raise SystemExit(0 if all(passed for _, passed in checks) else 1)
"""


def _fixture(tmp_path: Path, sentinel: Path) -> _SandboxConfig:
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    probe = tmp_path / "tools" / "legacy_probe.py"
    probe.parent.mkdir()
    probe.write_text(_REPORT_TEMPLATE.format(sentinel=str(sentinel)), encoding="utf-8")
    return _SandboxConfig(
        files=(
            _AllowlistedFile(source, PurePosixPath("/repo/source.txt")),
            _AllowlistedFile(probe, PurePosixPath("/repo/tools/legacy_probe.py")),
        )
    )


def _tests_by_name(report: dict[str, object]) -> dict[str, dict[str, object]]:
    tests = report["tests"]
    assert isinstance(tests, list)
    return {
        item["name"]: item
        for item in tests
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def test_import_has_no_launch_or_output(tmp_path: Path) -> None:
    launcher = _LAUNCHER_PATH
    script = """
import importlib.util
from pathlib import Path
path = Path(__LAUNCHER__)
spec = importlib.util.spec_from_file_location('characterize_legacy_import_only', path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print('imported')
""".replace("__LAUNCHER__", repr(str(launcher)))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "LANG": "C.UTF-8"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "imported\n"
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_timeout_parser_rejects_non_positive_and_unbounded_values() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _timeout_value("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _timeout_value("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        _timeout_value("3600.1")
    assert _timeout_value("0.5") == 0.5


def test_missing_bwrap_fails_closed_without_running_probe(tmp_path: Path) -> None:
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("secret", encoding="utf-8")
    config = _fixture(tmp_path, sentinel)
    source = tmp_path / "source.txt"
    result = _run_sandbox(
        "fast", 2.0, _config=config, _bwrap_path=str(tmp_path / "absent")
    )

    assert result.returncode == 2
    assert result.error is not None
    assert "bwrap" in result.error
    assert result.report is not None
    assert result.report["error"] == result.error
    assert source.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(
    sys.platform != "linux", reason="bubblewrap namespaces require Linux"
)
def test_actual_namespace_isolation_and_disposable_scratch(tmp_path: Path) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable; refusal path is covered separately")
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("host-only", encoding="utf-8")
    config = _fixture(tmp_path, sentinel)
    source = tmp_path / "source.txt"

    result = _run_sandbox("fast", 10.0, _config=config, _bwrap_path=bwrap)

    assert result.returncode == 0, result.error or result.stderr
    assert result.error is None
    assert result.report is not None
    checks = _tests_by_name(result.report)
    assert {name: checks[name]["status"] for name in checks} == {
        "host-sentinel-inaccessible": "pass",
        "source-read-only": "pass",
        "network-denied": "pass",
        "scratch-writable": "pass",
    }
    assert source.read_text(encoding="utf-8") == "original"
    # /work is a namespace tmpfs, not a host temporary path or bind mount.
    assert not (tmp_path / "scratch.txt").exists()
    assert not (tmp_path / "work").exists()


@pytest.mark.skipif(
    sys.platform != "linux", reason="bubblewrap namespaces require Linux"
)
def test_namespace_resolves_interpreter_and_libraries_with_foreign_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable; refusal path is covered separately")
    invocation_home = tmp_path / "invocation-home"
    invocation_home.mkdir()
    sentinel = invocation_home / "private-config"
    sentinel.write_text("host-only", encoding="utf-8")
    config = _fixture(tmp_path, sentinel)
    probe = tmp_path / "tools" / "legacy_probe.py"
    native_checks = """\
import ctypes
import encodings
import ssl
import sys

libc = ctypes.CDLL('libc.so.6')
libc.strlen.argtypes = [ctypes.c_char_p]
libc.strlen.restype = ctypes.c_size_t
"""
    checks = """\
checks = [('native-library-loaded', libc.strlen(b'isolated') == 8)]
for name, filename in (
    ('interpreter-read-only', sys.executable),
    ('stdlib-read-only', encodings.__file__),
):
    try:
        with Path(filename).open('r+b'):
            pass
    except OSError:
        checks.append((name, True))
    else:
        checks.append((name, False))
"""
    probe.write_text(
        native_checks + probe.read_text(encoding="utf-8").replace("checks = []", checks),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(invocation_home))

    result = _run_sandbox("fast", 10.0, _config=config, _bwrap_path=bwrap)

    assert result.returncode == 0, result.error or result.stderr
    assert result.error is None
    assert result.report is not None
    observed = _tests_by_name(result.report)
    assert {name: item["status"] for name, item in observed.items()} == {
        "native-library-loaded": "pass",
        "interpreter-read-only": "pass",
        "stdlib-read-only": "pass",
        "host-sentinel-inaccessible": "pass",
        "source-read-only": "pass",
        "network-denied": "pass",
        "scratch-writable": "pass",
    }
    assert sentinel.read_text(encoding="utf-8") == "host-only"
    assert (tmp_path / "source.txt").read_text(encoding="utf-8") == "original"


def test_valid_failure_status_and_malformed_output_never_become_success(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel"
    config = _fixture(tmp_path, sentinel)
    probe = tmp_path / "tools" / "legacy_probe.py"
    valid = probe.read_text(encoding="utf-8")
    probe.write_text(
        valid.replace("raise SystemExit(0 if", "raise SystemExit(1 if"),
        encoding="utf-8",
    )
    bwrap = shutil.which("bwrap")
    if sys.platform != "linux" or bwrap is None:
        pytest.skip("bubblewrap is unavailable; refusal path is covered separately")
    failed = _run_sandbox("fast", 10.0, _config=config, _bwrap_path=bwrap)
    assert failed.returncode == 1
    assert failed.report is not None
    assert failed.error is None

    probe.write_text("print('not-json')\n", encoding="utf-8")
    malformed = _run_sandbox("fast", 10.0, _config=config, _bwrap_path=bwrap)
    assert malformed.returncode == 1
    assert malformed.error is not None
    assert malformed.report is not None
    assert "malformed" in malformed.error


@pytest.mark.skipif(
    sys.platform != "linux", reason="bubblewrap namespaces require Linux"
)
def test_large_valid_json_report_is_fully_drained(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    config = _fixture(tmp_path, sentinel)
    payload_size = 300 * 1024
    probe = tmp_path / "tools" / "legacy_probe.py"
    probe.write_text(
        f"""\
import json

payload = 'x' * {payload_size}
report = {{
    'schema_version': 1,
    'mode': 'fast',
    'python': 'fixture',
    'source_hashes': {{}},
    'tests': [],
    'corpus': None,
    'adaptations': [],
    'field_mismatch': payload,
}}
print(json.dumps(report, separators=(',', ':')))
""",
        encoding="utf-8",
    )
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable; refusal path is covered separately")

    result = _run_sandbox("fast", 10.0, _config=config, _bwrap_path=bwrap)

    assert result.returncode == 0, result.error or result.stderr
    assert result.error is None
    assert result.report is not None
    assert len(result.stdout) > 256 * 1024
    assert result.report["field_mismatch"] == "x" * payload_size


@pytest.mark.skipif(
    sys.platform != "linux", reason="bubblewrap namespaces require Linux"
)
def test_capture_overflow_is_explicitly_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "sentinel"
    config = _fixture(tmp_path, sentinel)
    payload_size = 4 * 1024
    probe = tmp_path / "tools" / "legacy_probe.py"
    probe.write_text(
        f"""\
import json

payload = 'x' * {payload_size}
report = {{
    'schema_version': 1,
    'mode': 'fast',
    'python': 'fixture',
    'source_hashes': {{}},
    'tests': [],
    'corpus': None,
    'adaptations': [],
    'field_mismatch': payload,
}}
print(json.dumps(report, separators=(',', ':')))
""",
        encoding="utf-8",
    )
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable; refusal path is covered separately")
    monkeypatch.setattr(_launcher, "MAX_CAPTURE_BYTES", 1024)

    result = _run_sandbox("fast", 10.0, _config=config, _bwrap_path=bwrap)

    assert result.returncode != 0
    assert result.error is not None
    assert "stdout capture overflow" in result.error
    assert result.report is not None
    assert result.report["error"] == result.error


def test_wall_timeout_is_reported_and_returns_before_default_limit(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel"
    config = _fixture(tmp_path, sentinel)
    probe = tmp_path / "tools" / "legacy_probe.py"
    probe.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    bwrap = shutil.which("bwrap")
    if sys.platform != "linux" or bwrap is None:
        pytest.skip("bubblewrap is unavailable; refusal path is covered separately")

    started = time.monotonic()
    result = _run_sandbox("fast", 0.5, _config=config, _bwrap_path=bwrap)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.returncode != 0
    assert result.error is not None
    assert "timed out" in result.error
    assert elapsed < 8.0

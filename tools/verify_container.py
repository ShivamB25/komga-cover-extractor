"""Prove the built image with isolated volumes, no network, and real service signals."""
from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_PREPARE = r"""
import io, json, os, pathlib, sys, zipfile
from PIL import Image
uid, gid = map(int, sys.argv[1:])
series = pathlib.Path('/books/Series')
series.mkdir()
with zipfile.ZipFile(series / 'Series v01.cbz', 'w') as archive:
    for name, color in [('01.png', (255, 0, 0)), ('cover.png', (0, 0, 255))]:
        stream = io.BytesIO()
        Image.new('RGB', (16, 16), color).save(stream, format='PNG')
        archive.writestr(name, stream.getvalue())
pathlib.Path('/config/config.toml').write_text('''
paths = ['/books']
download_folders = ['/books']
state_dir = '/state/journal'
log_dir = '/state/logs'
output_covers_as_webp = true
compress_image_option = false
compare_detected_cover_to_blank_images = false
bookwalker_check = false
send_scan_request_to_komga_libraries_toggle = false
uncheck_non_qbit_upgrades_toggle = false
log_to_file = true
watchdog_toggle = false
''', encoding='utf-8')
for root in ['/books', '/config', '/state']:
    for parent, dirs, files in os.walk(root):
        os.chown(parent, uid, gid)
        for name in files:
            os.chown(os.path.join(parent, name), uid, gid)
"""
_MANIFEST = r'''
import hashlib, json, os, pathlib
result = {}
for root in ['/books', '/state']:
    paths = [pathlib.Path(root), *sorted(pathlib.Path(root).rglob('*'))]
    result[root] = {str(p): [p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else '<directory>']
                    for p in paths}
print(json.dumps(result, sort_keys=True))
'''
_UMASK = r'''
import json, os, pathlib, stat, sys
assert sys.argv[1:] == ['a b', '*', '$PATH']
assert os.getuid() > 0 and os.getpid() == 1
path = pathlib.Path('/tmp/umask-proof')
path.write_text('runtime')
assert stat.S_IMODE(path.stat().st_mode) == 0o640
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'mode': '0640', 'pid': os.getpid()}))
'''
_ARTWORK = r'''
import json, os, pathlib
from PIL import Image
path = pathlib.Path('/books/Series/Series v01.webp')
with Image.open(path) as image:
    assert image.format == 'WEBP' and image.size == (16, 16)
    r, g, b = image.convert('RGB').getpixel((8, 8))
    assert r > 180 and g < 40 and b < 40
assert path.stat().st_uid == os.getuid() and os.getuid() > 0
assert pathlib.Path('/books/Series/Series v01.cbz').is_file()
print(json.dumps({'format': 'WEBP', 'uid': os.getuid(), 'source_retained': True}))
'''


_SERVE_WITH_HELPER = r'''
import json, os, pathlib, selectors, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
mode = sys.argv[1]
requests = []
class API(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass
    def respond(self, status, body, content_type='text/plain'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        requests.append(('POST', self.path))
        if self.path == '/api/v2/auth/login':
            self.send_response(200)
            self.send_header('Set-Cookie', 'SID=proof; Path=/')
            self.send_header('Content-Length', '3')
            self.end_headers()
            self.wfile.write(b'Ok.')
        elif self.path == '/api/v2/torrents/info':
            self.respond(401, b'Unauthorized') if self.server.reject else self.respond(200, b'[]', 'application/json')
        else:
            self.respond(400, b'No mutation authorized')
    def do_GET(self):
        requests.append(('GET', self.path))
        if self.server.reject:
            self.respond(401, b'Unauthorized')
        elif self.path.startswith('/api/v2/torrents/info'):
            self.respond(200, b'[]', 'application/json')
        elif self.path == '/api/v2/app/webapiVersion':
            self.respond(200, b'2.11.0')
        else:
            self.respond(404, b'Not found')
server = ThreadingHTTPServer(('127.0.0.1', 0), API)
server.reject = False
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
command = [
    sys.executable, '-I', '-m', 'komga_cover_extractor', '--config', '/config/config.toml', 'serve',
    '--set', 'watchdog_toggle=true', '--set', 'uncheck_non_qbit_upgrades_toggle=true',
    '--set', 'check_for_existing_series_toggle=true',
    '--set', 'qbittorrent_ip="127.0.0.1"', '--set', f'qbittorrent_port="{server.server_port}"',
    '--set', 'qbittorrent_username="proof"', '--set', 'qbittorrent_password="proof"',
    '--set', 'qbittorrent_target_category="proof"', '--set', 'qbit_poll_interval_seconds=1',
]
receipts = []
process = None
try:
    for iteration in range(2 if mode == 'helper-failure' else 1):
        server.reject = False
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        signal.signal(signal.SIGTERM, lambda *_: process.send_signal(signal.SIGTERM) if process.poll() is None else None)
        output, errors = bytearray(), bytearray()
        initialized = False
        children = ()
        deadline = time.monotonic() + 60
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                assert time.monotonic() < deadline, 'supervised service exceeded proof deadline'
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    assert len(key.data) <= 1024 * 1024, 'service proof output budget exceeded'
                    if key.fileobj is process.stderr:
                        sys.stderr.buffer.write(chunk)
                        sys.stderr.buffer.flush()
                if not initialized and b'kce serve ready\n' in errors:
                    assert b'kce watch ready\n' in errors and b'kce qbit ready\n' in errors
                    children = tuple(int(value) for value in pathlib.Path(
                        f'/proc/{process.pid}/task/{process.pid}/children').read_text().split())
                    assert len(children) == 2, 'serve did not own both initialized children'
                    initialized = True
                    if mode == 'helper-failure':
                        server.reject = True
                        deadline = time.monotonic() + 30
        status = process.wait(timeout=5)
        assert initialized, (output.decode(errors='replace'), errors.decode(errors='replace'))
        expected = 1 if mode == 'helper-failure' else 143
        assert status == expected, (status, errors.decode(errors='replace'))
        assert all(not pathlib.Path(f'/proc/{pid}').exists() for pid in children), 'owned child survived supervisor exit'
        records = [json.loads(line) for line in output.splitlines() if line]
        summaries = [record for record in records if record.get('event') != 'job']
        assert len(summaries) == 2 and any(record.get('event') == 'summary' for record in summaries)
        receipts.append({'round': iteration + 1, 'initialized_children': 2, 'exit_code': status, 'children_reaped': True})
        process.stdout.close()
        process.stderr.close()
    assert sum(method == 'POST' and path == '/api/v2/auth/login' for method, path in requests) == len(receipts)
    assert all(method == 'GET' or path in {'/api/v2/auth/login', '/api/v2/torrents/info'} for method, path in requests)
    print(json.dumps({'status': 'pass', 'mode': mode, 'rounds': receipts, 'mutation_posts': 0}))
finally:
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
    server.shutdown()
    server.server_close()
    thread.join()
sys.exit(143 if mode == 'signal' else 0)
'''


def _run(command: Sequence[str], *, expected: int = 0, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    output = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if output.returncode != expected:
        raise ValueError(f"deployment command returned {output.returncode}, expected {expected}\n{output.stdout}\n{output.stderr}")
    return output


def _json(command: Sequence[str]) -> dict[str, Any]:
    result = json.loads(_run(command).stdout)
    if not isinstance(result, dict):
        raise ValueError("container did not return a JSON object")
    return result


def _watch_lifecycle(image: str, mounts: Sequence[str], name: str) -> None:
    command = [
        "docker", "run", "--name", name, "--network", "none", *mounts,
        image, "python", "-I", "-c", _SERVE_WITH_HELPER, "signal",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, sys.stdout.buffer)
    selector.register(process.stderr, selectors.EVENT_READ, sys.stderr.buffer)
    recent = b""
    deadline = time.monotonic() + 60
    try:
        while b"kce serve ready\n" not in recent:
            if process.poll() is not None:
                raise ValueError("serve exited before initialized watcher readiness")
            if time.monotonic() >= deadline:
                raise ValueError("serve did not become initialized within 60 seconds")
            for key, _ in selector.select(timeout=0.25):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.write(chunk)
                key.data.flush()
                if key.fd == process.stderr.fileno():
                    recent = (recent + chunk)[-8192:]
        started = time.monotonic()
        _run(["docker", "kill", "--signal=TERM", name], timeout=10)
        stdout, stderr = process.communicate(timeout=30)
        sys.stdout.buffer.write(stdout)
        sys.stderr.buffer.write(stderr)
        if process.returncode != 143 or time.monotonic() - started >= 30:
            raise ValueError("serve did not preserve SIGTERM status and bounded shutdown")
        state = _json(["docker", "inspect", "--format", "{{json .State}}", name])
        if state.get("Running") is not False or state.get("Pid") != 0:
            raise ValueError("container still has an owned running service process")
    finally:
        selector.close()
        if process.poll() is None:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        process.stderr.close()
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)


def verify_container(image: str, source_root: Path) -> dict[str, object]:
    token = uuid.uuid4().hex
    volumes = {role: f"kce-proof-{token}-{role}" for role in ("books", "config", "state")}
    created = []
    identity = _json(["docker", "run", "--rm", "--network", "none", image, "python", "-c", "import json, os; print(json.dumps({'uid': os.getuid(), 'gid': os.getgid()}))"])
    if identity.get("uid", 0) <= 0:
        raise ValueError("the image does not default to its non-root appuser")
    try:
        for volume in volumes.values():
            _run(["docker", "volume", "create", volume])
            created.append(volume)
        provision_mounts = [value for role, volume in volumes.items() for value in ("--mount", f"type=volume,src={volume},dst=/{role}")]
        mounts = [value for role, volume in volumes.items() for value in ("--mount", f"type=volume,src={volume},dst=/{role}" + (",readonly" if role == "config" else ""))]
        _run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", *provision_mounts, image, "python", "-c", _PREPARE, str(identity["uid"]), str(identity["gid"])])
        base = ["docker", "run", "--rm", "--network", "none", *mounts, image]
        _run([*base, "kce", "--help"])
        before = _json([*base, "python", "-c", _MANIFEST])
        preview = _json([*base, "kce", "--config", "/config/config.toml", "extract", "/books/Series/Series v01.cbz", "--json"])
        if preview.get("counts", {}).get("committed") != 0 or preview.get("counts", {}).get("planned", 0) < 1:
            raise ValueError("mounted-root extraction did not default to a planned preview")
        if _json([*base, "python", "-c", _MANIFEST]) != before:
            raise ValueError("container preview changed mounted books or created journal/log files")
        apply = _json([*base, "kce", "--config", "/config/config.toml", "extract", "/books/Series/Series v01.cbz", "--apply", "--json"])
        if apply.get("counts", {}).get("committed", 0) < 1:
            raise ValueError("mounted-root apply did not commit its cover group")
        artwork = _json([*base, "python", "-c", _ARTWORK])
        mask = _json(["docker", "run", "--rm", "--network", "none", "--env", "UMASK=027", image, "python", "-c", _UMASK, "a b", "*", "$PATH"])
        invalid = _run(["docker", "run", "--rm", "--network", "none", "--env", "UMASK=008", image, "python", "-c", "print('command-ran')"], expected=2)
        if invalid.stdout or "UMASK must" not in invalid.stderr:
            raise ValueError("invalid runtime UMASK reached the child command")
        proof_script = source_root / "tools/verify_installed.py"
        installed = _json(["docker", "run", "--rm", "--network", "none", "--mount", f"type=bind,src={proof_script},dst=/verification/verify_installed.py,readonly", image, "python", "-I", "/verification/verify_installed.py", "--source-root", "/app"])
        _watch_lifecycle(image, mounts, f"kce-proof-{token}-watch")
        helper_failure = _json([*base, "python", "-I", "-c", _SERVE_WITH_HELPER, "helper-failure"])
        for flag in ("MANGA_ISBN", "EPUB_CONVERTER"):
            output = subprocess.run(["docker", "build", "--target", "wheel", "--build-arg", f"{flag}=true", str(source_root)], capture_output=True, text=True, timeout=180)
            if output.returncode == 0 or f"{flag}=true requires the original" not in output.stdout + output.stderr:
                raise ValueError(f"{flag} did not fail with its named absent-source prerequisite")
        return {
            "status": "pass", "image": image, "runtime_identity": identity,
            "mounted_artwork": artwork, "runtime_umask": mask,
            "installed_proof": installed, "watch_helper_shutdown": "two-initialized-children-then-SIGTERM-143",
            "helper_failure_and_fresh_restart": helper_failure,
            "optional_modes": "named-missing-source-failures",
        }
    finally:
        for volume in reversed(created):
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        report = verify_container(args.image, args.source_root.resolve())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"container proof failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Crash proofs require Linux process groups and /proc file descriptors")

_SUBJECT = """
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import ArtifactSpec, Operation, OperationPlan, RootPermission
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

installed_version = importlib.metadata.version('komga-cover-extractor')
case = Path(sys.argv[1])
root = case / 'library'
state = case / 'state'


def pause(phase, member, operation_id):
    marker = case / 'checkpoint.json'
    with marker.open('x', encoding='utf-8') as stream:
        json.dump({'phase': phase, 'member': member, 'operation_id': operation_id, 'pid': os.getpid()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    deadline = time.monotonic() + 60
    while not (case / 'continue').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Owned test parent did not resume this child')
        time.sleep(0.01)


def journal():
    store = StateStore(state)
    records = []
    for record in store.list_operations():
        members = []
        for member in store.list_members(record.operation_id):
            value = {
                'member_id': member.member_id, 'kind': member.kind, 'status': member.status,
                'destination': str(member.destination), 'error': member.error, 'aux': member.aux,
            }
            for name in ('stage_path', 'backup_path', 'cleanup_path'):
                observed = getattr(member, name)
                value[name] = str(observed) if observed is not None else None
            for name in ('source', 'stage_fingerprint', 'backup_fingerprint', 'published_fingerprint', 'cleanup_fingerprint'):
                observed = getattr(member, name)
                value[name] = observed.as_dict() if observed is not None else None
            members.append(value)
        records.append({
            'operation_id': record.operation_id, 'phase': record.phase, 'error': record.error,
            'plan': record.plan.as_dict(), 'members': members,
        })
    outbox = [
        {'event_id': event.event_id, 'operation_id': event.operation_id, 'member_id': event.member_id,
         'event_type': event.event_type, 'payload': event.payload, 'state': event.state, 'attempts': event.attempts}
        for event in store.list_outbox()
    ]
    return {'operations': records, 'outbox': outbox}


def report(result=None):
    print(json.dumps({'result': result.as_dict() if result is not None else None,
                      'journal': journal(), 'installed_version': installed_version}, sort_keys=True))
"""

_MOVE_PLAN = """
source = root / 'source.cbz'
target = root / 'target.cbz'
operation_id = 'remaining-boundary'
plan = OperationPlan('crash-proof-v1', operation_id, (
    Operation('book', 'move', target, source=fingerprint(source, root),
              destination_before=fingerprint(target, root), collision='replace', reason='book_upgrade'),
), (RootPermission(root),))
"""


@pytest.fixture
def installed_case() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="kce-transaction-proof-") as directory:
        case = Path(directory).resolve()
        for name in ("library", "process", "home", "tmp"):
            (case / name).mkdir()
        yield case


def _environment(case: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("KCE_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment.update(HOME=str(case / "home"), XDG_STATE_HOME=str(case / "home" / "state"), TMPDIR=str(case / "tmp"))
    return environment


@contextmanager
def _child(case: Path, source: str, *arguments: str) -> Iterator[subprocess.Popen[str]]:
    interpreter = os.environ.get("KCE_PROOF_PYTHON", sys.executable)
    child = subprocess.Popen(
        [interpreter, "-I", "-c", _SUBJECT + "\n" + textwrap.dedent(source), str(case), *arguments],
        cwd=case / "process", env=_environment(case), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.communicate(timeout=10)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def _run(case: Path, source: str, *arguments: str) -> dict[str, Any]:
    with _child(case, source, *arguments) as child:
        stdout, stderr = child.communicate(timeout=60)
        assert child.returncode == 0, f"Installed isolated child exited {child.returncode}\n{stderr}\n{stdout}"
        return json.loads(stdout)


def _wait_checkpoint(case: Path, child: subprocess.Popen[str]) -> dict[str, Any]:
    marker = case / "checkpoint.json"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=10)
            pytest.fail(f"Child exited before the requested filesystem boundary\n{stderr}\n{stdout}")
        time.sleep(0.01)
    pytest.fail("Installed child did not reach the requested boundary within 45 seconds")


def _kill(case: Path, source: str, *arguments: str) -> dict[str, Any]:
    with _child(case, source, *arguments) as child:
        checkpoint = _wait_checkpoint(case, child)
        assert checkpoint["pid"] == child.pid
        os.killpg(child.pid, signal.SIGKILL)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL, f"{stderr}\n{stdout}"
        assert stdout == "", "A killed operation must not report a completed consumer result"
        return checkpoint


def _inspect(case: Path) -> dict[str, Any]:
    return _run(case, "report()")


def _recover(case: Path, operation_id: str) -> dict[str, Any]:
    return _run(case, """
    result = TransactionExecutor(state, (root,)).recover(sys.argv[2], authorization=RunAuthorization(apply=True))
    report(result)
    """, operation_id)


def _operation(proof: dict[str, Any], operation_id: str) -> dict[str, Any]:
    matches = [record for record in proof["journal"]["operations"] if record["operation_id"] == operation_id]
    assert len(matches) == 1
    return matches[0]


def _events(proof: dict[str, Any], operation_id: str) -> list[dict[str, Any]]:
    return [event for event in proof["journal"]["outbox"] if event["operation_id"] == operation_id]


def _identity(path: Path) -> tuple[int, int, int, int, str]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(path: Path) -> dict[str, tuple[int, int, int, int, str] | str]:
    return {
        item.relative_to(path).as_posix(): _identity(item) if item.is_file() else "directory"
        for item in sorted(path.rglob("*"))
        if item.name != ".kce-transaction.lock"
    }


def _png(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes((red, green, blue)))) + chunk(b"IEND", b""))


def _book(path: Path, label: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in (
            ("pages/01.png", _png(255, 0, 0)),
            ("ComicInfo.xml", f"<ComicInfo><Title>{label}</Title></ComicInfo>".encode()),
        ):
            archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), content)
    return path.read_bytes()


def _assert_complete_move(
    case: Path, proof: dict[str, Any], original_source: bytes, prior_target: bytes, *, committed: int,
) -> None:
    record = _operation(proof, "remaining-boundary")
    assert record["phase"] == "complete"
    assert proof["result"]["exit_code"] == 0
    assert proof["result"]["counts"]["committed"] == committed
    assert proof["result"]["counts"]["failed"] == 0
    member, = record["members"]
    assert member["status"] == "complete"
    target = case / "library" / "target.cbz"
    assert target.read_bytes() == original_source
    assert not (case / "library" / "source.cbz").exists()
    assert member["published_fingerprint"]["sha256"] == hashlib.sha256(original_source).hexdigest()
    assert Path(member["backup_path"]).read_bytes() == prior_target
    assert Path(member["cleanup_path"]).read_bytes() == original_source
    assert member["backup_fingerprint"]["sha256"] == hashlib.sha256(prior_target).hexdigest()
    assert member["cleanup_fingerprint"]["sha256"] == hashlib.sha256(original_source).hexdigest()
    events = _events(proof, "remaining-boundary")
    assert len(events) == 1
    assert (events[0]["event_type"], events[0]["payload"]["kind"], events[0]["payload"]["destination"]) == (
        "local_commit", "move", str(target),
    )
    assert events[0]["attempts"] == 0
    assert all(member["stage_path"] is None or not Path(member["stage_path"]).exists() for member in record["members"])


@pytest.mark.parametrize("boundary", ["staged", "before_publish"])
def test_external_writer_appearing_after_stage_never_gets_overwritten(installed_case: Path, boundary: str) -> None:
    case = installed_case
    source = case / "library" / "source.cbz"
    original = _book(source, "observed source")
    target = source.with_name("target.cbz")
    operation_id = "late-destination"
    subject = """
    source = root / 'source.cbz'
    plan = OperationPlan('crash-proof-v1', 'late-destination', (
        Operation('book', 'move', root / 'target.cbz', source=fingerprint(source, root)),
    ), (RootPermission(root),))
    def checkpoint(phase, member):
        if phase == sys.argv[2] and member == 'book':
            pause(phase, member, plan.operation_id)
    result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
    report(result)
    """
    with _child(case, subject, boundary) as child:
        observed = _wait_checkpoint(case, child)
        assert observed["phase"] == boundary
        staged = _operation(_inspect(case), operation_id)["members"][0]
        assert Path(staged["stage_path"]).read_bytes() == original
        external = _run(case, """
        target = root / 'target.cbz'
        with target.open('xb') as stream:
            stream.write(b'late independent writer owns these bytes')
            stream.flush()
            os.fsync(stream.fileno())
        report()
        """)
        assert _events(external, operation_id) == []
        external_identity = _identity(target)
        (case / "continue").touch()
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, stderr
        refused = json.loads(stdout)
    assert refused["result"]["exit_code"] == 3
    assert refused["result"]["counts"]["committed"] == 0
    assert any(decision["status"] == "review_required" for decision in refused["result"]["decisions"])
    assert _operation(refused, operation_id)["phase"] == "needs_review"
    assert target.read_bytes() == b"late independent writer owns these bytes"
    assert _identity(target) == external_identity
    assert source.read_bytes() == original
    assert _events(refused, operation_id) == []
    candidate = Path(_operation(refused, operation_id)["members"][0]["stage_path"])
    assert candidate.read_bytes() == original
    before = _tree(case / "library")
    for _ in range(2):
        recovery = _recover(case, operation_id)
        assert recovery["result"]["exit_code"] == 3
        assert recovery["result"]["counts"]["committed"] == 0
        assert _tree(case / "library") == before
        assert _events(recovery, operation_id) == []


@pytest.mark.parametrize("boundary", ["before_stage_intent", "before_stage", "staged", "before_publish"])
def test_killed_intent_boundaries_recover_in_fresh_process_without_claiming_unowned_scratch(
    installed_case: Path, boundary: str,
) -> None:
    case = installed_case
    source_bytes = _book(case / "library" / "source.cbz", "incoming book")
    target_bytes = _book(case / "library" / "target.cbz", "prior book")
    unrelated = case / "library" / ".kce-stage-user-owned"
    unrelated.write_bytes(b"Unrelated scratch name is not executor ownership")
    unrelated_identity = _identity(unrelated)
    subject = _MOVE_PLAN + textwrap.dedent("""
    if sys.argv[2] == 'before_stage_intent':
        real_save = StateStore.save_member
        def save_member(store, member):
            if (member.member_id == 'book' and member.status == 'staging'
                    and member.stage_path is not None and (member.aux or {}).get('stage_inode') is None):
                pause('before_stage_intent', member.member_id, member.operation_id)
            return real_save(store, member)
        StateStore.save_member = save_member
    def checkpoint(phase, member):
        if phase == sys.argv[2] and member == 'book':
            pause(phase, member, operation_id)
    result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
    report(result)
    """)
    checkpoint = _kill(case, subject, boundary)
    assert checkpoint["phase"] == boundary
    interrupted = _inspect(case)
    record = _operation(interrupted, "remaining-boundary")
    member, = record["members"]
    assert _events(interrupted, "remaining-boundary") == []
    assert (case / "library" / "source.cbz").read_bytes() == source_bytes
    assert (case / "library" / "target.cbz").read_bytes() == target_bytes
    if boundary == "before_stage_intent":
        assert member["status"] == "planned"
        assert member["stage_path"] is None
    elif boundary == "before_stage":
        assert member["status"] == "staging"
        assert not Path(member["stage_path"]).exists()
    else:
        assert Path(member["stage_path"]).read_bytes() == source_bytes
        assert member["stage_fingerprint"]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        if boundary == "before_publish":
            assert record["phase"] == "publishing"
            assert member["status"] == "publishing"
            assert Path(member["backup_path"]).read_bytes() == target_bytes
    recovered = _recover(case, "remaining-boundary")
    _assert_complete_move(case, recovered, source_bytes, target_bytes, committed=1)
    before = _tree(case / "library")
    repeated = _recover(case, "remaining-boundary")
    assert repeated["result"]["exit_code"] == 0
    assert _tree(case / "library") == before
    assert _events(repeated, "remaining-boundary") == _events(recovered, "remaining-boundary")
    assert unrelated.read_bytes() == b"Unrelated scratch name is not executor ownership"
    assert _identity(unrelated) == unrelated_identity


@pytest.mark.parametrize("phase", ["stage", "backup", "publish", "cleanup"])
@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("fault_errno", [errno.ENOSPC, errno.EACCES], ids=["ENOSPC", "EACCES"])
def test_phase_specific_io_failure_recovers_real_effects_and_never_republishes(
    installed_case: Path, phase: str, timing: str, fault_errno: int,
) -> None:
    case = installed_case
    source_bytes = _book(case / "library" / "source.cbz", "incoming book")
    target_bytes = _book(case / "library" / "target.cbz", "prior book")
    subject = _MOVE_PLAN + textwrap.dedent("""
    phase, timing, fault_errno = sys.argv[2], sys.argv[3], int(sys.argv[4])
    seen = False
    real_open, real_write, real_replace, real_unlink = os.open, os.write, os.replace, os.unlink
    
    def fail(path):
        global seen
        seen = True
        (case / 'fault.json').write_text(json.dumps({'phase': phase, 'timing': timing, 'path': str(path)}), encoding='utf-8')
        raise OSError(fault_errno, 'Injected filesystem capacity or permission failure', str(path))
    
    def matches_phase(path):
        candidate = Path(path)
        if not candidate.is_relative_to(root):
            return False
        if phase == 'stage':
            return candidate.name.startswith('.kce-stage-')
        if phase == 'backup':
            return candidate.name == 'backup' and candidate.parent.name.startswith('.kce-quarantine-')
        return False
    
    def open_file(path, flags, *args, **kwargs):
        if not seen and timing == 'before' and flags & os.O_CREAT and matches_phase(path):
            fail(path)
        return real_open(path, flags, *args, **kwargs)
    
    def write_file(descriptor, data):
        path = Path(os.readlink(f'/proc/self/fd/{descriptor}'))
        if not seen and timing == 'after' and matches_phase(path):
            real_write(descriptor, data)
            fail(path)
        return real_write(descriptor, data)
    
    def replace_file(old, new, *args, **kwargs):
        if not seen and phase == 'publish' and Path(new) == target:
            if timing == 'after':
                real_replace(old, new, *args, **kwargs)
            fail(new)
        return real_replace(old, new, *args, **kwargs)
    
    def unlink_file(path, *args, **kwargs):
        if not seen and phase == 'cleanup' and Path(path) == source:
            if timing == 'after':
                real_unlink(path, *args, **kwargs)
            fail(path)
        return real_unlink(path, *args, **kwargs)
    
    os.open, os.write, os.replace, os.unlink = open_file, write_file, replace_file, unlink_file
    result = TransactionExecutor(state, (root,)).apply(plan, authorization=RunAuthorization(apply=True))
    os.open, os.write, os.replace, os.unlink = real_open, real_write, real_replace, real_unlink
    report(result)
    """)
    failed = _run(case, subject, phase, timing, str(fault_errno))
    fault = json.loads((case / "fault.json").read_text(encoding="utf-8"))
    assert (fault["phase"], fault["timing"]) == (phase, timing)
    record = _operation(failed, "remaining-boundary")
    member, = record["members"]
    if phase == "cleanup":
        assert failed["result"]["exit_code"] == 3
        assert failed["result"]["counts"]["committed"] == 1
        assert record["phase"] == "cleanup_pending"
        assert member["status"] == "cleanup_pending"
        assert len(_events(failed, "remaining-boundary")) == 1
        assert (case / "library" / "target.cbz").read_bytes() == source_bytes
        assert Path(member["cleanup_path"]).read_bytes() == source_bytes
        assert (case / "library" / "source.cbz").exists() == (timing == "before")
    else:
        assert failed["result"]["exit_code"] == 1
        assert failed["result"]["counts"]["committed"] == 0
        assert failed["result"]["counts"]["failed"] == 1
        expected_status = "permission_denied" if fault_errno == errno.EACCES else "publication_failed"
        assert [decision["status"] for decision in failed["result"]["decisions"]] == [expected_status]
        assert _events(failed, "remaining-boundary") == []
        assert (case / "library" / "source.cbz").read_bytes() == source_bytes
        expected_target = source_bytes if phase == "publish" and timing == "after" else target_bytes
        assert (case / "library" / "target.cbz").read_bytes() == expected_target
        if phase in {"stage", "backup"}:
            retained = Path(member["stage_path"] if phase == "stage" else member["backup_path"])
            assert retained.exists() == (timing == "after")
            if timing == "after":
                assert retained.read_bytes() == (source_bytes if phase == "stage" else target_bytes)
        if phase == "publish":
            assert Path(member["backup_path"]).read_bytes() == target_bytes
    already_published = _identity(case / "library" / "target.cbz") if phase == "cleanup" or phase == "publish" and timing == "after" else None
    recovered = _recover(case, "remaining-boundary")
    _assert_complete_move(case, recovered, source_bytes, target_bytes, committed=int(phase != "cleanup"))
    if already_published is not None:
        assert _identity(case / "library" / "target.cbz") == already_published
    after = _tree(case / "library")
    repeated = _recover(case, "remaining-boundary")
    _assert_complete_move(case, repeated, source_bytes, target_bytes, committed=0)
    assert _tree(case / "library") == after
    assert _events(repeated, "remaining-boundary") == _events(recovered, "remaining-boundary")


@pytest.mark.parametrize("phase", ["published", "cleanup_done"])
def test_case_only_directory_and_book_rename_recovers_all_associations(installed_case: Path, phase: str) -> None:
    case = installed_case
    source = case / "library" / "Series" / "series v01.cbz"
    original = _book(source, "case-only directory and book rename")
    artwork = source.with_suffix(".png")
    artwork.write_bytes(_png(0, 255, 0))
    subject = """
    old, new = root / 'Series', root / 'series'
    book = old / 'series v01.cbz'
    plan = OperationPlan('crash-proof-v1', 'case-only-directory', (
        Operation('create-directory', 'mkdir', new),
        Operation('book', 'move', new / 'Series v01.cbz', source=fingerprint(book, root), depends_on=('create-directory',)),
        Operation('artwork', 'move', new / 'Series v01.png', source=fingerprint(book.with_suffix('.png'), root), depends_on=('create-directory',)),
        Operation('remove-original', 'rmdir', old, depends_on=('book', 'artwork')),
    ), (RootPermission(root),), 'case_only_directory_rename')
    def checkpoint(phase, member):
        selected = 'book' if sys.argv[2] == 'published' else 'remove-original'
        if phase == sys.argv[2] and member == selected:
            pause(phase, member, plan.operation_id)
    result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
    report(result)
    """
    checkpoint = _kill(case, subject, phase)
    assert checkpoint["operation_id"] == "case-only-directory"
    interrupted = _inspect(case)
    assert (case / "library" / "series" / "Series v01.cbz").read_bytes() == original
    assert len(_events(interrupted, "case-only-directory")) == (4 if phase == "cleanup_done" else 0)
    book_identity = _identity(case / "library" / "series" / "Series v01.cbz")
    recovered = _recover(case, "case-only-directory")
    assert recovered["result"]["exit_code"] == 0
    assert _operation(recovered, "case-only-directory")["phase"] == "complete"
    assert not (case / "library" / "Series").exists()
    assert (case / "library" / "series" / "Series v01.png").read_bytes() == _png(0, 255, 0)
    assert _identity(case / "library" / "series" / "Series v01.cbz") == book_identity
    retained = {Path(member["cleanup_path"]).read_bytes() for member in _operation(recovered, "case-only-directory")["members"] if member["kind"] == "move"}
    assert retained == {original, _png(0, 255, 0)}
    assert len(_events(recovered, "case-only-directory")) == 4
    before = _tree(case / "library")
    repeated = _recover(case, "case-only-directory")
    assert repeated["result"]["exit_code"] == 0
    assert _tree(case / "library") == before
    assert _events(repeated, "case-only-directory") == _events(recovered, "case-only-directory")


@pytest.mark.parametrize("phase", ["published", "before_cleanup", "cleanup_done"])
def test_cyclic_folder_plan_recovers_books_and_sidecars_without_in_place_swaps(installed_case: Path, phase: str) -> None:
    case = installed_case
    names = ("Alpha", "Beta", "Gamma")
    originals = {
        name: _book(case / "library" / name / "Old" / "book.cbz", name)
        for name in names
    }
    colors = {"Alpha": _png(255, 0, 0), "Beta": _png(0, 255, 0), "Gamma": _png(0, 0, 255)}
    for name in names:
        (case / "library" / name / "Old" / "book.png").write_bytes(colors[name])
    subject = """
    names = ('Alpha', 'Beta', 'Gamma')
    operations = []
    for index, name in enumerate(names):
        operations.append(Operation(f'mkdir-{index}', 'mkdir', root / name / 'New'))
    for index, name in enumerate(names):
        destination = root / names[(index + 1) % len(names)] / 'New'
        creator = f'mkdir-{(index + 1) % len(names)}'
        operations.extend((
            Operation(f'book-{index}', 'move', destination / 'book.cbz',
                      source=fingerprint(root / name / 'Old' / 'book.cbz', root), depends_on=(creator,)),
            Operation(f'art-{index}', 'move', destination / 'book.png',
                      source=fingerprint(root / name / 'Old' / 'book.png', root), depends_on=(creator,)),
        ))
    for index, name in enumerate(names):
        operations.append(Operation(f'remove-{index}', 'rmdir', root / name / 'Old', depends_on=(f'book-{index}', f'art-{index}')))
    plan = OperationPlan('crash-proof-v1', 'cyclic-directories', tuple(operations), (RootPermission(root),), 'cyclic_directory_relocation')
    def checkpoint(phase, member):
        selected = 'remove-0' if sys.argv[2] == 'cleanup_done' else 'book-0'
        if phase == sys.argv[2] and member == selected:
            pause(phase, member, plan.operation_id)
    result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
    report(result)
    """
    _kill(case, subject, phase)
    interrupted = _inspect(case)
    assert len(_events(interrupted, "cyclic-directories")) == (0 if phase == "published" else 12)
    first = case / "library" / "Beta" / "New" / "book.cbz"
    assert first.read_bytes() == originals["Alpha"]
    first_identity = _identity(first)
    recovered = _recover(case, "cyclic-directories")
    assert recovered["result"]["exit_code"] == 0
    record = _operation(recovered, "cyclic-directories")
    assert record["phase"] == "complete"
    assert _identity(first) == first_identity
    for index, name in enumerate(names):
        destination = case / "library" / names[(index + 1) % len(names)] / "New"
        assert (destination / "book.cbz").read_bytes() == originals[name]
        assert (destination / "book.png").read_bytes() == colors[name]
        assert not (case / "library" / name / "Old").exists()
    retained = {Path(member["cleanup_path"]).read_bytes() for member in record["members"] if member["kind"] == "move"}
    assert retained == set(originals.values()) | set(colors.values())
    assert len(_events(recovered, "cyclic-directories")) == 12
    before = _tree(case / "library")
    repeated = _recover(case, "cyclic-directories")
    assert repeated["result"]["exit_code"] == 0
    assert _tree(case / "library") == before
    assert _events(repeated, "cyclic-directories") == _events(recovered, "cyclic-directories")


@pytest.mark.parametrize("phase", ["published", "before_cleanup"])
def test_interrupted_two_book_and_sidecar_swap_recovers_verified_originals(installed_case: Path, phase: str) -> None:
    case = installed_case
    first = case / "library" / "first.cbz"
    second = case / "library" / "second.cbz"
    originals = {
        first: _book(first, "first complete book"), second: _book(second, "second complete book"),
        first.with_suffix(".png"): _png(255, 0, 0), second.with_suffix(".png"): _png(0, 255, 0),
    }
    for path in (first.with_suffix(".png"), second.with_suffix(".png")):
        path.write_bytes(originals[path])
    subject = """
    first, second = root / 'first.cbz', root / 'second.cbz'
    first_art, second_art = first.with_suffix('.png'), second.with_suffix('.png')
    plan = OperationPlan('crash-proof-v1', 'book-and-art-swap', (
        Operation('first-book', 'move', second, source=fingerprint(first, root),
                  destination_before=fingerprint(second, root), collision='replace'),
        Operation('first-art', 'move', second_art, source=fingerprint(first_art, root),
                  destination_before=fingerprint(second_art, root), collision='replace'),
        Operation('second-book', 'move', first, source=fingerprint(second, root),
                  destination_before=fingerprint(first, root), collision='replace'),
        Operation('second-art', 'move', first_art, source=fingerprint(second_art, root),
                  destination_before=fingerprint(first_art, root), collision='replace'),
    ), (RootPermission(root),), 'book_and_associated_artwork_swap')
    def checkpoint(phase, member):
        if phase == sys.argv[2] and member == 'first-book':
            pause(phase, member, plan.operation_id)
    result = TransactionExecutor(state, (root,), checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
    report(result)
    """
    _kill(case, subject, phase)
    interrupted = _inspect(case)
    assert second.read_bytes() == originals[first]
    assert len(_events(interrupted, "book-and-art-swap")) == (0 if phase == "published" else 4)
    first_publication = _identity(second)
    recovered = _recover(case, "book-and-art-swap")
    assert recovered["result"]["exit_code"] == 0
    assert recovered["result"]["counts"]["committed"] == int(phase == "published")
    assert _identity(second) == first_publication
    assert first.read_bytes() == originals[second]
    assert first.with_suffix(".png").read_bytes() == originals[second.with_suffix(".png")]
    assert second.with_suffix(".png").read_bytes() == originals[first.with_suffix(".png")]
    record = _operation(recovered, "book-and-art-swap")
    assert record["phase"] == "complete"
    retained = {Path(member["cleanup_path"]).read_bytes() for member in record["members"]}
    assert retained == set(originals.values())
    assert all(member["status"] == "complete" for member in record["members"])
    assert len(_events(recovered, "book-and-art-swap")) == 4
    before = _tree(case / "library")
    repeated = _recover(case, "book-and-art-swap")
    assert repeated["result"]["exit_code"] == 0
    assert _tree(case / "library") == before
    assert _events(repeated, "book-and-art-swap") == _events(recovered, "book-and-art-swap")


@pytest.mark.parametrize("timing", ["before", "after"])
def test_operation_intent_kill_before_any_stage_or_publication_has_truthful_restart(installed_case: Path, timing: str) -> None:
    case = installed_case
    source_bytes = _book(case / "library" / "source.cbz", "incoming book")
    target_bytes = _book(case / "library" / "target.cbz", "prior book")
    unrelated = case / "library" / ".kce-stage-user-owned"
    unrelated.write_bytes(b"User-owned name is not a recorded transaction stage")
    unrelated_identity = _identity(unrelated)
    subject = _MOVE_PLAN + textwrap.dedent("""
    real_record = StateStore.record_plan
    def record_plan(store, observed_plan, *args, **kwargs):
        if sys.argv[2] == 'before':
            pause('before_operation_intent', '', observed_plan.operation_id)
        result = real_record(store, observed_plan, *args, **kwargs)
        if sys.argv[2] == 'after':
            pause('after_operation_intent', '', observed_plan.operation_id)
        return result
    StateStore.record_plan = record_plan
    result = TransactionExecutor(state, (root,)).apply(plan, authorization=RunAuthorization(apply=True))
    report(result)
    """)
    checkpoint = _kill(case, subject, timing)
    assert checkpoint["phase"] == f"{timing}_operation_intent"
    interrupted = _inspect(case)
    assert _events(interrupted, "remaining-boundary") == []
    assert (case / "library" / "source.cbz").read_bytes() == source_bytes
    assert (case / "library" / "target.cbz").read_bytes() == target_bytes
    assert _identity(unrelated) == unrelated_identity
    if timing == "before":
        assert interrupted["journal"]["operations"] == []
        recovery = _recover(case, "remaining-boundary")
        assert recovery["result"]["exit_code"] == 2
        assert recovery["result"]["counts"]["committed"] == 0
        assert [decision["reason"] for decision in recovery["result"]["decisions"]] == ["operation_not_found"]
        assert recovery["journal"]["operations"] == []
        assert _events(recovery, "remaining-boundary") == []
        recovered = _run(case, _MOVE_PLAN + textwrap.dedent("""
        result = TransactionExecutor(state, (root,)).apply(plan, authorization=RunAuthorization(apply=True))
        report(result)
        """))
    else:
        record = _operation(interrupted, "remaining-boundary")
        assert record["phase"] == "planned"
        member, = record["members"]
        assert member["status"] == "planned"
        assert member["stage_path"] is None
        assert member["published_fingerprint"] is None
        recovered = _recover(case, "remaining-boundary")
    _assert_complete_move(case, recovered, source_bytes, target_bytes, committed=1)
    assert _identity(unrelated) == unrelated_identity
    before = _tree(case / "library")
    repeated = _recover(case, "remaining-boundary")
    _assert_complete_move(case, repeated, source_bytes, target_bytes, committed=0)
    assert _tree(case / "library") == before
    assert _events(repeated, "remaining-boundary") == _events(recovered, "remaining-boundary")

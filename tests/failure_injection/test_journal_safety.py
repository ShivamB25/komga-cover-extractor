from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor


def test_competing_recovery_cannot_change_live_writer_journal(tmp_path: Path) -> None:
    library = tmp_path / 'library'
    library.mkdir()
    state = tmp_path / 'state'
    ready = tmp_path / 'ready'
    proceed = tmp_path / 'proceed'
    script = '''import sys,time
from pathlib import Path
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import ArtifactSpec,Operation,OperationPlan,RootPermission
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
root,state,ready,proceed=map(Path,sys.argv[1:])
plan=OperationPlan('v1','active',(Operation('file','publish',root/'output',artifact=ArtifactSpec(content=b'committed')),),(RootPermission(root),))
def checkpoint(phase,member):
    if phase=='staged':
        ready.touch()
        deadline=time.monotonic()+10
        while not proceed.exists():
            if time.monotonic()>deadline: raise TimeoutError('test parent did not resume writer')
            time.sleep(.01)
result=TransactionExecutor(state,(root,),checkpoint=checkpoint).apply(plan,authorization=RunAuthorization(apply=True))
raise SystemExit(result.exit_code)
'''
    child = subprocess.Popen([sys.executable, '-I', '-c', script, str(library), str(state), str(ready), str(proceed)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        store = StateStore(state)
        before = store.get_operation('active')
        refused = TransactionExecutor(state, (library,)).recover('active', authorization=RunAuthorization(apply=True))
        prepared = TransactionExecutor(state, (library,)).prepare_state_root(
            authorization=RunAuthorization(apply=True),
        )
        assert (prepared.status, prepared.reason) == ("review_required", "cooperating_writer_lock_held")
        after = store.get_operation('active')
        assert refused.exit_code != 0
        assert before is not None and after is not None
        assert (after.phase, after.error) == (before.phase, before.error)
        proceed.touch()
        _, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert (library / 'output').read_bytes() == b'committed'
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_preview_leaves_hot_rollback_journal_untouched(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    store = StateStore(state)
    store.initialize()
    script = '''import os,sqlite3,sys
connection=sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA cache_size=5')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE metadata SET value=? WHERE key='store_id'", ('x'*2000000,))
os._exit(83)
'''
    crash = subprocess.run([sys.executable, '-I', '-c', script, str(store.path)], check=False)
    assert crash.returncode == 83
    journal = Path(str(store.path) + '-journal')
    assert journal.exists() and journal.stat().st_size > 0
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in state.iterdir() if path.is_file()}
    result = TransactionExecutor(state, (tmp_path,)).recover('missing', authorization=RunAuthorization())
    assert result.exit_code != 0
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in state.iterdir() if path.is_file()} == before


def test_state_preparation_cannot_repair_an_abandoned_owned_hot_journal(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    state = tmp_path / "state"
    script = '''import os,sqlite3,sys
from pathlib import Path
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import ArtifactSpec,Operation,OperationPlan,RootPermission
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
root,state=map(Path,sys.argv[1:])
plan=OperationPlan('hot-proof','abandoned-owned',(Operation('file','publish',root/'output',artifact=ArtifactSpec(content=b'original')),),(RootPermission(root),))
executor=TransactionExecutor(state,(root,))
def stop(phase,member):
    if phase=='staged':
        connection=sqlite3.connect(str(executor.state_store.path))
        connection.execute('PRAGMA cache_size=5')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("UPDATE metadata SET value=? WHERE key='store_id'",('x'*2000000,))
        os._exit(83)
executor.checkpoint=stop
executor.apply(plan,authorization=RunAuthorization(apply=True))
'''
    crashed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(library), str(state)],
        capture_output=True, timeout=20, check=False,
    )
    assert crashed.returncode == 83, crashed.stderr
    store = StateStore(state)
    journal = Path(str(store.path) + "-journal")
    assert journal.exists() and journal.stat().st_size > 0
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in state.iterdir() if path.is_file()}
    refused = TransactionExecutor(state, (library,)).prepare_state_root(
        authorization=RunAuthorization(apply=True),
    )
    assert (refused.status, refused.reason) == ("review_required", "abandoned_operation_lock_requires_recovery")
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in state.iterdir() if path.is_file()} == before
    assert not (library / "output").exists()
    recovered = TransactionExecutor(state, (library,)).recover(
        "abandoned-owned", authorization=RunAuthorization(apply=True),
    )
    assert (recovered.exit_code, recovered.committed, recovered.failed) == (0, 1, 0)
    assert (library / "output").read_bytes() == b"original"

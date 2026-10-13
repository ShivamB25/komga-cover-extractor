from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure.state import SCHEMA_VERSION, StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor


def _apply_candidate(state: Path, library: Path, operation_id: str) -> None:
    destination = library / "published.bin"
    plan = OperationPlan(
        "schema-safety-v1",
        operation_id,
        (Operation("candidate", "publish", destination, artifact=ArtifactSpec(content=b"candidate")),),
        (RootPermission(library),),
    )
    result = TransactionExecutor(state, (library,)).apply(
        plan, authorization=RunAuthorization(apply=True)
    )
    assert result.exit_code != 0
    assert not destination.exists()
    assert not list(library.iterdir())


@pytest.mark.parametrize("corruption", ("unknown-version", "trigger", "view", "index"))
def test_invalid_schema_is_rejected_before_publication(
    tmp_path: Path, corruption: str
) -> None:
    state = tmp_path / "state"
    library = tmp_path / "library"
    library.mkdir()
    store = StateStore(state)
    store.initialize()

    with sqlite3.connect(store.path) as connection:
        if corruption == "unknown-version":
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        elif corruption == "trigger":
            connection.execute(
                "CREATE TRIGGER discard_events BEFORE INSERT ON outbox "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        elif corruption == "view":
            connection.execute("CREATE VIEW event_view AS SELECT * FROM outbox")
        else:
            connection.execute("CREATE INDEX unexpected_event_idx ON outbox(event_type)")

    _apply_candidate(state, library, f"bad-schema-{corruption}")


def test_identity_sidecar_mismatch_is_rejected_before_publication(tmp_path: Path) -> None:
    state = tmp_path / "state"
    library = tmp_path / "library"
    library.mkdir()
    store = StateStore(state)
    identity = store.initialize()
    replacement = "f" * 32 if identity != "f" * 32 else "e" * 32
    (state / "store.id").write_text(f"{replacement}\n", encoding="ascii")

    _apply_candidate(state, library, "mismatched-store-identity")

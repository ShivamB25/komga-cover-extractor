from pathlib import Path

import pytest

from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor


@pytest.mark.parametrize(('size', 'accepted'), [(4, True), (5, False)])
def test_generated_artifact_budget_boundary(tmp_path: Path, size: int, accepted: bool) -> None:
    library = tmp_path / 'library'
    library.mkdir()
    state = tmp_path / 'state'
    target = library / 'output'
    data = b'x' * size
    plan = OperationPlan('v1', 'budget', (Operation('file', 'publish', target, artifact=ArtifactSpec(content=data)),), (RootPermission(library),))
    result = TransactionExecutor(state, (library,), max_stage_bytes=4).apply(plan, authorization=RunAuthorization(apply=True))
    if accepted:
        assert result.committed == 1 and target.read_bytes() == data
    else:
        assert result.exit_code == 2 and result.decisions[0].reason == 'staging_byte_limit'
        assert not target.exists() and not state.exists() and not list(library.iterdir())


def test_group_staging_budget_counts_sources_and_retained_originals(tmp_path: Path) -> None:
    library = tmp_path / 'library'
    library.mkdir()
    source = library / 'source'
    source.write_bytes(b'1234')
    target = library / 'target'
    operation = Operation('move', 'move', target, source=fingerprint(source, library))
    plan = OperationPlan('v1', 'move-budget', (operation,), (RootPermission(library),))
    state = tmp_path / 'state'
    rejected = TransactionExecutor(state, (library,), max_stage_bytes=7).apply(plan, authorization=RunAuthorization(apply=True))
    assert rejected.exit_code == 2 and source.read_bytes() == b'1234'
    assert not state.exists() and not target.exists()
    accepted = TransactionExecutor(state, (library,), max_stage_bytes=8).apply(plan, authorization=RunAuthorization(apply=True))
    assert accepted.committed == 1 and target.read_bytes() == b'1234' and not source.exists()
    assert any(path.is_file() and path.read_bytes() == b'1234' for path in library.glob('.kce-quarantine-*/source'))

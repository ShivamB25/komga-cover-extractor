from pathlib import Path

import pytest

from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure.filesystem import FilesystemError, fingerprint


def test_plan_rejects_shared_destinations_and_unsatisfied_dependencies(tmp_path: Path) -> None:
    target = tmp_path / 'cover.jpg'
    first = Operation('cover', 'publish', target, artifact=ArtifactSpec(content=b'one'))
    second = Operation('other', 'publish', target, artifact=ArtifactSpec(content=b'two'))
    with pytest.raises(ValueError):
        OperationPlan('policy', 'op', (first, second), (RootPermission(tmp_path),))
    dependency = Operation('dependent', 'publish', tmp_path / 'poster.jpg',
                           artifact=ArtifactSpec(content=b'two'), depends_on=('absent',))
    with pytest.raises(ValueError):
        OperationPlan('policy', 'op', (dependency,), (RootPermission(tmp_path),))


def test_replacement_requires_observed_prior_target(tmp_path: Path) -> None:
    target = tmp_path / 'cover.jpg'
    with pytest.raises(ValueError):
        Operation('cover', 'publish', target, artifact=ArtifactSpec(content=b'new'), collision='replace')
    target.write_bytes(b'old')
    old = fingerprint(target, tmp_path)
    operation = Operation('cover', 'publish', target, destination_before=old,
                          artifact=ArtifactSpec(content=b'new'), collision='replace')
    assert operation.destination_before is not None and operation.artifact is not None
    assert operation.destination_before.sha256 != operation.artifact.digest
    assert target.read_bytes() == b'old'


def test_fingerprints_detect_same_size_replacement_and_reject_links(tmp_path: Path) -> None:
    book = tmp_path / 'book.cbz'
    book.write_bytes(b'old')
    original = fingerprint(book, tmp_path)
    book.write_bytes(b'new')
    assert fingerprint(book, tmp_path) != original
    alias = tmp_path / 'alias.cbz'
    alias.symlink_to(book)
    with pytest.raises(FilesystemError):
        fingerprint(alias, tmp_path)
    outside = tmp_path.parent / (tmp_path.name + '-other')
    with pytest.raises(FilesystemError):
        fingerprint(outside, tmp_path)


@pytest.mark.parametrize(('status', 'exit_code'), [
    ('planned', 0), ('publication_failed', 1), ('missing_dependency', 2),
    ('conflict', 3), ('stale_plan', 3), ('delivery_deferred', 3),
])
def test_cli_exit_contract_distinguishes_failure_from_review(status, exit_code) -> None:
    assert RunResult((Decision(status, 'reason'),)).exit_code == exit_code


def test_local_and_remote_authorizations_do_not_imply_each_other() -> None:
    local = RunAuthorization(apply=True)
    remote = RunAuthorization(allow_remote=True)
    assert not local.allow_remote and not local.allow_torrent_removal
    assert not remote.apply and not remote.allow_torrent_removal

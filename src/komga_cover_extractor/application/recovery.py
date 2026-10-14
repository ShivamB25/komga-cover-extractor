"""Recovery is an explicit invocation, with the same authorization as publication."""
from dataclasses import replace
from pathlib import Path
from threading import Event

from ..domain.decisions import RunAuthorization, RunResult
from ..domain.libraries import LibraryRoot
from ..infrastructure.transactions import TransactionExecutor
from .conversion_workflow import release_conversion_artifacts


def recover_operation(
    operation_id: str,
    *,
    state_dir: Path,
    roots: tuple[Path, ...],
    authorization: RunAuthorization,
    max_stage_bytes: int = 8_589_934_592,
    cancel: Event | None = None,
) -> RunResult:
    result = TransactionExecutor(state_dir, roots, cancel=cancel, max_stage_bytes=max_stage_bytes).recover(
        operation_id, authorization=authorization,
    )
    if result.committed and authorization.apply:
        for plan in result.plans:
            settlement = release_conversion_artifacts(
                plan, roots=tuple(LibraryRoot(root) for root in roots),
                state_dir=state_dir, authorization=authorization,
            )
            if settlement is not None:
                result = replace(result, decisions=(*result.decisions, settlement))
    return result

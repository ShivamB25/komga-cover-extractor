"""Invocation authorization and outcomes shared by every entry point."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .operations import OperationPlan, PlanReport
from .releases import ReleaseSnapshot

Status = Literal[
    "observed", "planned", "committed", "skipped", "no_match", "ambiguous",
    "unsupported", "invalid_input", "missing_dependency", "stale_plan", "conflict",
    "permission_denied", "publication_failed", "review_required", "cancelled",
    "delivery_deferred", "failed",
]


@dataclass(frozen=True, slots=True)
class RunAuthorization:
    apply: bool = False
    allow_remote: bool = False
    allow_torrent_removal: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.apply, self.allow_remote, self.allow_torrent_removal)):
            raise ValueError("authorization requires explicit booleans")


@dataclass(frozen=True, slots=True)
class Decision:
    status: Status
    reason: str
    path: Path | None = None
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "reason": self.reason,
                "path": str(self.path) if self.path else None, "candidates": list(self.candidates)}


@dataclass(frozen=True, slots=True)
class RunResult:
    decisions: tuple[Decision, ...] = ()
    plans: tuple[OperationPlan, ...] = ()
    committed: int = 0
    skipped: int = 0
    failed: int = 0
    deferred_deliveries: tuple[str, ...] = ()
    releases: tuple[ReleaseSnapshot, ...] = ()
    plan_reports: tuple[PlanReport, ...] = ()
    missing_releases: tuple[dict[str, object], ...] = ()
    planned_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "plans", tuple(self.plans))
        object.__setattr__(self, "deferred_deliveries", tuple(self.deferred_deliveries))
        object.__setattr__(self, "releases", tuple(self.releases))
        object.__setattr__(self, "plan_reports", tuple(self.plan_reports))
        object.__setattr__(self, "missing_releases", tuple(self.missing_releases))
        if min(self.committed, self.skipped, self.failed) < 0:
            raise ValueError("result counts must be nonnegative")
        if self.planned_count is not None and (type(self.planned_count) is not int or self.planned_count < 0):
            raise ValueError("planned count must be a nonnegative integer")

    @property
    def planned(self) -> int:
        return self.planned_count if self.planned_count is not None else len(self.plans) + len(self.plan_reports)

    @property
    def exit_code(self) -> int:
        statuses = {decision.status for decision in self.decisions}
        if self.failed or statuses & {"publication_failed", "permission_denied", "failed"}:
            return 1
        if statuses & {"invalid_input", "unsupported", "missing_dependency"}:
            return 2
        if self.deferred_deliveries or statuses & {"ambiguous", "stale_plan", "conflict", "review_required", "cancelled", "delivery_deferred"}:
            return 3
        return 0

    def as_dict(self) -> dict[str, object]:
        plans = [report.as_dict() for report in self.plan_reports]
        plans.extend(plan.as_dict() for plan in self.plans)
        return {"ok": self.exit_code == 0, "exit_code": self.exit_code,
                "decisions": [item.as_dict() for item in self.decisions],
                "plans": plans,
                "missing_releases": list(self.missing_releases),
                "releases": [
                    {"path": str(release.path), "kind": release.kind,
                     "series": release.display_series, "number": release.number.to_dict(),
                     "year": release.year, "publisher": release.publisher,
                     "release_group": release.release_group, "premium": release.is_premium,
                     "extras": list(release.extras), "subtitle": release.subtitle,
                     "identifiers": list(release.identifiers), "provenance": list(release.provenance)}
                    for release in self.releases
                ],
                "counts": {"planned": self.planned, "committed": self.committed,
                           "skipped": self.skipped, "failed": self.failed},
                "deferred_deliveries": list(self.deferred_deliveries)}

"""Report local integer-volume gaps without filling explicit sets or decimals."""
from collections.abc import Sequence
from pathlib import Path

from ..domain.decisions import Decision, RunResult
from ..domain.releases import ReleaseSnapshot
from ..parsing.numbers import detect_missing_releases


def report_missing_releases(releases: Sequence[ReleaseSnapshot], *, max_expansion: int = 10_000) -> RunResult:
    detect_missing_releases((), max_expansion=max_expansion)
    folders: dict[Path, list[ReleaseSnapshot]] = {}
    for release in releases:
        if release.observation.root_role in {"library", "both"}:
            folders.setdefault(release.parent, []).append(release)
    reports: list[dict[str, object]] = []
    decisions: list[Decision] = []
    for directory, items in sorted(folders.items()):
        keys = {release.canonical_series_key for release in items}
        if len(keys) != 1 or "" in keys:
            decisions.append(Decision("review_required", "mixed_series_gap_report", directory))
            reports.append({"directory": str(directory), "series": directory.name,
                            "status": "review_required", "reason": "mixed_series_gap_report",
                            "missing_volumes": []})
            continue
        report = detect_missing_releases(
            tuple(release.number for release in items if release.kind == "volume"),
            max_expansion=max_expansion,
        )
        reports.append({"directory": str(directory), "series": items[0].display_series,
                        "status": report.status, "reason": report.reason,
                        "missing_volumes": list(report.missing), "observed_volumes": list(report.observed)})
        decisions.append(Decision(report.status if report.status == "review_required" else "observed",
                                  report.reason or "local_gaps_reported", directory))
    return RunResult(tuple(decisions), missing_releases=tuple(reports), releases=tuple(releases))

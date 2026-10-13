"""Verify distribution member identities and the complete frozen regression report."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

FROZEN_HASHES = {
    "komga_cover_extractor.py": "00e507f11af2b6078f3c374ac0be95908ca44d89d5b93b77c5e299a84208c23e",
    "settings.py": "b4d40983a89046fec3a48f2ac7694b402baaba4622fdbf513b88497c1a71c454",
    "tests.py": "a4c1f06298a932e0e67a901a23d03c7c70aff8c1c779c2892419fe11dc1d8de1",
    "manga_novel_dataset.csv": "a8c196f729286940dbbb6ff8efe4edfb3267187fbc71a1023973de65d8dcc7e7",
    "addons/qbit_torrent_unchecker/qbit_torrent_unchecker.py": "990677a1b42368a1bf1448a82118058cb6f30d9036f47852610a9ca9c8d3d46f",
}
LEGACY_FAILURES = frozenset({
    "test_remove_ignored_folders", "test_get_extra_from_group",
    "test_get_keyword_score", "test_has_multiple_numbers",
})
LEGACY_EXERCISES = frozenset({"test_rename_files", "test_sorting_volumes_by_volume_number"})
FROZEN_MEMBERS = (*FROZEN_HASHES, "blank_black.png", "blank_white.jpg", "LICENSE.md")


def verify_legacy_report(report: Mapping[str, Any]) -> None:
    if report.get("schema_version") != 1 or report.get("mode") != "all" or report.get("error"):
        raise ValueError("legacy characterization did not complete its full report")
    original_probe_hashes = {
        name: FROZEN_HASHES[name]
        for name in ("komga_cover_extractor.py", "settings.py", "tests.py", "manga_novel_dataset.csv")
    }
    if report.get("source_hashes") != original_probe_hashes:
        raise ValueError("legacy characterization used different original sources")
    tests = report.get("tests")
    if not isinstance(tests, list) or len(tests) != 41:
        raise ValueError("legacy characterization did not account for every original assertion and exercise")
    names = {row.get("name") for row in tests if isinstance(row, dict)}
    if len(names) != len(tests):
        raise ValueError("legacy characterization test identities are missing or repeated")
    failed = {row["name"] for row in tests if row.get("status") == "fail"}
    not_asserted = {row["name"] for row in tests if row.get("status") == "not_asserted"}
    if failed != LEGACY_FAILURES or not_asserted != LEGACY_EXERCISES:
        raise ValueError("legacy characterization differs from its four known failures and two unasserted exercises")
    if any(row.get("status") not in {"pass", "fail", "not_asserted"} for row in tests):
        raise ValueError("legacy characterization contains a blocked or unaccounted assertion")
    corpus = report.get("corpus")
    if not isinstance(corpus, dict) or any(corpus.get(key) != expected for key, expected in {
        "rows": 44714, "processed_rows": 44714, "validated_rows": 44465,
        "skipped_rows": 249, "status": "pass", "mismatch_counts": {}, "mismatches": [],
    }.items()):
        raise ValueError("legacy full-corpus accounting or labeled results changed")
    if corpus.get("legacy_validate_csv", {}).get("status") != "pass":
        raise ValueError("the original CSV validator did not complete")


def verify_modern_report(report: Mapping[str, Any]) -> None:
    if any(report.get(key) != expected for key, expected in {
        "schema_version": 1, "status": "pass", "rows": 44714,
        "eligible_rows": 44465, "original_excluded_rows": 249,
        "dataset_sha256": FROZEN_HASHES["manga_novel_dataset.csv"], "mismatches": [],
    }.items()):
        raise ValueError("modern corpus has missing rows or unexplained differences")
    dispositions = report.get("row_dispositions")
    if not isinstance(dispositions, list) or len(dispositions) != 44714:
        raise ValueError("modern corpus is missing per-row dispositions")
    rows = [item.get("row") for item in dispositions if isinstance(item, dict)]
    if rows != list(range(1, 44715)):
        raise ValueError("modern corpus row identities are incomplete or out of order")
    if any(item.get("status") not in {"matched", "intentional_correction", "original_exclusion"} for item in dispositions):
        raise ValueError("modern corpus includes an unresolved row")


def verify_distribution(dist: Path, source_root: Path) -> dict[str, object]:
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("the proof directory must contain exactly one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("wheel contains repeated member identities")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not (
                path.parts[0] == "komga_cover_extractor" or path.parts[0].endswith(".dist-info")
            ) or any(part in {"tests", "tools", "docs", "refactor-handoff", "__pycache__"} for part in path.parts):
                raise ValueError("wheel includes development, scratch or unrelated application inputs")
            if path.suffix in {".pyc", ".pyo"}:
                raise ValueError("wheel includes cached executable bytecode")
        for name in ("blank_black.png", "blank_white.jpg"):
            if archive.read(f"komga_cover_extractor/resources/{name}") != (source_root / name).read_bytes():
                raise ValueError("wheel blank-reference resource bytes changed")
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        relative = {"/".join(PurePosixPath(member.name).parts[1:]): member for member in members}
        if len(relative) != len(members):
            raise ValueError("sdist contains repeated member identities")
        for name in FROZEN_MEMBERS:
            target = f"tests/fixtures/legacy/{name}"
            member = relative.get(target)
            if member is None or not member.isfile():
                raise ValueError(f"sdist is missing the frozen fixture {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("sdist frozen member is unreadable")
            with stream:
                content = stream.read()
            expected = (source_root / target).read_bytes()
            if content != expected or (name in FROZEN_HASHES and hashlib.sha256(content).hexdigest() != FROZEN_HASHES[name]):
                raise ValueError("sdist does not retain the exact frozen characterization fixture")
        for name in ("tools/characterize_legacy.py", "tools/legacy_probe.py", "tools/characterize_modern.py", "uv.lock", "Dockerfile", "docker-entrypoint.sh"):
            if name not in relative:
                raise ValueError(f"sdist is missing its development consumer {name}")
    return {
        "wheel": wheel.name, "sdist": sdist.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
        "frozen_fixture_members": list(FROZEN_MEMBERS),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("legacy", "modern"):
        commands.add_parser(name).add_argument("report", type=Path)
    package = commands.add_parser("package")
    package.add_argument("dist", type=Path)
    package.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        if args.command == "package":
            evidence = verify_distribution(args.dist, args.source_root)
        else:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("report must be a JSON object")
            if args.command == "legacy":
                verify_legacy_report(report)
                evidence = {"known_original_failures": sorted(LEGACY_FAILURES), "validated_rows": 44465}
            else:
                verify_modern_report(report)
                evidence = {"eligible_rows": 44465, "accounted_rows": 44714}
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"{args.command} proof failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "proof": args.command, **evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

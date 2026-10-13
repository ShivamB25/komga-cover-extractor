"""Generate both pip consumers from the application's locked all extra."""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = (
    Path("requirements.txt"),
    Path("addons/qbit_torrent_unchecker/requirements.txt"),
)
HEADER = (
    "# Generated from uv.lock by python tools/export_requirements.py.\n"
    "# Do not edit dependency pins here. Both launchers use the installed application.\n"
    "# Install the application wheel with [all] after these locked dependencies.\n"
)


def export_snapshot(root: Path, *, frozen: bool = False) -> str:
    result = subprocess.run(
        [
            "uv", "export", "--frozen" if frozen else "--locked",
            "--format", "requirements.txt", "--extra", "all",
            "--no-default-groups", "--no-emit-project", "--no-editable",
            "--no-header", "--no-annotate",
        ],
        cwd=root, check=True, stdout=subprocess.PIPE, text=True,
    )
    return HEADER + result.stdout


def publish_snapshot(root: Path, snapshot: str, *, check: bool = False) -> int:
    stale = []
    for relative in TARGETS:
        target = root / relative
        if check:
            if not target.is_file() or target.read_text(encoding="utf-8") != snapshot:
                stale.append(str(relative))
        else:
            target.write_text(snapshot, encoding="utf-8")
    if stale:
        print("Stale lock-derived requirements: " + ", ".join(stale), file=sys.stderr)
        print("Regenerate with python tools/export_requirements.py.", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare without changing either consumer")
    mode.add_argument("--frozen", action="store_true", help="export the existing lock without checking project freshness")
    args = parser.parse_args(argv)
    try:
        return publish_snapshot(ROOT, export_snapshot(ROOT, frozen=args.frozen), check=args.check)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"requirements export failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

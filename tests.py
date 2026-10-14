#!/usr/bin/env python3
"""Run modern behavioral checks; frozen characterization remains separate."""
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path


def main() -> int:
    try:
        distribution("pytest")
    except PackageNotFoundError:
        print("Install development dependencies first: uv sync --locked --all-extras --group legacy", file=sys.stderr)
        return 2
    return subprocess.call(
        [sys.executable, "-I", "-m", "pytest", *sys.argv[1:]],
        cwd=Path(__file__).resolve().parent,
    )


if __name__ == "__main__":
    raise SystemExit(main())

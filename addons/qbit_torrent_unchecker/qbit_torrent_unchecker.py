#!/usr/bin/env python3
"""Run the installed torrent service with historical argument translation."""
import os
import sys
from importlib.metadata import PackageNotFoundError, distribution


def main() -> int:
    try:
        distribution("komga-cover-extractor")
    except PackageNotFoundError:
        print(
            "Install Komga Cover Extractor in this interpreter first: "
            "python -m pip install '.[all]' from the project directory.",
            file=sys.stderr,
        )
        return 2
    os.execv(
        sys.executable,
        [sys.executable, "-I", "-m", "komga_cover_extractor", "legacy-qbit", *sys.argv[1:]],
    )


if __name__ == "__main__":
    raise SystemExit(main())

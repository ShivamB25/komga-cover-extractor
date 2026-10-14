"""Verify current wheel and development sdist members against maintained sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath


def verify_distribution(dist: Path, source_root: Path) -> dict[str, object]:
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("the proof directory must contain exactly one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    package_sources = {
        path.relative_to(source_root / "src").as_posix(): path
        for path in (source_root / "src/komga_cover_extractor").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }
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
        package_members = {name for name in names if name.startswith("komga_cover_extractor/")}
        if package_members != package_sources.keys():
            raise ValueError("wheel does not contain exactly the current maintained application")
        for name, source in package_sources.items():
            if archive.read(name) != source.read_bytes():
                raise ValueError(f"wheel application member differs from current source: {name}")
        for name in ("blank_black.png", "blank_white.jpg"):
            if archive.read(f"komga_cover_extractor/resources/{name}") != (source_root / name).read_bytes():
                raise ValueError("wheel blank-reference resource bytes changed")
    sdist_sources = {
        name: source_root / name
        for name in (
            "README.md", "docs/architecture.md", "docs/configuration.md", "docs/recovery.md",
            "Dockerfile", "docker-entrypoint.sh", ".dockerignore", ".gitignore",
            ".github/workflows/github.yaml", ".github/workflows/docker-hub.yml",
            "komga_cover_extractor.py", "tests.py",
            "addons/qbit_torrent_unchecker/qbit_torrent_unchecker.py",
            "addons/qbit_torrent_unchecker/requirements.txt", "requirements.txt",
            "tools/export_requirements.py", "tools/verify_distribution.py",
            "tools/verify_installed.py", "tools/verify_container.py", "tools/verify_deployment.sh",
            "pyproject.toml", "uv.lock", ".python-version", "LICENSE.md",
            "blank_black.png", "blank_white.jpg",
        )
    }
    for directory in ("src", "tests/fixtures/bookwalker", "tests/fixtures/native"):
        for source_path in (source_root / directory).rglob("*"):
            if source_path.is_file() and not any(part in {"__pycache__", ".pytest_cache"} for part in source_path.parts):
                if source_path.suffix not in {".pyc", ".pyo"}:
                    sdist_sources[source_path.relative_to(source_root).as_posix()] = source_path
    for source_path in (source_root / "tests").rglob("*.py"):
        if "__pycache__" not in source_path.parts:
            sdist_sources[source_path.relative_to(source_root).as_posix()] = source_path
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        relative: dict[str, tarfile.TarInfo] = {}
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or len(path.parts) < 2 or not member.isfile():
                raise ValueError("sdist includes unsafe or non-file members")
            roots.add(path.parts[0])
            name = "/".join(path.parts[1:])
            if name in relative:
                raise ValueError("sdist contains repeated member identities")
            relative[name] = member
        if len(roots) != 1 or relative.keys() != sdist_sources.keys() | {"PKG-INFO"}:
            raise ValueError("sdist does not contain exactly the current maintained development inputs")
        for name, source in sdist_sources.items():
            stream = archive.extractfile(relative[name])
            if stream is None:
                raise ValueError(f"sdist member is unreadable: {name}")
            with stream:
                if stream.read() != source.read_bytes():
                    raise ValueError(f"sdist member differs from current source: {name}")
    return {
        "wheel": wheel.name, "sdist": sdist.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
        "application_members": sorted(package_sources),
        "development_members": sorted(sdist_sources),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package.add_argument("dist", type=Path)
    package.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        evidence = verify_distribution(args.dist, args.source_root)
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"{args.command} proof failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "proof": args.command, **evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

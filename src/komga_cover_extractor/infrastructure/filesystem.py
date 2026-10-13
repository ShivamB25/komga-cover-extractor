"""Filesystem observations used as publication preconditions, never authorization."""
from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from ..domain.operations import FileFingerprint

BOOK_SUFFIXES = frozenset({".cbz", ".zip", ".epub", ".cbr", ".rar", ".7z", ".cb7"})
OWNED_PREFIX = ".kce-"


class FilesystemError(OSError):
    def __init__(self, reason: str, path: Path):
        self.reason = reason
        self.path = path
        super().__init__(reason)


def checked_path(path: Path, root: Path, *, allow_missing: bool = False) -> Path:
    """Reject lexical escapes and every observed symlink component, including root."""
    if ".." in path.parts or ".." in root.parts:
        raise FilesystemError("noncanonical_path", path)
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if not path.is_relative_to(root):
        raise FilesystemError("outside_authorized_root", path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                break
            raise FilesystemError("missing_path", current) from None
        if stat.S_ISLNK(info.st_mode):
            raise FilesystemError("symlink_traversal", current)
    return path


def fingerprint(path: Path, root: Path) -> FileFingerprint:
    path = checked_path(path, root)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise FilesystemError("not_regular_file", path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise FilesystemError("not_regular_file", path)
        hasher = hashlib.sha256()
        count = 0
        while chunk := stream.read(min(1024 * 1024, before.st_size - count + 1)):
            count += len(chunk)
            if count > before.st_size:
                raise FilesystemError("source_changed", path)
            hasher.update(chunk)
        digest = hasher.hexdigest()
        after = os.fstat(stream.fileno())
    current = path.lstat()
    def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise FilesystemError("source_changed", path)
    return FileFingerprint(root, path.relative_to(root).as_posix(), before.st_dev,
                           before.st_ino, before.st_size, before.st_mtime_ns, digest)


def matches(observation: FileFingerprint) -> bool:
    return fingerprint(observation.path, observation.root) == observation


def book_paths(root: Path) -> Iterator[Path]:
    checked_path(root, root)
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith((".", OWNED_PREFIX)))
        for name in sorted(files):
            path = Path(directory) / name
            if not name.startswith(".") and path.suffix.lower() in BOOK_SUFFIXES:
                checked_path(path, root)
                yield path


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .releases import ReleaseSnapshot


def _absolute_lexical(path: Path) -> Path:
    """Return an absolute normalized path without resolving links or touching disk."""
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    return Path(os.path.abspath(os.fspath(path)))


def _string_tuple(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
        raise TypeError(f"{name} must be a tuple of strings")
    return values


@dataclass(frozen=True, slots=True)
class LibraryRoot:
    path: Path
    formats: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    library_types: tuple[str, ...] = ()
    roles: tuple[str, ...] = ("library",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _absolute_lexical(self.path))
        for name in ("formats", "extensions", "library_types", "roles"):
            values = _string_tuple(name, getattr(self, name))
            if name == "roles" and (not values or any(not value for value in values)):
                raise ValueError("roles must contain at least one non-empty role")
            object.__setattr__(self, name, values)

    def contains(self, candidate: Path) -> bool:
        """Component-aware lexical containment; symlink policy belongs to I/O owners."""
        return is_within_root(candidate, self.path)


@dataclass(frozen=True, slots=True)
class PathMapping:
    local_root: Path
    remote_root: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_root", _absolute_lexical(self.local_root))
        if not isinstance(self.remote_root, str) or not self.remote_root.strip():
            raise ValueError("remote_root must be a non-empty POSIX path")
        # Remote paths are always interpreted with POSIX separators, even on Windows.
        # A drive-like prefix (for example C:/media) is a component, not something to strip.
        normalized = posixpath.normpath(self.remote_root.replace("\\", "/"))
        if normalized in {"", "."}:
            raise ValueError("remote_root must identify a path")
        object.__setattr__(self, "remote_root", normalized)

    def map_path(self, path: Path) -> str | None:
        """Map a contained local path to its POSIX remote path, or return None."""
        local = _absolute_lexical(path)
        try:
            relative = local.relative_to(self.local_root)
        except ValueError:
            return None
        parts = relative.parts
        if not parts:
            return self.remote_root
        return posixpath.join(self.remote_root, *parts)


@dataclass(frozen=True, slots=True, eq=False)
class LibrarySnapshot:
    releases: tuple[ReleaseSnapshot, ...] = ()
    roots: tuple[LibraryRoot, ...] = ()
    available: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.releases, tuple) or not isinstance(self.roots, tuple):
            raise TypeError("releases and roots must be tuples")
        if any(not isinstance(root, LibraryRoot) for root in self.roots):
            raise TypeError("roots must contain LibraryRoot values")
        if type(self.available) is not bool:
            raise TypeError("available must be a bool")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")

    def contains(self, path: Path) -> bool:
        return any(root.contains(path) for root in self.roots)

    def map_path(self, path: Path, mappings: tuple[PathMapping, ...]) -> str | None:
        return map_local_path(path, mappings)


def is_within_root(path: Path, root: Path) -> bool:
    """Return whether path is root or a descendant by path components."""
    candidate = _absolute_lexical(path)
    boundary = _absolute_lexical(root)
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


def normalize_library_roots(roots: tuple[LibraryRoot, ...]) -> tuple[LibraryRoot, ...]:
    """Merge role aliases for identical roots and reject contradictory/nested roots."""
    if not isinstance(roots, tuple) or any(not isinstance(root, LibraryRoot) for root in roots):
        raise TypeError("roots must be a tuple of LibraryRoot values")
    by_path: dict[Path, LibraryRoot] = {}
    order: list[Path] = []
    for root in roots:
        prior = by_path.get(root.path)
        if prior is None:
            by_path[root.path] = root
            order.append(root.path)
            continue
        if (prior.formats, prior.extensions, prior.library_types) != (
            root.formats, root.extensions, root.library_types
        ):
            raise ValueError(f"conflicting settings for duplicate root: {root.path}")
        roles = tuple(dict.fromkeys((*prior.roles, *root.roles)))
        by_path[root.path] = LibraryRoot(
            path=root.path,
            formats=prior.formats,
            extensions=prior.extensions,
            library_types=prior.library_types,
            roles=roles,
        )

    result = tuple(by_path[path] for path in order)
    for index, root in enumerate(result):
        for other in result[index + 1 :]:
            if is_within_root(other.path, root.path) or is_within_root(root.path, other.path):
                raise ValueError(f"nested library roots are not allowed: {root.path} and {other.path}")
    return result


def normalize_path_mappings(mappings: tuple[PathMapping, ...]) -> tuple[PathMapping, ...]:
    """Reject duplicate local roots unless their remote destinations agree."""
    if not isinstance(mappings, tuple) or any(not isinstance(item, PathMapping) for item in mappings):
        raise TypeError("mappings must be a tuple of PathMapping values")
    by_local: dict[Path, PathMapping] = {}
    order: list[Path] = []
    for mapping in mappings:
        prior = by_local.get(mapping.local_root)
        if prior is None:
            by_local[mapping.local_root] = mapping
            order.append(mapping.local_root)
        elif prior.remote_root != mapping.remote_root:
            raise ValueError(f"conflicting mappings for local root: {mapping.local_root}")
    return tuple(by_local[path] for path in order)


def map_local_path(path: Path, mappings: tuple[PathMapping, ...]) -> str | None:
    """Use the most-specific matching local root and preserve remote path spelling."""
    normalized = normalize_path_mappings(mappings)
    candidate = _absolute_lexical(path)
    matches: list[tuple[int, PathMapping]] = []
    for mapping in normalized:
        try:
            candidate.relative_to(mapping.local_root)
        except ValueError:
            continue
        matches.append((len(mapping.local_root.parts), mapping))
    if not matches:
        return None
    longest = max(length for length, _ in matches)
    selected = [mapping for length, mapping in matches if length == longest]
    # Equal-depth, distinct roots cannot both component-contain one path, but keep
    # this guard explicit in case a future path representation changes that fact.
    if len(selected) != 1:
        raise ValueError("ambiguous equally specific path mappings")
    return selected[0].map_path(candidate)

"""Immutable observations and release-number values used by the domain layer.

This module deliberately performs no filesystem, archive, configuration, or
network access. Callers provide every observation explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast

from .operations import FileFingerprint

ReleaseNumberKind = Literal["missing", "scalar", "range", "set"]

_NUMBER_KINDS = frozenset(("missing", "scalar", "range", "set"))


def _decimal_from_token(token: str) -> Decimal:
    if not isinstance(token, str):
        raise TypeError("release number tokens must be strings")
    if not token:
        raise ValueError("release number tokens cannot be empty")
    try:
        value = Decimal(token)
    except InvalidOperation as error:
        raise ValueError(f"invalid decimal release token: {token!r}") from error
    if not value.is_finite():
        raise ValueError("release number tokens must be finite decimals")
    return value


def _validate_limit(max_expansion: int) -> None:
    if isinstance(max_expansion, bool) or not isinstance(max_expansion, int):
        raise TypeError("max_expansion must be an integer")
    if max_expansion < 1:
        raise ValueError("max_expansion must be positive")


def _as_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("release values must not be converted from floats")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        result = _decimal_from_token(value)
    else:
        raise TypeError("release values must be Decimal, int, or decimal text")
    if not result.is_finite():
        raise ValueError("release values must be finite decimals")
    return result


@dataclass(frozen=True, slots=True)
class ReleaseNumber:
    """A known or missing release number without lossy numeric conversion.

    ``raw_tokens`` preserve the exact source spelling for later rendering.
    Ranges represent inclusive endpoints; sets represent only the explicitly
    listed members and never imply the values between them.
    """

    kind: ReleaseNumberKind
    values: tuple[Decimal, ...] = ()
    raw_tokens: tuple[str, ...] = ()
    part: int | Decimal | None = None

    def __post_init__(self) -> None:
        if self.kind not in _NUMBER_KINDS:
            raise ValueError(f"unsupported release number kind: {self.kind!r}")

        values = tuple(self.values)
        raw_tokens = tuple(self.raw_tokens)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "raw_tokens", raw_tokens)

        if self.part is not None and (
            isinstance(self.part, bool) or not isinstance(self.part, (int, Decimal))
            or isinstance(self.part, Decimal) and not self.part.is_finite()
            or self.part < 0
        ):
            raise ValueError("part must be a finite nonnegative integer or Decimal, or None")
        if any(not isinstance(value, Decimal) for value in values):
            raise TypeError("release number values must be Decimal instances")
        if any(not isinstance(token, str) for token in raw_tokens):
            raise TypeError("release number raw_tokens must contain strings")
        if len(values) != len(raw_tokens):
            raise ValueError("each release number value must have one source token")
        if any(not value.is_finite() for value in values):
            raise ValueError("release number values must be finite decimals")

        parsed_tokens = tuple(_decimal_from_token(token) for token in raw_tokens)
        if any(value.as_tuple() != token.as_tuple() for value, token in zip(values, parsed_tokens, strict=True)):
            raise ValueError("release number values must be constructed from raw_tokens")

        if self.kind == "missing":
            if values or raw_tokens:
                raise ValueError("a missing release number cannot have values or tokens")
        elif self.kind == "scalar":
            if len(values) != 1:
                raise ValueError("a scalar release number must have exactly one value")
        elif self.kind == "range":
            if len(values) != 2:
                raise ValueError("a range release number must have two endpoints")
            if values[0] > values[1]:
                raise ValueError("range release number endpoints must be ordered")
        elif not values:
            raise ValueError("a set release number must contain at least one member")

    @classmethod
    def missing(cls, *, part: int | Decimal | None = None) -> ReleaseNumber:
        """Create a missing number while retaining an independently known part."""

        return cls(kind="missing", part=part)

    @classmethod
    def from_tokens(
        cls,
        kind: ReleaseNumberKind,
        raw_tokens: Sequence[str],
        *,
        part: int | Decimal | None = None,
    ) -> ReleaseNumber:
        """Construct exact Decimal values directly from source tokens."""

        if isinstance(raw_tokens, str):
            raise TypeError("raw_tokens must be a sequence of tokens, not one string")
        tokens = tuple(raw_tokens)
        values = tuple(_decimal_from_token(token) for token in tokens)
        return cls(kind=kind, values=values, raw_tokens=tokens, part=part)

    @classmethod
    def scalar(cls, token: str, *, part: int | Decimal | None = None) -> ReleaseNumber:
        return cls.from_tokens("scalar", (token,), part=part)

    @classmethod
    def range(
        cls, start: str, end: str, *, part: int | Decimal | None = None
    ) -> ReleaseNumber:
        return cls.from_tokens("range", (start, end), part=part)

    @classmethod
    def explicit_set(
        cls, tokens: Sequence[str], *, part: int | Decimal | None = None
    ) -> ReleaseNumber:
        return cls.from_tokens("set", tokens, part=part)

    def contains(self, value: Decimal | int | str) -> bool:
        """Return whether a numeric value is a member of this release number."""

        return release_number_contains(self, value)

    def covers(self, other: ReleaseNumber) -> bool:
        """Return whether this explicit numeric domain covers ``other``."""

        return release_number_covers(self, other)

    def expand(self, *, max_expansion: int = 10_000) -> tuple[Decimal, ...]:
        """Return explicit members, expanding only bounded integral ranges.

        A range with fractional endpoints is intentionally not expanded: the
        value object cannot infer which chapter increments its producer meant.
        """

        return expand_release_number(self, max_expansion=max_expansion)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation with exact decimal strings."""

        return {
            "kind": self.kind,
            "values": [str(value) for value in self.values],
            "raw_tokens": list(self.raw_tokens),
            "part": str(self.part) if isinstance(self.part, Decimal) else self.part,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ReleaseNumber:
        """Rebuild a value from :meth:`to_dict` output without float input."""

        expected_keys = {"kind", "values", "raw_tokens", "part"}
        if set(data) != expected_keys:
            raise ValueError("release number data has unexpected or missing fields")
        kind = data["kind"]
        if not isinstance(kind, str) or kind not in _NUMBER_KINDS:
            raise ValueError("release number data has an invalid kind")
        tokens_value = data["raw_tokens"]
        values_value = data["values"]
        if not isinstance(tokens_value, Sequence) or isinstance(tokens_value, str):
            raise TypeError("serialized raw_tokens must be a sequence of strings")
        if not isinstance(values_value, Sequence) or isinstance(values_value, str):
            raise TypeError("serialized values must be a sequence of decimal strings")
        if any(not isinstance(token, str) for token in tokens_value):
            raise TypeError("serialized raw_tokens must contain strings")
        if any(not isinstance(value, str) for value in values_value):
            raise TypeError("serialized values must contain decimal strings")

        part_value = data["part"]
        if isinstance(part_value, str):
            part: int | Decimal | None = _decimal_from_token(part_value)
        elif part_value is None or type(part_value) is int:
            part = part_value
        else:
            raise TypeError("serialized part must be an integer, decimal string, or None")
        result = cls.from_tokens(
            cast(ReleaseNumberKind, kind), cast(Sequence[str], tokens_value), part=part,
        )
        serialized_values = tuple(_decimal_from_token(value) for value in cast(Sequence[str], values_value))
        if len(serialized_values) != len(result.values) or any(
            value.as_tuple() != expected.as_tuple()
            for value, expected in zip(serialized_values, result.values, strict=True)
        ):
            raise ValueError("serialized values do not match their raw source tokens")
        return result


def missing_release_number(*, part: int | Decimal | None = None) -> ReleaseNumber:
    """Factory spelling for consumers that prefer a named free function."""

    return ReleaseNumber.missing(part=part)


def release_number_contains(
    number: ReleaseNumber, value: Decimal | int | str
) -> bool:
    """Test membership without converting through binary floating point."""

    candidate = _as_decimal(value)
    if number.kind == "missing":
        return False
    if number.kind == "scalar":
        return candidate == number.values[0]
    if number.kind == "range":
        return number.values[0] <= candidate <= number.values[1]
    return candidate in number.values


def release_number_covers(container: ReleaseNumber, candidate: ReleaseNumber) -> bool:
    """Test set/range coverage while preserving explicit set semantics."""

    if container.kind == "missing" or candidate.kind == "missing":
        return False
    if container.part != candidate.part:
        return False
    if candidate.kind == "range":
        if container.kind != "range":
            return False
        return (
            container.values[0] <= candidate.values[0]
            and container.values[1] >= candidate.values[1]
        )
    return all(container.contains(value) for value in candidate.values)


def compare_release_numbers(left: ReleaseNumber, right: ReleaseNumber) -> int | None:
    """Compare unambiguous scalar releases, returning ``None`` otherwise.

    Ranges and explicit sets have no implied total ordering. A part mismatch is
    also incomparable; callers that order parts must apply their own policy.
    """

    if left.kind != "scalar" or right.kind != "scalar" or left.part != right.part:
        return None
    left_value, right_value = left.values[0], right.values[0]
    return (left_value > right_value) - (left_value < right_value)


def expand_release_number(
    number: ReleaseNumber, *, max_expansion: int = 10_000
) -> tuple[Decimal, ...]:
    """Expand a release value into explicit members under a hard size bound."""

    _validate_limit(max_expansion)
    if number.kind != "range":
        if len(number.values) > max_expansion:
            raise ValueError("release number expansion exceeds max_expansion")
        return number.values

    start, end = number.values
    if start != start.to_integral_value() or end != end.to_integral_value():
        raise ValueError("fractional ranges cannot be expanded without parser policy")
    first, last = int(start), int(end)
    count = last - first + 1
    if count > max_expansion:
        raise ValueError("release number expansion exceeds max_expansion")
    return tuple(Decimal(value) for value in range(first, last + 1))


@dataclass(frozen=True, slots=True)
class ArchivePageObservation:
    """A caller-observed archive page; ``None`` means grayscale was not checked."""

    name: str
    is_grayscale: bool | None = None
    is_directory: bool = False

    def __post_init__(self) -> None:
        if type(self.is_directory) is not bool:
            raise TypeError("archive directory observation must be a boolean")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("archive page name must be a non-empty string")
        if self.is_grayscale is not None and not isinstance(self.is_grayscale, bool):
            raise TypeError("is_grayscale must be bool or None")


@dataclass(frozen=True, slots=True)
class FileObservation:
    """Explicit filesystem/archive evidence supplied to pure release parsing.

    Metadata is an ordered tuple of raw name/value pairs so repeated XML values
    remain observable. Siblings and archive pages are observations, not requests
    for this value object to scan or open anything.
    """

    path: Path
    siblings: tuple[Path, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    root_role: str = "unknown"
    archive_pages: tuple[ArchivePageObservation, ...] = ()
    fingerprint: FileFingerprint | None = None
    at_configured_root: bool = False

    def __post_init__(self) -> None:
        if type(self.at_configured_root) is not bool:
            raise TypeError("root placement must be a boolean observation")
        if self.root_role not in {"library", "download", "both", "unknown"}:
            raise ValueError("unsupported observed root role")
        if self.fingerprint is not None and not isinstance(self.fingerprint, FileFingerprint):
            raise TypeError("fingerprint must be an observed FileFingerprint")
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(
            self, "siblings", tuple(Path(sibling) for sibling in self.siblings)
        )
        metadata = tuple(tuple(pair) for pair in self.metadata)
        if any(
            len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
            for pair in metadata
        ):
            raise TypeError("metadata must contain ordered string name/value pairs")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "archive_pages", tuple(self.archive_pages))
        if not isinstance(self.root_role, str) or not self.root_role.strip():
            raise ValueError("root_role must be a non-empty string")
        if any(not isinstance(page, ArchivePageObservation) for page in self.archive_pages):
            raise TypeError("archive_pages must contain ArchivePageObservation values")


@dataclass(frozen=True, slots=True, eq=False)
class ReleaseSnapshot:
    """A parsed release tied to one observation, with identity equality.

    Two snapshots are never equal merely because their parsed fields happen to
    match. Consumers must choose an explicit path, content, or semantic key for
    deduplication.
    """

    observation: FileObservation
    number: ReleaseNumber = field(default_factory=ReleaseNumber.missing)
    kind: str = "unknown"
    display_series: str = ""
    canonical_series_key: str = ""
    provenance: tuple[str, ...] = ()
    year: int | None = None
    release_group: str | None = None
    publisher: str | None = None
    is_premium: bool = False
    extras: tuple[str, ...] = ()
    subtitle: str | None = None
    identifiers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.observation, FileObservation):
            raise TypeError("observation must be a FileObservation")
        if not isinstance(self.number, ReleaseNumber):
            raise TypeError("number must be a ReleaseNumber")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be a non-empty string")
        if not isinstance(self.display_series, str):
            raise TypeError("display_series must be a string")
        if not isinstance(self.canonical_series_key, str):
            raise TypeError("canonical_series_key must be a string")
        if self.year is not None and (
            isinstance(self.year, bool) or not isinstance(self.year, int)
        ):
            raise TypeError("year must be an integer or None")
        if not isinstance(self.is_premium, bool):
            raise TypeError("is_premium must be bool")
        for field_name in ("release_group", "publisher", "subtitle"):
            field_value = getattr(self, field_name)
            if field_value is not None and not isinstance(field_value, str):
                raise TypeError(f"{field_name} must be a string or None")

        provenance = tuple(self.provenance)
        extras = tuple(self.extras)
        identifiers = tuple(tuple(pair) for pair in self.identifiers)
        if any(not isinstance(source, str) for source in provenance):
            raise TypeError("provenance must contain strings")
        if any(not isinstance(extra, str) for extra in extras):
            raise TypeError("extras must contain strings")
        if any(
            len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
            for pair in identifiers
        ):
            raise TypeError("identifiers must contain ordered string name/value pairs")
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "extras", extras)
        object.__setattr__(self, "identifiers", identifiers)

    @property
    def path(self) -> Path:
        return self.observation.path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def parent(self) -> Path:
        return self.path.parent

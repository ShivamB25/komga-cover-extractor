import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from komga_cover_extractor.domain.releases import (
    ArchivePageObservation,
    FileObservation,
    ReleaseNumber,
    ReleaseSnapshot,
    compare_release_numbers,
    expand_release_number,
    missing_release_number,
    release_number_covers,
)


def test_missing_scalar_range_and_explicit_set_remain_distinct() -> None:
    missing = ReleaseNumber.missing()
    scalar = ReleaseNumber.scalar("0")
    interval = ReleaseNumber.range("1.01", "1.10")
    explicit = ReleaseNumber.explicit_set(("1", "3"))

    assert (missing.kind, missing.values, missing.raw_tokens) == ("missing", (), ())
    assert scalar.values == (Decimal("0"),)
    assert scalar.contains("0")
    assert interval.values == (Decimal("1.01"), Decimal("1.10"))
    assert interval.raw_tokens == ("1.01", "1.10")
    assert explicit.kind == "set"
    assert explicit.values == (Decimal("1"), Decimal("3"))
    assert explicit.contains(Decimal("1"))
    assert not explicit.contains("2")
    assert not missing.contains(Decimal("0"))


def test_decimal_source_precision_and_part_are_retained() -> None:
    number = ReleaseNumber.scalar("1.100", part=12)

    assert number.values[0].as_tuple() == Decimal("1.100").as_tuple()
    assert number.raw_tokens == ("1.100",)
    assert number.part == 12
    assert number.to_dict() == {
        "kind": "scalar",
        "values": ["1.100"],
        "raw_tokens": ["1.100"],
        "part": 12,
    }
    assert ReleaseNumber.from_dict(number.to_dict()) == number


@pytest.mark.parametrize("part", [0, Decimal("1.5"), Decimal("1.000000000000000001")])
def test_zero_and_fractional_parts_survive_json_without_aliasing_release_numbers(
    part: int | Decimal,
) -> None:
    number = ReleaseNumber.scalar("53", part=part)
    restored = ReleaseNumber.from_dict(json.loads(json.dumps(number.to_dict())))
    assert restored.part == part and restored.values == (Decimal("53"),)
    assert restored.covers(number) and number.covers(restored)
    assert not restored.covers(ReleaseNumber.scalar("53"))
    assert not restored.covers(ReleaseNumber.scalar("53.15"))
    assert not restored.covers(ReleaseNumber.scalar("53", part=12))


@pytest.mark.parametrize("part", [-1, Decimal("-0.5"), Decimal("NaN"), Decimal("Infinity")])
def test_negative_and_nonfinite_parts_cannot_enter_release_identity(part: int | Decimal) -> None:
    with pytest.raises(ValueError):
        ReleaseNumber.scalar("53", part=part)


def test_release_number_rejects_invalid_shapes_and_float_values() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ReleaseNumber("scalar", (), ())
    with pytest.raises(ValueError, match="ordered"):
        ReleaseNumber.range("3", "1")
    with pytest.raises(ValueError, match="finite"):
        ReleaseNumber.scalar("NaN")
    with pytest.raises(TypeError, match="Decimal"):
        ReleaseNumber("scalar", (1.0,), ("1",))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="floats"):
        ReleaseNumber.scalar("1").contains(1.0)  # type: ignore[arg-type]


def test_coverage_and_comparison_are_explicit_for_each_kind() -> None:
    wide_range = ReleaseNumber.range("1", "3")
    narrow_range = ReleaseNumber.range("2", "3")
    explicit = ReleaseNumber.explicit_set(("1", "3"))
    scalar = ReleaseNumber.scalar("2")

    assert release_number_covers(wide_range, narrow_range)
    assert wide_range.covers(explicit)
    assert not explicit.covers(wide_range)
    assert scalar.covers(ReleaseNumber.scalar("2"))
    assert compare_release_numbers(ReleaseNumber.scalar("1.01"), scalar) == -1
    assert compare_release_numbers(scalar, ReleaseNumber.scalar("2")) == 0
    assert compare_release_numbers(wide_range, narrow_range) is None
    assert compare_release_numbers(
        ReleaseNumber.scalar("2", part=1), ReleaseNumber.scalar("2", part=2)
    ) is None


def test_range_expansion_is_integral_and_bounded() -> None:
    number = ReleaseNumber.range("1", "3")

    assert expand_release_number(number, max_expansion=3) == tuple(
        Decimal(value) for value in (1, 2, 3)
    )
    assert ReleaseNumber.explicit_set(("1", "3")).expand() == (
        Decimal("1"),
        Decimal("3"),
    )
    with pytest.raises(ValueError, match="max_expansion"):
        number.expand(max_expansion=2)
    with pytest.raises(ValueError, match="fractional ranges"):
        ReleaseNumber.range("1.1", "2.1").expand()
    with pytest.raises(ValueError, match="positive"):
        number.expand(max_expansion=0)


def test_serialization_rejects_values_that_disagree_with_source_tokens() -> None:
    serialized = ReleaseNumber.scalar("1.00").to_dict()
    serialized["values"] = ["1.0"]

    with pytest.raises(ValueError, match="do not match"):
        ReleaseNumber.from_dict(serialized)
    serialized["values"] = [1.0]
    with pytest.raises(TypeError, match="decimal strings"):
        ReleaseNumber.from_dict(serialized)


def test_snapshot_keeps_explicit_observations_and_uses_identity_equality() -> None:
    path = Path("not-scanned/Series v01.cbz")
    observation = FileObservation(
        path=path,
        siblings=(Path("not-scanned/Series v02.cbz"),),
        metadata=(("Identifier", "9780000000000"), ("Identifier", "catalogue-7")),
        root_role="library",
        archive_pages=(
            ArchivePageObservation("images/01.png", is_grayscale=False),
            ArchivePageObservation("images/02.png", is_grayscale=None),
        ),
    )
    first = ReleaseSnapshot(
        observation=observation,
        number=ReleaseNumber.scalar("1"),
        kind="volume",
        display_series="Series",
        canonical_series_key="series",
        provenance=("filename", "comicinfo"),
        year=2022,
        release_group="Group",
        publisher="Publisher",
        is_premium=True,
        extras=("(Digital)",),
        subtitle="Side Story",
        identifiers=(("ISBN", "9780000000000"),),
    )
    second = ReleaseSnapshot(
        observation=observation,
        number=ReleaseNumber.scalar("1"),
        kind="volume",
        display_series="Series",
        canonical_series_key="series",
    )

    assert first is not second
    assert first != second
    assert len({first, second}) == 2
    assert first.path == path
    assert first.name == "Series v01.cbz"
    assert first.stem == "Series v01"
    assert first.parent == Path("not-scanned")
    assert observation.root_role == "library"
    assert [page.is_grayscale for page in observation.archive_pages] == [False, None]
    assert observation.metadata[0][0] == observation.metadata[1][0] == "Identifier"
    with pytest.raises(FrozenInstanceError):
        setattr(first, "display_series", "changed")


def test_missing_number_factory_preserves_independent_part() -> None:
    number = missing_release_number(part=12)

    assert number.kind == "missing"
    assert number.values == ()
    assert number.raw_tokens == ()
    assert number.part == 12

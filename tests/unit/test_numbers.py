from decimal import Decimal

import pytest

from komga_cover_extractor.domain.releases import ReleaseNumber
from komga_cover_extractor.parsing.numbers import (
    NumberPolicy,
    detect_missing_releases,
    parse_release_number,
)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("DAR LN01.cbz", "volume"),
        ("DAR Light Novels 01.cbz", "volume"),
        ("DAR Novel 01.cbz", "volume"),
        ("DAR Books 01.cbz", "volume"),
        ("DAR Volume 01.cbz", "volume"),
        ("DAR Vols 01.cbz", "volume"),
        ("DAR Discs 01.cbz", "volume"),
        ("DAR Tomo 01.cbz", "volume"),
        ("DAR Tome 01.cbz", "volume"),
        ("DAR Von 01.cbz", "volume"),
        ("DAR V 01.cbz", "volume"),
        ("DAR 第01.cbz", "volume"),
        ("DAR T01.cbz", "volume"),
        ("DAR Chapter 001.cbz", "chapter"),
        ("DAR Chap 001.cbz", "chapter"),
        ("DAR Chs 001.cbz", "chapter"),
        ("DAR Cs 001.cbz", "chapter"),
        ("DAR D001.cbz", "chapter"),
    ],
)
def test_legacy_keyword_variants_preserve_ordered_match(name: str, kind: str) -> None:
    number = parse_release_number(name, kind, NumberPolicy())

    assert number.kind == "scalar"
    assert number.values == (Decimal("1" if kind == "volume" else "1"),)


def test_zero_is_known_and_missing_remains_distinct() -> None:
    policy = NumberPolicy()

    zero = parse_release_number("DAR c000.cbz", "chapter", policy)
    missing = parse_release_number("DAR chapter missing.cbz", "chapter", policy)
    empty = parse_release_number("", "chapter", policy)

    assert zero.kind == "scalar"
    assert zero.values == (Decimal("0"),)
    assert zero.raw_tokens == ("000",)
    assert missing == ReleaseNumber.missing()
    assert empty == ReleaseNumber.missing()


def test_decimal_source_tokens_preserve_precision_and_trailing_zeroes() -> None:
    policy = NumberPolicy()

    first = parse_release_number("Series v01.01.cbz", "volume", policy)
    second = parse_release_number("Series v01.10.cbz", "volume", policy)
    chapter = parse_release_number("Series c179.1.cbz", "chapter", policy)
    trailing_zero = parse_release_number("Series v1.0.cbz", "volume", policy)

    assert first.raw_tokens == ("01.01",)
    assert first.values[0].as_tuple() == Decimal("01.01").as_tuple()
    assert second.raw_tokens == ("01.10",)
    assert second.values[0].as_tuple() == Decimal("01.10").as_tuple()
    assert chapter.raw_tokens == ("179.1",)
    assert trailing_zero.raw_tokens == ("1.0",)
    assert trailing_zero.values[0].as_tuple() == Decimal("1.0").as_tuple()


def test_two_hyphen_tokens_are_ranges_and_longer_sequences_are_explicit_sets() -> None:
    policy = NumberPolicy()

    contiguous = parse_release_number("Series v01-03.cbz", "volume", policy)
    explicit = parse_release_number("Series v01-03-07.cbz", "volume", policy)

    assert contiguous.kind == "range"
    assert contiguous.raw_tokens == ("01", "03")
    assert contiguous.values == (Decimal("01"), Decimal("03"))
    assert explicit.kind == "set"
    assert explicit.raw_tokens == ("01", "03", "07")
    assert explicit.values == (Decimal("01"), Decimal("03"), Decimal("07"))
    assert not explicit.contains(Decimal("2"))


def test_volume_and_chapter_parts_are_separate_integers() -> None:
    policy = NumberPolicy()

    volume = parse_release_number("Series v01 Part 12.cbz", "volume", policy)
    chapter = parse_release_number("Series c001x10.cbz", "chapter", policy)

    assert volume.values == (Decimal("01"),)
    assert volume.part == 12
    assert chapter.values == (Decimal("001"),)
    assert chapter.part == 10


@pytest.mark.parametrize(("suffix", "part"), [("x0", 0), ("x1.5", Decimal("1.5")), ("#1.5", Decimal("1.5"))])
def test_originally_accepted_chapter_parts_remain_separate_from_chapter_number(
    suffix: str, part: int | Decimal,
) -> None:
    number = parse_release_number(f"Series c053{suffix}.cbz", "chapter", NumberPolicy())
    assert number.values == (Decimal("53"),) and number.part == part
    assert number.covers(ReleaseNumber.scalar("53", part=part))
    assert not number.covers(ReleaseNumber.scalar("53"))
    assert not number.covers(ReleaseNumber.scalar("53", part=12))


def test_extra_marker_and_digit_underscores_are_not_float_loss() -> None:
    policy = NumberPolicy()

    extra = parse_release_number("Series v01_extra.cbz", "volume", policy)
    decimal = parse_release_number("Series_v01_10.cbz", "volume", policy)
    ordinary = parse_release_number("Series_Title v01.cbz", "volume", policy)

    assert extra.values == (Decimal("01.5"),)
    assert extra.raw_tokens == ("01.5",)
    assert decimal.values == (Decimal("01.10"),)
    assert decimal.raw_tokens == ("01.10",)
    assert ordinary.values == (Decimal("01"),)


def test_legacy_single_volume_year_cutoff_does_not_reject_ranges_or_chapters() -> None:
    policy = NumberPolicy()

    assert parse_release_number("Series v2022.cbz", "volume", policy).kind == "missing"
    assert parse_release_number("Series v1999.cbz", "volume", policy).values == (Decimal("1999"),)
    assert parse_release_number("Series v2022-2023.cbz", "volume", policy).kind == "range"
    assert parse_release_number("Series c2022.cbz", "chapter", policy).values == (
        Decimal("2022"),
    )


def test_episode_edition_and_subtitle_digits_do_not_replace_release_number() -> None:
    policy = NumberPolicy()
    volume = parse_release_number(
        "Series v01 - Episode 05 (2022) (2-in-1 Edition).cbz",
        "volume",
        policy,
    )
    chapter = parse_release_number(
        "Series c001 - Side Story 02 (2022).cbz",
        "chapter",
        policy,
    )
    episode_only = parse_release_number(
        "Series Episode 5.cbz", "chapter", policy
    )

    assert volume.raw_tokens == ("01",)
    assert chapter.raw_tokens == ("001",)
    assert episode_only.kind == "missing"


def test_empty_keyword_lists_compile_and_custom_policy_isolated() -> None:
    empty = NumberPolicy((), (), (), ())
    custom = NumberPolicy(("Issue",), (), (), ())

    assert parse_release_number("Series v01.cbz", "volume", empty).kind == "missing"
    assert parse_release_number("Series Issue 3.cbz", "volume", custom).values == (
        Decimal("3"),
    )
    assert parse_release_number("Series Issue 3.cbz", "volume", NumberPolicy()).kind == "missing"


def test_missing_release_report_preserves_ranges_sets_parts_and_decimal_values() -> None:
    report = detect_missing_releases(
        (
            ReleaseNumber.explicit_set(("1", "3")),
            ReleaseNumber.scalar("3", part=12),
            ReleaseNumber.scalar("2.5"),
            ReleaseNumber.missing(),
        )
    )

    assert report.status == "complete"
    assert report.observed == (1, 3)
    assert report.missing == (2,)
    assert report.reason is None


def test_missing_release_ranges_are_bounded_and_integral_only() -> None:
    complete = detect_missing_releases((ReleaseNumber.range("1", "3"),), max_expansion=3)
    fractional = detect_missing_releases((ReleaseNumber.range("1.1", "3.1"),))
    limited = detect_missing_releases((ReleaseNumber.range("1", "4"),), max_expansion=3)

    assert complete.missing == ()
    assert fractional.missing == ()
    assert limited.status == "review_required"
    assert limited.reason == "range_limit"
    assert limited.missing == ()


def test_missing_release_report_rejects_invalid_limits_and_input_types() -> None:
    with pytest.raises(ValueError, match="positive"):
        detect_missing_releases((), max_expansion=0)
    with pytest.raises(TypeError, match="ReleaseNumber"):
        detect_missing_releases(("1",))  # type: ignore[arg-type]


def test_incomplete_numeric_candidates_do_not_become_known_prefixes() -> None:
    malformed = parse_release_number("Series v1.2.3.cbz", "volume", NumberPolicy())
    precise = parse_release_number("Series v1.20.cbz", "volume", NumberPolicy())

    assert malformed.kind == "missing"
    assert precise.values == (Decimal("1.20"),)
    assert precise.raw_tokens == ("1.20",)


def test_ordered_keyword_cleanup_rejects_leftover_keyword_text() -> None:
    short_first = NumberPolicy(
        volume_keywords=("V", "Volumes?"),
        chapter_keywords=("C", "Chapters?"),
    )
    long_first = NumberPolicy(
        volume_keywords=("Volumes?", "V"),
        chapter_keywords=("Chapters?", "C"),
    )

    assert parse_release_number("Series Volumes 2.cbz", "volume", short_first).kind == "missing"
    assert parse_release_number("Series Chapters 3.cbz", "chapter", short_first).kind == "missing"
    assert parse_release_number("Series Volumes 2.cbz", "volume", long_first).values == (
        Decimal("2"),
    )
    assert parse_release_number("Series Chapters 3.cbz", "chapter", long_first).values == (
        Decimal("3"),
    )


def test_descending_collection_covers_intermediate_volumes_but_explicit_set_does_not() -> None:
    policy = NumberPolicy()
    interval = parse_release_number("Series v3-1.cbz", "volume", policy)
    fourth = parse_release_number("Series v4.cbz", "volume", policy)
    explicit = parse_release_number("Series v3-1-4.cbz", "volume", policy)

    assert interval.kind == "range"
    assert interval.raw_tokens == ("1", "3")
    assert interval.covers(ReleaseNumber.scalar("2"))
    complete = detect_missing_releases((interval, fourth))
    assert complete.status == "complete"
    assert complete.observed == (1, 2, 3, 4)
    assert complete.missing == ()

    assert explicit.kind == "set"
    assert explicit.raw_tokens == ("3", "1", "4")
    assert not explicit.covers(ReleaseNumber.scalar("2"))
    incomplete = detect_missing_releases((explicit,))
    assert incomplete.status == "complete"
    assert incomplete.observed == (1, 3, 4)
    assert incomplete.missing == (2,)


def test_descending_decimal_interval_retains_each_endpoint_precision() -> None:
    interval = parse_release_number("Series v03.10-01.01.cbz", "volume", NumberPolicy())

    assert interval.kind == "range"
    assert interval.raw_tokens == ("01.01", "03.10")
    assert interval.values[0].as_tuple() == Decimal("01.01").as_tuple()
    assert interval.values[1].as_tuple() == Decimal("03.10").as_tuple()


@pytest.mark.parametrize(
    ("name", "kind", "tokens"),
    [
        ("Series v3-1_extra Part 12.cbz", "volume", ("1.5", "3")),
        ("Series v1-3_extra Part 12.cbz", "volume", ("1", "3.5")),
        ("Series v03.10-1_extra Part 12.cbz", "volume", ("1.5", "03.10")),
        ("Series v01.01-3_extra Part 12.cbz", "volume", ("01.01", "3.5")),
        ("Series c3-1_extrax12.cbz", "chapter", ("1.5", "3")),
        ("Series c1-3_extra#12.cbz", "chapter", ("1", "3.5")),
    ],
)
def test_extra_range_ordering_retains_endpoint_values_and_part_coverage(
    name: str, kind: str, tokens: tuple[str, str]
) -> None:
    number = parse_release_number(name, kind, NumberPolicy())

    assert number.kind == "range"
    assert number.raw_tokens == tokens
    assert number.values == (Decimal(tokens[0]), Decimal(tokens[1]))
    assert number.part == 12
    assert number.covers(ReleaseNumber.scalar("2", part=12))
    assert not number.covers(ReleaseNumber.scalar("2", part=10))
    assert not number.contains("0.9")
    assert not number.contains("3.6")

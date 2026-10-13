from decimal import Decimal
from pathlib import Path

import pytest

from komga_cover_extractor.domain.releases import (
    ArchivePageObservation,
    FileObservation,
    ReleaseSnapshot,
)
from komga_cover_extractor.parsing.filenames import (
    ParsingPolicy,
    RenamePolicy,
    parse_release,
    render_release,
)
from komga_cover_extractor.parsing.numbers import NumberPolicy, parse_release_number


def observed(
    name: str,
    *,
    metadata: tuple[tuple[str, str], ...] = (),
    siblings: tuple[Path, ...] = (Path("Library/Series/Other v02.cbz"),),
    root_role: str = "library",
    at_configured_root: bool = False,
    archive_pages: tuple[ArchivePageObservation, ...] = (),
) -> FileObservation:
    return FileObservation(
        Path("Library/Series") / name,
        metadata=metadata,
        siblings=siblings,
        root_role=root_role,
        at_configured_root=at_configured_root,
        archive_pages=archive_pages,
    )


def test_subtitle_publisher_marker_is_literal_observed_text() -> None:
    release = parse_release(
        observed(
            "Series v01 - Side Story [Orbit+ Press].epub",
            metadata=(("Publisher", "Orbit+ Press"),),
        ),
        ParsingPolicy(),
    )

    assert release.publisher == "Orbit+ Press"
    assert release.subtitle == "Side Story"
    assert render_release(release, RenamePolicy(restructure_when_renaming=True)) == (
        "Series v01 - Side Story [Orbit+ Press].epub"
    )


def test_name_publisher_marker_remains_available_when_metadata_wins() -> None:
    release = parse_release(
        observed(
            "Series v01 - Side Story [Alias Press].epub",
            metadata=(("Publisher", "Primary House"),),
        ),
        ParsingPolicy(publishers=("Alias Press",)),
    )

    assert release.publisher == "Primary House"
    assert release.subtitle == "Side Story"
    assert render_release(
        release,
        RenamePolicy(
            restructure_when_renaming=True,
            add_publisher_name_to_file_name_when_renaming=True,
            publishers=("Alias Press",),
        ),
    ) == "Series v01 - Side Story [Primary House].epub"


def test_restructure_removes_other_known_publisher_extras_in_dictionary_order() -> None:
    release = parse_release(
        observed(
            "Series v01 [Alias Press] [Second Press] [Digital].epub",
            metadata=(("Publisher", "Primary House"),),
        ),
        ParsingPolicy(publishers=("Alias Press", "Second Press")),
    )

    assert render_release(
        release,
        RenamePolicy(
            restructure_when_renaming=True,
            add_publisher_name_to_file_name_when_renaming=True,
            publishers=("Alias Press", "Second Press"),
        ),
    ) == "Series v01 [Primary House] [Digital].epub"


@pytest.mark.parametrize(("literal", "near_match"), [
    ("Orbit. Press", "Orbitx Press"),
    ("Orbit+ Press", "Orbitt Press"),
])
def test_publisher_rendering_preserves_near_matches_and_removes_exact_literals(literal: str, near_match: str) -> None:
    release = parse_release(
        observed(
            f"Series v01 [{near_match}] [{literal}] [Digital].epub",
            metadata=(("Publisher", "Primary House"),),
        ),
        ParsingPolicy(),
    )
    assert render_release(
        release,
        RenamePolicy(
            restructure_when_renaming=True,
            add_publisher_name_to_file_name_when_renaming=True,
            publishers=(literal,),
            publisher_similarity_score=1.0,
        ),
    ) == f"Series v01 [Primary House] [{near_match}] [Digital].epub"


def test_subtitle_number_guard_compares_the_whole_release_sequence() -> None:
    collection = parse_release(
        observed("Series v01-03 - Volume 01 (2022).cbz"), ParsingPolicy()
    )
    scalar = parse_release(
        observed("Series v01 - Volume 01 (2022).cbz"), ParsingPolicy()
    )

    assert collection.number.raw_tokens == ("01", "03")
    assert collection.subtitle == "Volume 01"
    assert scalar.subtitle is None


def test_metadata_repetitions_are_retained_and_last_values_drive_legacy_fields() -> None:
    release = parse_release(
        observed(
            "Series v01.epub",
            metadata=(
                ("dc:description", "A description"),
                ("dc:date", "1949-01-01"),
                ("dc:date", "2005-02-03"),
                ("dc:publisher", "First House"),
                ("dc:publisher", "Last House, LLC extra"),
                ("dc:identifier", "urn:isbn:9780000000001"),
                ("DC:IDENTIFIER", "urn:uuid:second"),
                ("ISBN", "9780000000002"),
            ),
        ),
        ParsingPolicy(),
    )

    assert release.year == 2005
    assert release.publisher == "Last House"
    assert release.identifiers == (
        ("dc:identifier", "urn:isbn:9780000000001"),
        ("DC:IDENTIFIER", "urn:uuid:second"),
        ("ISBN", "9780000000002"),
    )
    assert release.observation.metadata[1:3] == (
        ("dc:date", "1949-01-01"), ("dc:date", "2005-02-03")
    )


@pytest.mark.parametrize(
    ("metadata", "year"),
    [
        ((("dc:description", "Description"), ("dc:date", "1950-01-01")), 1950),
        ((("dc:description", "Description"), ("dc:date", "1949-01-01")), None),
        ((("dc:date", "2000-01-01"),), None),
        ((("Summary", "Description"), ("Year", "not a year")), None),
    ],
)
def test_metadata_year_gates_do_not_invent_a_year(
    metadata: tuple[tuple[str, str], ...], year: int | None
) -> None:
    release = parse_release(observed("Series v01.epub", metadata=metadata), ParsingPolicy())

    assert release.year == year
    assert release.number.values == (Decimal("1"),)


@pytest.mark.parametrize(
    ("name", "metadata", "premium"),
    [
        ("Series v01 [Premium].epub", (), True),
        ("Series v01 [Premium].cbz", (), False),
        ("Series v01.epub", (("OPS/toc.xhtml", "J-Novel Club Bonus Short Stories"),), True),
        ("Series v01.epub", (("OPS/copyright.xhtml", "Premium E-Book"),), True),
        ("Series v01.epub", (("premium", "true"),), True),
        ("Series v01.epub", (("premium", "false"),), False),
        ("Series v01.epub", (("OPS/toc.xhtml", "Other Press Bonus Illustrations"),), False),
    ],
)
def test_premium_detection_consumes_explicit_observations_only(
    name: str, metadata: tuple[tuple[str, str], ...], premium: bool
) -> None:
    release = parse_release(observed(name, metadata=metadata), ParsingPolicy())

    assert release.is_premium is premium
    assert release.display_series == "Series"


def test_premium_addition_retains_unrelated_extras_and_novel_brackets() -> None:
    release = parse_release(
        observed(
            "Series v1 (Digital) (Premium) [Collector].epub",
            metadata=(("premium", "true"),),
        ),
        ParsingPolicy(),
    )

    assert release.extras == ("[Premium]", "(Digital)", "[Collector]")
    assert render_release(
        release,
        RenamePolicy(restructure_when_renaming=True, search_and_add_premium_to_file_name=True),
    ) == "Series v01 [Premium] (Digital) [Collector].epub"


def test_release_group_suffix_precedes_dictionary_and_last_bracket_match_wins() -> None:
    policy = ParsingPolicy(release_groups=("First", "Second"))
    suffix = parse_release(observed("Series v01 (First)-Suffix.cbz"), policy)
    bracketed = parse_release(observed("Series v01 (First) (Second).cbz"), policy)

    assert suffix.release_group == "Suffix"
    assert bracketed.release_group == "Second"


@pytest.mark.parametrize(
    ("publisher_enabled", "rendered"),
    [
        (False, "Series v01 (Group) (Digital).cbz"),
        (True, "Series v01 (Digital) (Group).cbz"),
    ],
)
def test_release_group_movement_retains_the_effective_publisher_gate(
    publisher_enabled: bool, rendered: str
) -> None:
    release = parse_release(
        observed("Series v01 (Group) (Digital).cbz"),
        ParsingPolicy(release_groups=("Group",)),
    )

    assert render_release(
        release,
        RenamePolicy(
            restructure_when_renaming=True,
            move_release_group_to_end_of_file_name=True,
            add_publisher_name_to_file_name_when_renaming=publisher_enabled,
        ),
    ) == rendered


def test_volume_series_removes_metadata_when_no_volume_keyword_matches() -> None:
    release = parse_release(observed("Unnumbered Book (Digital).epub"), ParsingPolicy())

    assert release.number.kind == "missing"
    assert release.display_series == "Unnumbered Book"
    assert release.extras == ("(Digital)",)


def test_one_shot_label_cleaning_does_not_override_exception_classification() -> None:
    release = parse_release(
        observed("Unnumbered Book - One-shot (Digital).epub", siblings=()),
        ParsingPolicy(),
    )

    assert release.display_series == "Unnumbered Book"
    assert release.number.kind == "missing"
    assert "classification:one_shot" not in release.provenance
    assert "classification:exception_keyword" in release.provenance


def test_chapter_series_cleans_one_shot_label_before_the_chapter_keyword() -> None:
    release = parse_release(
        observed("Series - One-shot c001 (Digital).cbz"), ParsingPolicy()
    )

    assert release.kind == "chapter"
    assert release.display_series == "Series"
    assert release.number.values == (Decimal("1"),)


@pytest.mark.parametrize("role", ["library", "download", "both", "unknown"])
def test_folder_fallback_uses_root_position_not_role(role: str) -> None:
    nested = parse_release(observed("Volume 1.cbz", root_role=role), ParsingPolicy())
    top_level = parse_release(
        observed("Volume 1.cbz", root_role=role, at_configured_root=True),
        ParsingPolicy(),
    )

    assert nested.display_series == "Series"
    assert top_level.display_series == ""


@pytest.mark.parametrize("role", ["library", "download", "both", "unknown"])
def test_direct_root_one_shot_bypass_applies_only_to_download_role(role: str) -> None:
    release = parse_release(
        observed("Unnumbered Book.epub", root_role=role, at_configured_root=True),
        ParsingPolicy(),
    )

    assert release.number.kind == ("scalar" if role in {"download", "both"} else "missing")
    assert ("classification:one_shot" in release.provenance) is (role in {"download", "both"})


def test_custom_exception_keywords_are_policy_local_and_empty_is_allowed() -> None:
    source = observed("Unnumbered Book (Bonus).epub", siblings=())
    exceptional = parse_release(source, ParsingPolicy(exception_keywords=("Bonus",)))
    ordinary = parse_release(source, ParsingPolicy(exception_keywords=()))

    assert exceptional.number.kind == "missing"
    assert "classification:exception_keyword" in exceptional.provenance
    assert ordinary.number.values == (Decimal("1"),)
    assert "classification:one_shot" in ordinary.provenance


def test_configured_exception_regexes_remain_patterns_instead_of_dictionary_literals() -> None:
    release = parse_release(
        observed("Unnumbered Book (Bonus content).epub", siblings=()),
        ParsingPolicy(exception_keywords=(r"Bon(us|us content)",)),
    )
    assert release.number.kind == "missing"
    assert release.display_series == "Unnumbered Book"
    assert "classification:exception_keyword" in release.provenance
    assert "classification:one_shot" not in release.provenance


@pytest.mark.parametrize("first", [
    ArchivePageObservation("ComicInfo.xml"),
    ArchivePageObservation("000.txt"),
    ArchivePageObservation("000/", is_directory=True),
])
def test_grayscale_heuristic_does_not_skip_an_observed_first_nonimage_member(first: ArchivePageObservation) -> None:
    pages = tuple(ArchivePageObservation(f"pages/{index:03}.png", True) for index in range(86))
    release = parse_release(
        observed(
            "Unnumbered Book.cbz",
            siblings=(),
            archive_pages=(first, *pages),
        ),
        ParsingPolicy(),
    )
    image_first = parse_release(
        observed("Unnumbered Book.cbz", siblings=(), archive_pages=pages),
        ParsingPolicy(),
    )

    assert release.kind == "volume"
    assert image_first.kind == "chapter"


@pytest.mark.parametrize(("page_count", "kind"), [(85, "chapter"), (86, "volume")])
def test_color_page_count_uses_the_configured_inclusive_threshold(page_count: int, kind: str) -> None:
    release = parse_release(
        observed(
            "Unnumbered Book.cbz",
            siblings=(),
            archive_pages=tuple(
                ArchivePageObservation(f"{index:03}.png", False) for index in range(page_count)
            ),
        ),
        ParsingPolicy(),
    )

    assert release.kind == kind
    assert release.number.values == (Decimal("1"),)


def test_parts_are_not_taken_from_series_names_or_detected_subtitles() -> None:
    release = parse_release(
        observed("Part 12 Series v01 - Side Story Part 10 (Digital).cbz"),
        ParsingPolicy(),
    )
    numbered_part = parse_release(
        observed("Part 12 Series v01 Part 10 - Side Story (Digital).cbz"),
        ParsingPolicy(),
    )
    direct = parse_release_number("Part 12 Series v01 Part 10.cbz", "volume", NumberPolicy())

    assert release.display_series == "Part 12 Series"
    assert release.subtitle == "Side Story Part 10"
    assert release.number.part is None
    assert numbered_part.number.part == 10
    assert direct.values == (Decimal("1"),)
    assert direct.part == 10


@pytest.mark.parametrize(
    ("name", "rendered"),
    [
        ("Series 001_10 (Digital).cbz", "Series c001.10 (Digital).cbz"),
        ("Series 001x12 (Digital).cbz", "Series c001x12 (Digital).cbz"),
        ("Series 001#10 (Digital).cbz", "Series c001#10 (Digital).cbz"),
        ("Series - #404 - Subtitle (Digital).cbz", "Series - c404 - Subtitle (Digital).cbz"),
    ],
)
def test_no_keyword_chapter_render_replaces_the_complete_observed_fragment(
    name: str, rendered: str
) -> None:
    release = parse_release(observed(name), ParsingPolicy())

    assert release.kind == "chapter"
    assert render_release(
        release, RenamePolicy(rename_chapters_with_preferred_chapter_keyword=True)
    ) == rendered


@pytest.mark.parametrize(
    ("name", "rendered"),
    [
        ("Series v. 1 (Digital).cbz", "Series Volume 01 (Digital).cbz"),
        ("Series v001 (Digital).cbz", "Series Volume 01 (Digital).cbz"),
        ("Series v0010 (Digital).cbz", "Series Volume 10 (Digital).cbz"),
        ("Series v_1 (Digital).cbz", "Series Volume 01 (Digital).cbz"),
        ("Series v01-03-07 (Digital).cbz", "Series Volume 01-03-07 (Digital).cbz"),
        ("Series v01.10 (Digital).cbz", "Series Volume 01.10 (Digital).cbz"),
    ],
)
def test_preserve_mode_renders_keyword_variants_sets_and_raw_decimals(
    name: str, rendered: str
) -> None:
    release = parse_release(observed(name), ParsingPolicy())

    assert render_release(release, RenamePolicy(preferred_volume_renaming_format="Volume ")) == rendered


@pytest.mark.parametrize("restructure", [False, True])
def test_extra_marker_stays_on_the_release_number_and_preserves_other_fractions(restructure: bool) -> None:
    release = parse_release(observed("Series v01.5-03_extra (Digital).cbz"), ParsingPolicy())

    assert release.number.raw_tokens == ("01.5", "03.5")
    assert render_release(
        release,
        RenamePolicy(preferred_volume_renaming_format="Volume ", restructure_when_renaming=restructure),
    ) == "Series Volume 01.5-03_extra (Digital).cbz"


def test_extra_marker_in_the_title_does_not_truncate_an_ordinary_decimal_number() -> None:
    release = parse_release(observed("Series_extra v01.5 (Digital).cbz"), ParsingPolicy())

    assert release.number.values == (Decimal("1.5"),)
    assert render_release(release, RenamePolicy(preferred_volume_renaming_format="Volume ")) == (
        "Series_extra Volume 01.5 (Digital).cbz"
    )


@pytest.mark.parametrize("restructure", [False, True])
def test_issue_number_is_adjacent_to_release_and_existing_marker_is_not_duplicated(restructure: bool) -> None:
    release = parse_release(observed("Series v1 #1 (Digital).cbz"), ParsingPolicy())

    assert render_release(
        release,
        RenamePolicy(add_issue_number_to_manga_file_name=True, restructure_when_renaming=restructure),
    ) == "Series v01 #01 (Digital).cbz"


def test_preserve_mode_one_shot_addition_does_not_drop_year_or_extras() -> None:
    release = parse_release(
        observed("Unnumbered Book (2022) [Digital] [Collector].epub", siblings=()),
        ParsingPolicy(),
    )

    assert release.number.values == (Decimal("1"),)
    assert render_release(release, RenamePolicy(add_volume_one_number_to_one_shots=True)) == (
        "Unnumbered Book v01 (2022) [Digital] [Collector].epub"
    )


@pytest.mark.parametrize(("replace_unicode", "name"), [(False, "Café v01.cbz"), (True, "Cafe v01.cbz")])
def test_restructure_unicode_flag_transliterates_instead_of_dropping_letters(
    replace_unicode: bool, name: str
) -> None:
    release = parse_release(observed("Café v01.cbz"), ParsingPolicy())

    assert render_release(
        release,
        RenamePolicy(restructure_when_renaming=True, replace_unicode_when_restructuring=replace_unicode),
    ) == name


@pytest.mark.parametrize("keyword", ["../Volume ", "Volume/", "Volume\\", "Volume\0"])
def test_public_renderer_rejects_filename_escape_from_policy(keyword: str) -> None:
    release = parse_release(observed("Series v1.cbz"), ParsingPolicy())

    with pytest.raises(ValueError, match="relative basename"):
        render_release(release, RenamePolicy(preferred_volume_renaming_format=keyword))


@pytest.mark.parametrize("name", ["..", "C:book.epub", "book\\elsewhere.epub"])
def test_public_renderer_validates_unchanged_filenames_too(name: str) -> None:
    release = ReleaseSnapshot(FileObservation(Path(name)), kind="unknown")

    with pytest.raises(ValueError, match="relative basename"):
        render_release(release, RenamePolicy())


def test_renderer_rejects_backslashes_from_metadata_without_mutating_observation() -> None:
    release = parse_release(
        observed("Series v01.epub", metadata=(("Publisher", "Press\\Elsewhere"),)),
        ParsingPolicy(),
    )

    with pytest.raises(ValueError, match="relative basename"):
        render_release(
            release,
            RenamePolicy(restructure_when_renaming=True, add_publisher_name_to_file_name_when_renaming=True),
        )
    assert release.observation.metadata == (("Publisher", "Press\\Elsewhere"),)


@pytest.mark.parametrize(
    ("folder", "series"),
    [
        ("My_Series", "My Series"),
        ("My Series - Complete", "My Series"),
        ("My Series Volume 1", "My Series"),
    ],
)
def test_volume_folder_fallback_applies_the_complete_series_cleanup(folder: str, series: str) -> None:
    release = parse_release(
        FileObservation(Path(folder) / "Volume 1.cbz", root_role="download"),
        ParsingPolicy(),
    )

    assert release.display_series == series
    assert release.number.values == (Decimal("1"),)


def test_chapter_folder_fallback_does_not_use_the_child_number_to_erase_title_digits() -> None:
    release = parse_release(
        FileObservation(Path("Series 1") / "c001.cbz", root_role="download"),
        ParsingPolicy(),
    )

    assert release.kind == "chapter"
    assert release.display_series == "Series 1"
    assert release.number.raw_tokens == ("001",)


def test_explicit_chapter_keyword_does_not_erase_a_number_in_the_series_title() -> None:
    release = parse_release(observed("Series 001 c001.cbz"), ParsingPolicy())

    assert release.display_series == "Series 001"
    assert release.number.values == (Decimal("1"),)


def test_chapter_folder_cleanup_removes_a_leading_number_before_the_title() -> None:
    release = parse_release(
        FileObservation(Path("006 - Unnumbered Book") / "c001.cbz", root_role="download"),
        ParsingPolicy(),
    )

    assert release.kind == "chapter"
    assert release.number.values == (Decimal("1"),)
    assert release.display_series == "Unnumbered Book"


@pytest.mark.parametrize(
    ("name", "part", "rendered"),
    [
        ("Series c1x12 (Digital).cbz", 12, "Series c001x12 (Digital).cbz"),
        ("Series c1#10 (Digital).cbz", 10, "Series c001#10 (Digital).cbz"),
    ],
)
def test_keyword_chapter_parts_are_separate_and_rendered_once(name: str, part: int, rendered: str) -> None:
    release = parse_release(observed(name), ParsingPolicy())
    number = parse_release_number(name, "chapter", NumberPolicy())

    assert release.number.values == (Decimal("1"),)
    assert release.number.part == part
    assert number.part == part
    assert render_release(
        release, RenamePolicy(rename_chapters_with_preferred_chapter_keyword=True)
    ) == rendered


@pytest.mark.parametrize("restructure", [False, True])
def test_known_chapter_zero_remains_known_in_both_render_modes(restructure: bool) -> None:
    release = parse_release(observed("Series c000 (Digital).cbz"), ParsingPolicy())

    assert release.kind == "chapter"
    assert release.number.values == (Decimal("0"),)
    assert render_release(
        release,
        RenamePolicy(
            restructure_when_renaming=restructure,
            rename_chapters_with_preferred_chapter_keyword=True,
        ),
    ) == "Series c000 (Digital).cbz"


def test_part_marker_comes_from_the_release_fragment_not_a_number_in_the_title() -> None:
    release = parse_release(observed("9#12 Series c1x10 (Digital).cbz"), ParsingPolicy())

    assert release.display_series == "9#12 Series"
    assert release.number.part == 10
    assert render_release(
        release, RenamePolicy(rename_chapters_with_preferred_chapter_keyword=True)
    ) == "9#12 Series c001x10 (Digital).cbz"


def test_renderer_does_not_replace_an_ignored_bracketed_chapter_number() -> None:
    release = parse_release(observed("[c999x12] Series c1x10 (Digital).cbz"), ParsingPolicy())

    assert release.number.values == (Decimal("1"),)
    assert release.number.part == 10
    assert render_release(
        release, RenamePolicy(rename_chapters_with_preferred_chapter_keyword=True)
    ) == "[c999x12] Series c001x10 (Digital).cbz"


def test_renderer_replaces_only_the_parsed_volume_not_a_bracketed_number() -> None:
    release = parse_release(observed("Series [v09] v1 (Digital).cbz"), ParsingPolicy())

    assert release.number.values == (Decimal("1"),)
    assert render_release(release, RenamePolicy(preferred_volume_renaming_format="Volume ")) == (
        "Series [v09] Volume 01 (Digital).cbz"
    )


@pytest.mark.parametrize("restructure", [False, True])
@pytest.mark.parametrize(
    ("name", "rendered"),
    [
        ("Series v01.5-03_extra (Digital).cbz", "Series v01.5-03_extra (Digital).cbz"),
        ("Series v001-003-007 (Digital).cbz", "Series v01-03-07 (Digital).cbz"),
        ("Series 001_10x12 (Digital).cbz", "Series c001.10x12 (Digital).cbz"),
    ],
)
def test_render_parse_render_keeps_parts_sets_raw_decimals_and_extra_markers(
    restructure: bool, name: str, rendered: str
) -> None:
    policy = ParsingPolicy()
    renderer = RenamePolicy(
        restructure_when_renaming=restructure,
        rename_chapters_with_preferred_chapter_keyword=True,
    )
    release = parse_release(observed(name), policy)
    first = render_release(release, renderer)
    reparsed = parse_release(observed(first), policy)

    assert first == rendered
    assert render_release(reparsed, renderer) == rendered


@pytest.mark.parametrize(("kind", "name"), [("volume", "Series v01 Part 1.5.cbz"), ("chapter", "Series c001x1.5.cbz")])
def test_fractional_parts_are_invalid_instead_of_truncated(kind: str, name: str) -> None:
    with pytest.raises(ValueError):
        parse_release_number(name, kind, NumberPolicy())

    assert parse_release_number("Series c001x12.cbz", "chapter", NumberPolicy()).part == 12


def test_missing_volume_number_with_extra_marker_leaves_the_filename_unchanged() -> None:
    release = parse_release(observed("Series v2022_extra.cbz"), ParsingPolicy())

    assert release.number.kind == "missing"
    assert render_release(release, RenamePolicy()) == "Series v2022_extra.cbz"


@pytest.mark.parametrize(
    ("replace_unicode", "rendered"),
    [(False, "Café 日 v01.cbz"), (True, "Cafe Ri v01.cbz")],
)
def test_transliteration_whitespace_and_display_unicode_are_stable_across_roundtrip(
    replace_unicode: bool, rendered: str
) -> None:
    policy = ParsingPolicy()
    renderer = RenamePolicy(
        restructure_when_renaming=True,
        replace_unicode_when_restructuring=replace_unicode,
    )
    release = parse_release(observed("Café 日 v1.cbz"), policy)
    first = render_release(release, renderer)
    reparsed = parse_release(observed(first), policy)

    assert release.display_series == "Café 日"
    assert first == rendered
    assert render_release(reparsed, renderer) == rendered


def test_volume_classification_preserves_the_broader_legacy_keyword_grammar() -> None:
    release = parse_release(observed("Series v-1 (Digital).cbz"), ParsingPolicy())

    assert release.kind == "volume"
    assert release.number.kind == "missing"
    assert release.display_series == "Series"
    assert render_release(release, RenamePolicy()) == "Series v-1 (Digital).cbz"

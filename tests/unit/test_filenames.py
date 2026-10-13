from decimal import Decimal
from pathlib import Path

import pytest

from komga_cover_extractor.domain.releases import (
    ArchivePageObservation,
    FileObservation,
)
from komga_cover_extractor.parsing.filenames import (
    ParsingPolicy,
    RenamePolicy,
    parse_release,
    render_release,
)
from komga_cover_extractor.parsing.normalization import clean_str
from komga_cover_extractor.parsing.numbers import NumberPolicy


def observation(
    name: str,
    *,
    siblings: tuple[Path, ...] = (),
    metadata: tuple[tuple[str, str], ...] = (),
    root_role: str = "unknown",
    archive_pages: tuple[ArchivePageObservation, ...] = (),
) -> FileObservation:
    return FileObservation(
        path=Path("Library") / name,
        siblings=siblings,
        metadata=metadata,
        root_role=root_role,
        archive_pages=archive_pages,
    )


def test_release_snapshot_preserves_display_and_canonical_series_separately() -> None:
    release = parse_release(
        observation(
            "Series Name v01 (2022) (Digital) (Group).cbz",
            siblings=(Path("Library/Series Name v02.cbz"),),
            root_role="library",
        ),
        ParsingPolicy(release_groups=("Group",)),
    )

    assert release.kind == "volume"
    assert release.display_series == "Series Name"
    assert release.canonical_series_key == clean_str("Series Name")
    assert release.year == 2022
    assert release.release_group == "Group"
    assert release.extras == ("(Digital)", "(Group)")


def test_filename_subtitle_and_metadata_helpers_keep_legacy_gates() -> None:
    release = parse_release(
        observation(
            "The Title v01 - Side Story (2022) (Digital).cbz",
            metadata=(
                ("Summary", "A description"),
                ("Year", "1949"),
                ("Publisher", "Example Press, LLC details"),
            ),
            root_role="library",
        ),
        ParsingPolicy(),
    )

    assert release.display_series == "The Title"
    assert release.year == 2022  # filename year takes precedence over metadata
    assert release.publisher == "Example Press"
    assert release.subtitle == "Side Story"
    assert release.extras == ("(Digital)",)


def test_metadata_year_requires_the_legacy_description_and_minimum_year() -> None:
    gated_out = parse_release(
        observation(
            "Series v01.cbz",
            metadata=(("Year", "2001"),),
            root_role="library",
        ),
        ParsingPolicy(),
    )
    accepted = parse_release(
        observation(
            "Series v01.cbz",
            metadata=(("Summary", "Description"), ("Year", "1950")),
            root_role="library",
        ),
        ParsingPolicy(),
    )

    assert gated_out.year is None
    assert accepted.year == 1950


def test_extras_are_unique_premium_first_and_extension_aware() -> None:
    manga = parse_release(
        observation(
            "Series v01 (Digital) (Digital) (Publisher) (Premium).cbz",
            root_role="library",
        ),
        ParsingPolicy(publishers=("Publisher",)),
    )
    novel = parse_release(
        observation(
            "Series v01 (Digital) (J-Novel Club Premium).epub",
            root_role="library",
            metadata=(("Publisher", "Publisher"),),
        ),
        ParsingPolicy(publishers=("Publisher",)),
    )

    assert manga.extras == ("(Premium)", "(Digital)", "(Publisher)")
    assert novel.extras == ("[Premium]", "(Digital)")


def test_one_shot_requires_explicit_root_or_sibling_observations() -> None:
    download = parse_release(
        FileObservation(
            path=Path("Downloads/One Book.cbz"),
            siblings=(Path("Downloads/One Book.cbz"), Path("Downloads/Other.cbz")),
            root_role="download",
            at_configured_root=True,
        ),
        ParsingPolicy(),
    )
    contained_download = parse_release(
        FileObservation(
            path=Path("Downloads/Series/One Book.cbz"),
            siblings=(
                Path("Downloads/Series/One Book.cbz"),
                Path("Downloads/Series/Other.cbz"),
            ),
            root_role="download",
        ),
        ParsingPolicy(),
    )
    test_mode_root = parse_release(
        FileObservation(
            path=Path("Tests/One Book.cbz"),
            siblings=(Path("Tests/One Book.cbz"),),
            root_role="unknown",
            at_configured_root=True,
        ),
        ParsingPolicy(),
    )
    multi_file_library = parse_release(
        FileObservation(
            path=Path("Library/One Book.cbz"),
            siblings=(Path("Library/Other.cbz"),),
            root_role="library",
        ),
        ParsingPolicy(),
    )

    assert download.number.values == (Decimal("1"),)
    assert "classification:one_shot" in download.provenance
    assert multi_file_library.number.kind == "missing"
    assert "classification:one_shot" not in multi_file_library.provenance
    assert contained_download.number.kind == "missing"
    assert "classification:one_shot" not in contained_download.provenance
    assert "classification:one_shot" in test_mode_root.provenance


@pytest.mark.parametrize("suffix", [".png", ".PNG"])
def test_observed_archive_pages_drive_the_85_page_and_grayscale_heuristic(suffix: str) -> None:
    grayscale_first = parse_release(
        FileObservation(
            path=Path("Library/One Book.cbz"),
            root_role="library",
            archive_pages=(
                ArchivePageObservation("pages/002.png", False),
                ArchivePageObservation(f"pages/001{suffix}", True),
                *(
                    ArchivePageObservation(f"pages/{index:03}.png", False)
                    for index in range(3, 87)
                ),
            ),
        ),
        ParsingPolicy(),
    )
    large_color_archive = parse_release(
        FileObservation(
            path=Path("Library/One Book.cbz"),
            root_role="library",
            archive_pages=tuple(
                ArchivePageObservation(f"pages/{index:03}.png", False)
                for index in range(86)
            ),
        ),
        ParsingPolicy(),
    )

    assert grayscale_first.kind == "chapter"
    assert grayscale_first.number.values == (Decimal("1"),)
    assert large_color_archive.kind == "volume"


def test_volume_folder_fallback_obeys_explicit_root_role() -> None:
    library = parse_release(
        FileObservation(
            path=Path("My Series/Volume 1.cbz"),
            siblings=(Path("My Series/Volume 1.cbz"),),
            root_role="library",
        ),
        ParsingPolicy(),
    )
    download = parse_release(
        FileObservation(
            path=Path("My Series/Volume 1.cbz"),
            siblings=(Path("My Series/Volume 1.cbz"),),
            root_role="download",
            at_configured_root=True,
        ),
        ParsingPolicy(),
    )

    assert library.display_series == "My Series"
    assert download.display_series == ""


def test_policy_dictionaries_are_per_snapshot_and_not_cached_by_filename() -> None:
    observed = observation(
        "Series v01 (Group).cbz",
        root_role="library",
    )
    first = parse_release(observed, ParsingPolicy(release_groups=("Group",)))
    second = parse_release(observed, ParsingPolicy(release_groups=("Other",)))

    assert first.release_group == "Group"
    assert second.release_group is None


def test_extra_marker_and_decimal_underscores_survive_rendering() -> None:
    policy = ParsingPolicy()
    renderer = RenamePolicy(preferred_volume_renaming_format="Volume ")
    extra = parse_release(
        observation("Series v01_extra.cbz", root_role="library"), policy
    )
    decimal = parse_release(
        observation("Series_v01_10.cbz", root_role="library"), policy
    )

    assert render_release(extra, renderer) == "Series Volume 01_extra.cbz"
    assert extra.number.values == (Decimal("01.5"),)
    assert decimal.number.raw_tokens == ("01.10",)
    assert render_release(decimal, renderer) == "Series Volume 01.10.cbz"


def test_preserve_mode_render_is_idempotent_and_keeps_metadata_order() -> None:
    parsing_policy = ParsingPolicy()
    rename_policy = RenamePolicy(preferred_volume_renaming_format="Volume ")
    original = observation(
        "Series v1 (2022) (Digital).cbz",
        root_role="library",
    )
    release = parse_release(original, parsing_policy)
    rendered = render_release(release, rename_policy)
    reparsed = parse_release(
        FileObservation(path=original.path.with_name(rendered), root_role="library"),
        parsing_policy,
    )

    assert rendered == "Series Volume 01 (2022) (Digital).cbz"
    assert render_release(reparsed, rename_policy) == rendered


def test_restructure_render_matches_characterized_order() -> None:
    parsing_policy = ParsingPolicy(release_groups=("Group",))
    rename_policy = RenamePolicy(
        restructure_when_renaming=True,
        add_publisher_name_to_file_name_when_renaming=True,
        search_and_add_premium_to_file_name=True,
        move_release_group_to_end_of_file_name=True,
    )
    release = parse_release(
        observation(
            "Series v1 - Epilogue (2022) (Digital) (Publisher) (Group).epub",
            root_role="library",
            metadata=(
                ("Publisher", "Publisher"),
                (
                    "toc.xhtml",
                    "J-Novel Club Bonus Illustrations",
                ),
            ),
        ),
        parsing_policy,
    )

    assert release.subtitle == "Epilogue"
    assert release.is_premium
    assert render_release(release, rename_policy) == (
        "Series v01 - Epilogue [2022] [Publisher] [Premium] "
        "(Digital) [Group].epub"
    )


def test_bracket_only_unicode_and_numeric_titles_do_not_require_filesystem_access() -> None:
    bracketed = parse_release(
        FileObservation(path=Path("Library/[特別編].cbz"), root_role="library"),
        ParsingPolicy(),
    )
    numeric = parse_release(
        FileObservation(path=Path("Library/12345.cbz"), root_role="library"),
        ParsingPolicy(),
    )

    assert bracketed.name == "[特別編].cbz"
    assert bracketed.display_series == "Library"
    assert numeric.name == "12345.cbz"
    assert isinstance(numeric.display_series, str)


def test_empty_optional_regex_lists_are_policy_local() -> None:
    custom = ParsingPolicy(number_policy=NumberPolicy((), (), (), ()))
    release = parse_release(
        observation(
            "Series v01.cbz",
            siblings=(Path("Library/Other.cbz"),),
            root_role="library",
        ),
        custom,
    )

    assert release.number.kind == "missing"
    assert release.kind == "volume"


def test_unicode_series_display_is_not_transliterated_for_matching() -> None:
    release = parse_release(
        FileObservation(
            path=Path("Library/Café 特別編 v01.cbz"),
            root_role="library",
        ),
        ParsingPolicy(),
    )

    assert release.display_series == "Café 特別編"
    assert release.canonical_series_key != release.display_series

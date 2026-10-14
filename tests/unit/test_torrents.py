from __future__ import annotations

from pathlib import Path

import pytest

from komga_cover_extractor.application.torrents import plan_torrent
from komga_cover_extractor.domain.libraries import LibraryRoot, LibrarySnapshot
from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.integrations.http import HttpError
from komga_cover_extractor.integrations.qbittorrent import (
    parse_webapi_version,
    validate_files,
    validate_torrents,
)
from komga_cover_extractor.matching.ranking import RankingPolicy, RankRule
from komga_cover_extractor.matching.series import MatchingPolicy
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release

HASH = "a" * 40
PARSING = ParsingPolicy()
MATCHING = MatchingPolicy(match_through_image_similarity=False)
RANKING = RankingPolicy((RankRule("Digital", 2),))


def _torrent(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {"hash": HASH, "name": "Series", "category": "books",
                             "state": "pausedDL", "added_on": 100,
                             "save_path": "/remote/downloads", "total_size": 100}
    raw.update(overrides)
    return validate_torrents([raw])[0]


def _files(*names: str) -> tuple[dict[str, object], ...]:
    return validate_files([{"index": index, "name": name, "priority": 1, "size": 100}
                           for index, name in enumerate(names)])


def _library(*names: str) -> LibrarySnapshot:
    releases = tuple(parse_release(FileObservation(Path("/library/Series") / name,
                                                  root_role="library"), PARSING)
                     for name in names)
    return LibrarySnapshot(releases, (LibraryRoot(Path("/library")),))


def _plan(torrent: dict[str, object], files: tuple[dict[str, object], ...],
          library: LibrarySnapshot, **options: object):
    return plan_torrent(torrent, files, library, parsing=PARSING, matching=MATCHING,
                        ranking=RANKING, options={"qbittorrent_target_category": "books", **options})


@pytest.mark.parametrize(("state", "category", "reason"), [
    ("pausedDL", "books", "qbit_start_retained"),
    ("stoppedDL", "books", "qbit_start_retained"),
    ("downloading", "books", "qbit_ineligible_torrent"),
    ("pausedDL", "Books", "qbit_ineligible_torrent"),
])
def test_exact_category_and_eligible_states(state: str, category: str, reason: str) -> None:
    decisions = _plan(_torrent(state=state, category=category), _files("Series v02.cbz"),
                      _library("Series v01.cbz"))
    assert decisions[-1].reason == reason
    assert decisions[-1].candidates == (HASH,)


@pytest.mark.parametrize(("incoming", "existing", "status", "reason"), [
    ("Series v01.cbz", "Series v01 (Digital).cbz", "planned", "qbit_exclude_lower_rank"),
    ("Series v01 (Digital).cbz", "Series v01.cbz", "observed", "qbit_keep_strict_upgrade"),
    ("Series v01.cbz", "Series v01.cbz", "review_required", "qbit_equal_rank_content_identity_unknown"),
    ("Series v02.cbz", "Series v01 (Digital).cbz", "observed", "qbit_keep_new_release"),
    ("Series v01.cbr", "Series v01 (Digital).cbz", "planned", "qbit_exclude_lower_rank"),
    ("Series v01.rar", "Series v01 (Digital).cbz", "planned", "qbit_exclude_lower_rank"),
    ("Series v01.7z", "Series v01 (Digital).cbz", "planned", "qbit_exclude_lower_rank"),
])
def test_shared_release_number_match_and_strict_ranking(
    incoming: str, existing: str, status: str, reason: str,
) -> None:
    decisions = _plan(_torrent(), _files(incoming), _library(existing))
    assert (decisions[0].status, decisions[0].reason) == (status, reason)
    assert decisions[0].candidates == (HASH, incoming, "0")
    if status == "review_required":
        assert len(decisions) == 1


def test_full_relative_path_and_index_survive_equal_basenames() -> None:
    files = validate_files([
        {"index": 8, "name": "first/Series v01.cbz", "priority": 1, "size": 100},
        {"index": 12, "name": "second/Series v01.cbz", "priority": 1, "size": 200},
    ])
    decisions = _plan(_torrent(), files, _library("Series v01 (Digital).cbz"))
    assert [item.candidates for item in decisions] == [
        (HASH, "first/Series v01.cbz", "8"),
        (HASH, "second/Series v01.cbz", "12"),
        (HASH,),
    ]
    assert [item.reason for item in decisions] == [
        "qbit_exclude_lower_rank", "qbit_exclude_lower_rank", "qbit_remove_all_excluded",
    ]


def test_unacceptable_policy_uses_basename_unless_title_filter_is_explicit() -> None:
    files = _files("Rejected Folder/Series v02.cbz", "clean/Rejected v03.cbz")
    options = {"unacceptable_keywords": ("Rejected",), "delete_unacceptable_files_toggle": True}
    decisions = _plan(_torrent(name="Rejected pack"), files, _library(), **options)
    assert [item.reason for item in decisions] == [
        "qbit_keep_new_series", "qbit_exclude_unacceptable_basename", "qbit_start_retained",
    ]
    title = _plan(_torrent(name="Rejected pack"), files, _library(),
                  **options, delete_unacceptable_torrent_titles_in_qbit=True)
    assert [item.reason for item in title] == [
        "qbit_exclude_unacceptable_title", "qbit_exclude_unacceptable_title", "qbit_remove_all_excluded",
    ]


def test_unavailable_library_prevents_even_explicit_keyword_exclusion() -> None:
    decisions = _plan(_torrent(), _files("Rejected.cbz"), LibrarySnapshot(available=False),
                      delete_unacceptable_files_toggle=True, unacceptable_keywords=("Rejected",))
    assert [(item.status, item.reason) for item in decisions] == [
        ("missing_dependency", "qbit_library_unavailable"),
    ]


def test_ambiguous_matching_prevents_all_actions_for_that_torrent() -> None:
    existing = tuple(parse_release(FileObservation(Path(parent) / "Series v01.cbz"), PARSING)
                     for parent in ("/library/first/Series", "/library/second/Series"))
    library = LibrarySnapshot(existing, (LibraryRoot(Path("/library")),))
    decisions = _plan(_torrent(), _files("Rejected.txt", "Series v02.cbz"), library,
                      delete_unacceptable_files_toggle=True, unacceptable_keywords=("Rejected",))
    assert decisions[0].status == "skipped"
    assert decisions[0].reason == "qbit_torrent_undecided"
    assert decisions[1].status == "review_required"
    assert len(decisions) == 2


@pytest.mark.parametrize(("incoming", "existing", "reason"), [
    ("Series c000.cbz", "Series c001.cbz", "qbit_keep_new_release"),
    ("Series v01.01.cbz", "Series v01.10.cbz", "qbit_keep_new_release"),
    ("Series v01 Part 12.cbz", "Series v01 Part 10.cbz", "qbit_keep_new_release"),
    ("Series v01-03.cbz", "Series v02 (Digital).cbz", "qbit_collection_or_rank_ambiguous"),
])
def test_known_zero_exact_decimals_parts_and_collection_uncertainty(
    incoming: str, existing: str, reason: str,
) -> None:
    decisions = _plan(_torrent(), _files(incoming), _library(existing))
    assert decisions[0].reason == reason


def test_old_file_responses_use_complete_response_order_without_basename_keys() -> None:
    files = validate_files([
        {"name": "first/Series.cbz", "priority": 1, "size": 2},
        {"name": "second/Series.cbz", "priority": 1, "size": 3},
    ])
    assert [(item["index"], item["name"]) for item in files] == [
        (0, "first/Series.cbz"), (1, "second/Series.cbz"),
    ]


@pytest.mark.parametrize("name", ["../bad.cbz", "/bad.cbz", "C:/bad.cbz", "dir\\bad.cbz",
                                 "dir/../bad.cbz", "dir//bad.cbz", "dir/./bad.cbz", "bad\x00.cbz"])
def test_unsafe_torrent_relative_paths_are_not_observations(name: str) -> None:
    with pytest.raises(HttpError) as failure:
        validate_files([{"name": name, "priority": 1, "size": 10}])
    assert failure.value.status == "invalid_input"


@pytest.mark.parametrize("payload", [
    [{"name": "a.cbz", "index": 0, "priority": 1, "size": 1},
     {"name": "b.cbz", "priority": 1, "size": 1}],
    [{"name": "a.cbz", "index": 0, "priority": 1, "size": 1},
     {"name": "b.cbz", "index": 0, "priority": 1, "size": 1}],
    [{"name": "a.cbz", "priority": 1, "size": True}],
    [{"name": "a.cbz", "priority": 99, "size": 1}],
    [{"name": "a.cbz", "priority": 1, "size": 1, "progress": float("nan")}],
])
def test_ambiguous_or_malformed_file_schema_fails_closed(payload: object) -> None:
    with pytest.raises(HttpError) as failure:
        validate_files(payload)
    assert failure.value.status == "invalid_input"


@pytest.mark.parametrize(("text", "expected"), [("2.8.2", (2, 8, 2)), ("2.11.0", (2, 11, 0)), ("2.0", (2, 0, 0))])
def test_supported_api_versions_are_numeric(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_webapi_version(text) == expected


@pytest.mark.parametrize("text", ["", "unknown", "1.0", "3.0.0", "2.11.0beta", "2.11.0.1"])
def test_unknown_api_versions_do_not_authorize_mutation(text: str) -> None:
    with pytest.raises(HttpError) as failure:
        parse_webapi_version(text)
    assert (failure.value.status, failure.value.reason) == ("unsupported", "qbit_unsupported_webapi_version")


def test_oversized_numeric_api_version_is_a_typed_unsupported_response() -> None:
    with pytest.raises(HttpError) as failure:
        parse_webapi_version("2." + "9" * 5000)
    assert (failure.value.status, failure.value.reason) == ("unsupported", "qbit_unsupported_webapi_version")


@pytest.mark.parametrize("roots", [(), (LibraryRoot(Path("/downloads"), roles=("download",)),)])
def test_missing_library_roles_are_not_proof_of_a_new_series(roots: tuple[LibraryRoot, ...]) -> None:
    decisions = _plan(_torrent(), _files("Series v02.cbz"), LibrarySnapshot(roots=roots),
                      delete_unacceptable_files_toggle=True, unacceptable_keywords=("Series",))
    assert [(item.status, item.reason) for item in decisions] == [
        ("missing_dependency", "qbit_library_roots_unavailable"),
    ]


def test_explicit_collection_members_are_not_unchecked_for_partial_existing_coverage() -> None:
    decisions = _plan(_torrent(), _files("Series v01-03-05.cbz"), _library("Series v01-03 (Digital).cbz"))
    assert [(item.status, item.reason) for item in decisions] == [
        ("review_required", "qbit_collection_or_rank_ambiguous"),
    ]

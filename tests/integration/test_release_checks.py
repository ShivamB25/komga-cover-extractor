from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path
from threading import Event
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from komga_cover_extractor.application.release_checks import (
    bookwalker_notifications,
    check_bookwalker_releases,
    compare_bookwalker_releases,
)
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseNumber,
    ReleaseSnapshot,
)
from komga_cover_extractor.integrations.bookwalker import (
    Book,
    BookWalkerError,
    ObservationState,
    fetch_html,
    group_bookwalker_books,
    search_bookwalker,
)
from komga_cover_extractor.integrations.http import HttpClient, HttpError
from komga_cover_extractor.parsing.numbers import NumberPolicy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bookwalker"
TODAY = date(2026, 10, 13)


class Response:
    def __init__(self, content: bytes):
        self.content = content
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Client:
    def __init__(self, pages: dict[str, bytes | HttpError]):
        self.pages = pages
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.responses: list[Response] = []

    def request(self, method: str, relative: str, **kwargs: object) -> Response:
        self.calls.append((method, relative, kwargs))
        assert (method, kwargs) == ("GET", {"read_only": True, "expected_statuses": (200,)})
        content = self.pages[relative]
        if isinstance(content, HttpError):
            raise content
        response = Response(content)
        self.responses.append(response)
        return response

    @property
    def http(self) -> HttpClient:
        return cast(HttpClient, self)


def pages() -> dict[str, bytes | HttpError]:
    return {
        "search/?word=Series&np=0": (FIXTURES / "series.html").read_bytes(),
        "search/?word=Series&page=1&qcat=2": (FIXTURES / "search-1.html").read_bytes(),
        "search/?word=Series&page=2&qcat=2": (FIXTURES / "search-2.html").read_bytes(),
        "book-1/": (FIXTURES / "released.html").read_bytes(),
        "book-2/": (FIXTURES / "today.html").read_bytes(),
        "book-3/": (FIXTURES / "preorder.html").read_bytes(),
        "book-4/": (FIXTURES / "unknown.html").read_bytes(),
    }


def search_page(*titles: str, novel: bool = False, total_pages: int = 1) -> bytes:
    tag, label = ("a-tag-light-novel", "Light Novel") if novel else ("a-tag-manga", "Manga")
    rows = "".join(
        f"<li class='o-tile'><div class='o-tile-book-info'><a class='a-tile-thumb-img' href='/item-{index}/'></a>"
        f"<div class='{tag}'>{label}</div><h2 class='a-tile-ttl'>{escape(title)}</h2></div></li>"
        for index, title in enumerate(titles, 1)
    )
    pager = f"<div class='pager-area'><li>1</li><li>{total_pages}</li></div>" if total_pages > 1 else ""
    return (f"<html><body>{pager}<ul class='o-tile-list'>{rows}</ul></body></html>").encode()


def detail_page(series: str = "Series", alternative: str = "", released: str = "October 13, 2026") -> bytes:
    return ("<div class='product-detail-inner'><table class='product-detail'>"
            f"<tr><th>Series Title</th><td>{escape(series)}</td></tr>"
            f"<tr><th>Alternative Title</th><td>{escape(alternative)}</td></tr>"
            f"<tr><th>Release Date</th><td>{released}</td></tr></table></div>").encode()


def local(number: ReleaseNumber, *, novel: bool = False, premium: bool = False,
          role: str = "library", path: Path = Path("/library/Series/Series.cbz")) -> ReleaseSnapshot:
    if novel:
        path = path.with_suffix(".epub")
    return ReleaseSnapshot(FileObservation(path, root_role=role), number=number, kind="volume",
                           display_series="Series", canonical_series_key="series", is_premium=premium)


def remote(number: ReleaseNumber, *, premium: bool = False, url: str = "https://global.bookwalker.jp/book/",
           title: str = "Series", date_status: ObservationState = "known", released: date | None = TODAY) -> Book:
    return {"title": title, "original_title": title + " Volume " + "-".join(number.raw_tokens),
            "number": number, "premium": premium, "book_type": "novel", "url": url, "thumbnail": None,
            "date": released, "date_status": date_status,
            "release_state": "released" if released is not None and released <= TODAY else "preorder" if released else "unknown",
            "price": None, "price_status": "unknown", "currency": None, "description": "", "preview_image_url": None,
            "alternative_titles": (), "diagnostics": ()}


def test_search_reads_each_page_and_detail_and_never_fetches_chapters() -> None:
    client = Client(pages())
    result = search_bookwalker(client.http, "Series", "manga", number_policy=NumberPolicy(),
                              required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert [(book["number"].raw_tokens, book["release_state"], book["price"]) for book in result["books"]] == [
        (("1",), "released", Decimal("6.99")), (("2",), "released", Decimal("0.00")),
        (("3",), "preorder", None), (("4",), "unknown", None),
    ]
    assert result["complete"] is True
    assert [relative for _, relative, _ in client.calls] == [
        "search/?word=Series&np=0", "search/?word=Series&page=1&qcat=2", "book-1/", "book-2/",
        "search/?word=Series&page=2&qcat=2", "book-3/", "book-4/",
    ]
    assert all(response.closed for response in client.responses)


def test_international_manga_fallback_uses_category_eleven_and_not_retry_of_category_two() -> None:
    responses = {"search/?word=Series&np=0": (FIXTURES / "series.html").read_bytes(),
                 "search/?word=Series&page=1&qcat=2": search_page(),
                 "search/?word=Series&page=1&qcat=11": search_page("Series Volume 2"),
                 "item-1/": detail_page()}
    client = Client(responses)
    result = search_bookwalker(client.http, "Series", "manga", number_policy=NumberPolicy(),
                              required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert [book["number"].raw_tokens for book in result["books"]] == [("2",)]
    assert [parse_qs(urlsplit(relative).query)["qcat"] for _, relative, _ in client.calls if "qcat=" in relative] == [["2"], ["11"]]


def test_novel_categories_keep_parts_and_premium_and_match_alternative_names() -> None:
    client = Client({"search/?word=Series&np=0": (FIXTURES / "series.html").read_bytes(),
                     "search/?word=Series&page=1&qcat=3": search_page("Another Name Volume 1 Part 12 (Premium)", novel=True),
                     "item-1/": detail_page("Another Name", "Series")})
    result = search_bookwalker(client.http, "Series", "novel", number_policy=NumberPolicy(),
                              required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert [(book["title"], book["number"].raw_tokens, book["number"].part, book["premium"]) for book in result["books"]] == [
        ("Another Name", ("1",), 12, True),
    ]
    assert [relative for _, relative, _ in client.calls] == ["search/?word=Series&np=0", "search/?word=Series&page=1&qcat=3", "item-1/"]


def test_nonzero_alternative_similarity_is_not_a_match_below_threshold() -> None:
    client = Client({"search/?word=Series&np=0": (FIXTURES / "series.html").read_bytes(),
                     "search/?word=Series&page=1&qcat=3": search_page("Series Volume 2", novel=True),
                     "item-1/": detail_page("Different", "Seriesx")})
    result = search_bookwalker(client.http, "Series", "novel", number_policy=NumberPolicy(),
                              required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert result == {"books": (), "complete": True, "diagnostics": ()}
    assert [relative for _, relative, _ in client.calls] == ["search/?word=Series&np=0", "search/?word=Series&page=1&qcat=3", "item-1/"]


def test_shortened_search_relaxation_is_local_even_when_its_unique_series_gate_exits() -> None:
    long_query = "Northfield Library - Subtitle"
    client = Client({
        "search/?" + urlencode({"word": long_query, "np": 0}): (FIXTURES / "series.html").read_bytes(),
        "search/?" + urlencode({"word": long_query, "page": 1, "qcat": 3}): search_page("Northfield Library Volume 2", novel=True),
        "item-1/": detail_page("Northfield Library - Subtitle"),
        "search/?word=Northfield+Library&np=0": b"<ul class='o-tile-list'><li class='o-tile'></li><li class='o-tile'></li></ul>",
    })
    threshold = 0.979
    result = search_bookwalker(client.http, long_query, "novel", number_policy=NumberPolicy(),
                              required_similarity_score=threshold, clock=lambda: TODAY, request_interval_seconds=0)
    assert [book["number"].raw_tokens for book in result["books"]] == [("2",)]
    other = Client({"search/?word=Northfield+Library&np=0": b"<ul class='o-tile-list'><li class='o-tile'></li><li class='o-tile'></li></ul>",
                    "search/?word=Northfield+Library&page=1&qcat=3": search_page("Northfield Library Volume 3", novel=True),
                    "item-1/": detail_page("Northfield Libraryx")})
    second = search_bookwalker(other.http, "Northfield Library", "novel", number_policy=NumberPolicy(),
                              required_similarity_score=threshold, clock=lambda: TODAY, request_interval_seconds=0)
    assert second["books"] == ()
    assert [relative for _, relative, _ in other.calls] == ["search/?word=Northfield+Library&np=0", "search/?word=Northfield+Library&page=1&qcat=3", "item-1/"]


def test_explicit_alias_queries_find_other_titles_without_duplicate_detail_requests() -> None:
    client = Client({"search/?word=Original&np=0": (FIXTURES / "series.html").read_bytes(),
                     "search/?word=Original&page=1&qcat=3": search_page(),
                     "search/?word=Series&np=0": (FIXTURES / "series.html").read_bytes(),
                     "search/?word=Series&page=1&qcat=3": search_page("Series Volume 2", novel=True),
                     "search/?word=Series+English&np=0": (FIXTURES / "series.html").read_bytes(),
                     "search/?word=Series+English&page=1&qcat=3": search_page("Series Volume 2", novel=True),
                     "item-1/": detail_page("Series", "Series English")})
    result = search_bookwalker(client.http, "Original", "novel", number_policy=NumberPolicy(),
                              required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0,
                              alternative_queries=("Series", "Series English", "Series"))
    assert [book["number"].raw_tokens for book in result["books"]] == [("2",)]
    assert [relative for _, relative, _ in client.calls] == [
        "search/?word=Original&np=0", "search/?word=Original&page=1&qcat=3",
        "search/?word=Series&np=0", "search/?word=Series&page=1&qcat=3", "item-1/",
        "search/?word=Series+English&np=0", "search/?word=Series+English&page=1&qcat=3",
    ]


def test_grouping_preserves_every_book_deduplicates_by_url_and_sorts_numerically() -> None:
    first = remote(ReleaseNumber.scalar("1"), url="https://global.bookwalker.jp/1/")
    second = remote(ReleaseNumber.scalar("2"), url="https://global.bookwalker.jp/2/")
    tenth = remote(ReleaseNumber.scalar("10"), url="https://global.bookwalker.jp/10/")
    books = [tenth, first, second, first]
    groups = group_bookwalker_books(books, required_similarity_score=0.979)
    assert [[book["number"].raw_tokens for book in group] for group in groups] == [[("1",), ("2",), ("10",)]]
    assert [book["number"].raw_tokens for book in books] == [("10",), ("1",), ("2",), ("1",)]
    conflicting = {**first, "number": ReleaseNumber.scalar("3")}
    with pytest.raises(BookWalkerError) as caught:
        group_bookwalker_books([first, conflicting], required_similarity_score=0.979)
    assert (caught.value.status, caught.value.reason) == ("review_required", "bookwalker_conflicting_product_observations")


def test_distinct_matching_remote_series_are_ambiguous_not_empty_success() -> None:
    client = Client({"search/?word=Series&np=0": (FIXTURES / "series.html").read_bytes(),
                     "search/?word=Series&page=1&qcat=3": search_page("Alpha Volume 2", "Omega Volume 2", novel=True),
                     "item-1/": detail_page(), "item-2/": detail_page()})
    with pytest.raises(BookWalkerError) as caught:
        search_bookwalker(client.http, "Series", "novel", number_policy=NumberPolicy(),
                          required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert (caught.value.status, caught.value.reason, caught.value.candidates) == ("ambiguous", "bookwalker_multiple_series", ("Alpha", "Omega"))


@pytest.mark.parametrize(("failure", "status", "reason"), (
    (HttpError("permission_denied", "http_403"), "permission_denied", "http_403"),
    (HttpError("delivery_deferred", "http_rate_limited"), "delivery_deferred", "http_rate_limited"),
    (b"", "invalid_input", "bookwalker_empty_body"),
    (b"<html>Changed DOM</html>", "invalid_input", "bookwalker_search_dom_missing"),
))
def test_fetch_and_dom_failures_never_become_no_releases(failure: bytes | HttpError, status: str, reason: str) -> None:
    client = Client({"search/?word=Series&np=0": failure})
    with pytest.raises(BookWalkerError) as caught:
        search_bookwalker(client.http, "Series", "manga", number_policy=NumberPolicy(),
                          required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert (caught.value.status, caught.value.reason) == (status, reason)
    assert len(client.calls) == 1


def test_preview_entry_compares_local_books_and_returns_separate_channel_items_without_writes(tmp_path: Path) -> None:
    source = tmp_path / "Series" / "Series v01.cbz"
    source.parent.mkdir()
    source.write_bytes(b"original fixture bytes")
    client = Client(pages())
    result = check_bookwalker_releases([local(ReleaseNumber.scalar("1"), path=source)], client=client.http,
                                      number_policy=NumberPolicy(), required_similarity_score=0.979,
                                      authorization=RunAuthorization(), clock=lambda: TODAY, request_interval_seconds=0)
    assert result.exit_code == 3
    assert [(decision.status, decision.reason) for decision in result.decisions] == [
        ("skipped", "bookwalker_exact_match"), ("observed", "bookwalker_new_release"),
        ("observed", "bookwalker_new_release"), ("review_required", "bookwalker_date_unknown"),
    ]
    assert (result.committed, result.planned, result.skipped, result.failed, result.deferred_deliveries) == (0, 0, 1, 0, ())
    assert result.missing_releases[0]["delivery_authorized"] is False
    encoded = json.dumps(result.as_dict())
    assert '"price": null' in encoded and '"price": "0.00"' in encoded
    notifications = bookwalker_notifications(result.missing_releases)
    assert [(item["channel"], item["item"]["volume_number"], item["item"]["date"]) for item in notifications] == [
        ("bookwalker_released", "2", "2026-10-13"), ("bookwalker_preorder", "3", "2026-12-25"),
    ]
    assert source.read_bytes() == b"original fixture bytes"
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == ["Series", "Series/Series v01.cbz"]
    assert [method for method, _, _ in client.calls] == ["GET"] * 7


@pytest.mark.parametrize(("observed", "incoming", "expected_status", "expected_reason"), (
    (ReleaseNumber.scalar("1"), ReleaseNumber.scalar("1"), "existing", "exact_match"),
    (ReleaseNumber.range("1", "3"), ReleaseNumber.scalar("2"), "existing", "covered_range"),
    (ReleaseNumber.explicit_set(("1", "3", "5")), ReleaseNumber.scalar("2"), "missing", "new_release"),
    (ReleaseNumber.scalar("1", part=12), ReleaseNumber.scalar("1", part=12), "existing", "exact_match"),
    (ReleaseNumber.scalar("1"), ReleaseNumber.scalar("1", part=12), "existing", "complete_volume_covers_part"),
    (ReleaseNumber.scalar("1", part=12), ReleaseNumber.scalar("1"), "missing", "complete_volume_available"),
    (ReleaseNumber.scalar("1"), ReleaseNumber.range("1", "3"), "missing", "incomplete_collection"),
    (ReleaseNumber.missing(), ReleaseNumber.scalar("2"), "review_required", "unknown_local_number"),
    (ReleaseNumber.scalar("1"), ReleaseNumber.missing(), "review_required", "unknown_remote_number"),
    (ReleaseNumber.scalar("1.01"), ReleaseNumber.scalar("1.10"), "missing", "new_release"),
    (ReleaseNumber.scalar("1"), ReleaseNumber.range("1", "10001"), "review_required", "range_limit"),
))
def test_comparison_respects_numeric_domains_and_novel_parts(observed: ReleaseNumber, incoming: ReleaseNumber, expected_status: str, expected_reason: str) -> None:
    result = compare_bookwalker_releases([local(observed, novel=True)], [remote(incoming)], book_type="novel")
    assert (result[0]["status"], result[0]["reason"]) == (expected_status, expected_reason)


def test_collections_require_full_coverage_and_premium_is_not_hidden_by_standard_edition() -> None:
    collection = remote(ReleaseNumber.range("1", "3"))
    result = compare_bookwalker_releases([local(ReleaseNumber.scalar(str(number)), novel=True) for number in (1, 2, 3)], [collection], book_type="novel")
    assert (result[0]["status"], result[0]["reason"]) == ("existing", "covered_by_members")
    result = compare_bookwalker_releases([local(ReleaseNumber.scalar("1"), novel=True)], [remote(ReleaseNumber.scalar("1"), premium=True)], book_type="novel")
    assert (result[0]["status"], result[0]["reason"]) == ("missing", "premium_edition_available")
    result = compare_bookwalker_releases([local(ReleaseNumber.scalar("1"), novel=True, premium=True)], [remote(ReleaseNumber.scalar("1"))], book_type="novel")
    assert (result[0]["status"], result[0]["reason"]) == ("existing", "exact_match")
    result = compare_bookwalker_releases([local(ReleaseNumber.scalar("1"))], [remote(ReleaseNumber.scalar("1", part=12))], book_type="manga")
    assert (result[0]["status"], result[0]["reason"]) == ("missing", "new_release")


def test_page_limit_is_an_incomplete_report_and_does_not_schedule_delivery() -> None:
    client = Client(pages())
    result = check_bookwalker_releases([local(ReleaseNumber.scalar("1"))], client=client.http,
                                      number_policy=NumberPolicy(), required_similarity_score=0.979,
                                      authorization=RunAuthorization(allow_remote=True), clock=lambda: TODAY,
                                      max_pages=1, request_interval_seconds=0)
    assert result.exit_code == 3
    assert (result.decisions[0].status, result.decisions[0].reason) == ("review_required", "bookwalker_page_limit")
    assert result.missing_releases[0]["complete"] is False
    assert [decision.reason for decision in result.decisions[1:]] == ["bookwalker_exact_match", "bookwalker_new_release"]
    assert bookwalker_notifications(result.missing_releases) == ()
    assert [relative for _, relative, _ in client.calls] == ["search/?word=Series&np=0", "search/?word=Series&page=1&qcat=2", "book-1/", "book-2/"]


def test_download_overlap_and_mixed_roots_are_not_searched() -> None:
    client = Client({})
    for role in ("download", "both"):
        result = check_bookwalker_releases([local(ReleaseNumber.scalar("1"), role=role)], client=client.http,
                                          number_policy=NumberPolicy(), required_similarity_score=0.979,
                                          authorization=RunAuthorization(), request_interval_seconds=0)
        assert [(item.status, item.reason) for item in result.decisions] == [("no_match", "bookwalker_no_library_volumes")]
    result = check_bookwalker_releases([local(ReleaseNumber.scalar("1")), local(ReleaseNumber.scalar("2"), novel=True)],
                                      client=client.http, number_policy=NumberPolicy(), required_similarity_score=0.979,
                                      authorization=RunAuthorization(), request_interval_seconds=0)
    assert [(item.status, item.reason) for item in result.decisions] == [("review_required", "bookwalker_mixed_folder_type")]
    assert client.calls == []


def test_cancellation_before_fetch_and_during_request_interval_is_bounded() -> None:
    cancel = Event()
    cancel.set()
    client = Client(pages())
    with pytest.raises(BookWalkerError) as caught:
        search_bookwalker(client.http, "Series", "manga", number_policy=NumberPolicy(),
                          required_similarity_score=0.979, clock=lambda: TODAY, cancel=cancel)
    assert (caught.value.status, caught.value.reason, client.calls) == ("cancelled", "bookwalker_cancelled", [])
    class CancelOnWait(Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout == 2
            self.set()
            return True
    with pytest.raises(BookWalkerError) as caught:
        search_bookwalker(client.http, "Series", "manga", number_policy=NumberPolicy(),
                          required_similarity_score=0.979, clock=lambda: TODAY, cancel=CancelOnWait())
    assert (caught.value.status, caught.value.reason) == ("cancelled", "bookwalker_cancelled")
    assert [relative for _, relative, _ in client.calls] == ["search/?word=Series&np=0"]


def test_fetch_closes_empty_response_and_rejects_non_html_content() -> None:
    client = Client({"empty/": b""})
    with pytest.raises(BookWalkerError) as caught:
        fetch_html(client.http, "empty/")
    assert (caught.value.reason, client.responses[0].closed) == ("bookwalker_empty_body", True)
    class JsonClient(Client):
        def request(self, method: str, relative: str, **kwargs: object) -> Response:
            response = super().request(method, relative, **kwargs)
            response.headers["Content-Type"] = "application/json"
            return response
    json_client = JsonClient({"data/": b"{}"})
    with pytest.raises(BookWalkerError) as caught:
        fetch_html(json_client.http, "data/")
    assert (caught.value.reason, json_client.responses[0].closed) == ("bookwalker_non_html_response", True)


@pytest.mark.parametrize(("state", "expected_reason"), (("unknown", "date_unknown"), ("invalid", "date_invalid"), ("ambiguous", "date_ambiguous")))
def test_unknown_or_invalid_dates_require_review_and_never_become_preorders(state: ObservationState, expected_reason: str) -> None:
    book = remote(ReleaseNumber.scalar("2"), date_status=state, released=None)
    comparisons = compare_bookwalker_releases([local(ReleaseNumber.scalar("1"), novel=True)], [book], book_type="novel")
    assert (comparisons[0]["status"], comparisons[0]["reason"]) == ("review_required", expected_reason)
    report = {"source": "bookwalker", "complete": True, "comparisons": list(comparisons)}
    assert bookwalker_notifications([report]) == ()
    valid = compare_bookwalker_releases([local(ReleaseNumber.scalar("1"), novel=True)], [remote(ReleaseNumber.scalar("2"))], book_type="novel")
    notifications = bookwalker_notifications([{"source": "bookwalker", "complete": True, "comparisons": list(valid)}])
    assert [(notification["channel"], notification["item"]["date"]) for notification in notifications] == [("bookwalker_released", "2026-10-13")]


def test_notification_dates_keep_legacy_sort_direction_and_deduplicate_across_reports() -> None:
    books = [
        remote(ReleaseNumber.scalar("3"), released=date(2026, 12, 25), url="https://global.bookwalker.jp/3/"),
        remote(ReleaseNumber.scalar("2"), released=date(2026, 10, 13), url="https://global.bookwalker.jp/2/"),
        remote(ReleaseNumber.scalar("4"), released=date(2027, 1, 1), url="https://global.bookwalker.jp/4/"),
        remote(ReleaseNumber.scalar("1"), released=date(2026, 10, 12), url="https://global.bookwalker.jp/1/"),
    ]
    comparisons = compare_bookwalker_releases([], books, book_type="novel")
    report = {"source": "bookwalker", "complete": True, "comparisons": list(comparisons)}
    notifications = bookwalker_notifications([report, report])
    assert [(notification["channel"], notification["item"]["date"]) for notification in notifications] == [
        ("bookwalker_released", "2026-10-12"), ("bookwalker_released", "2026-10-13"),
        ("bookwalker_preorder", "2027-01-01"), ("bookwalker_preorder", "2026-12-25"),
    ]


def test_client_prerequisite_and_service_failure_remain_distinct_from_no_match() -> None:
    missing = check_bookwalker_releases([local(ReleaseNumber.scalar("1"))], client=None,
                                       number_policy=NumberPolicy(), required_similarity_score=0.979,
                                       authorization=RunAuthorization(), request_interval_seconds=0)
    assert (missing.exit_code, missing.decisions[0].status, missing.decisions[0].reason) == (
        2, "missing_dependency", "bookwalker_client_unavailable",
    )
    client = Client({"search/?word=Series&np=0": HttpError("permission_denied", "http_403")})
    denied = check_bookwalker_releases([local(ReleaseNumber.scalar("1"))], client=client.http,
                                      number_policy=NumberPolicy(), required_similarity_score=0.979,
                                      authorization=RunAuthorization(apply=True, allow_remote=True), request_interval_seconds=0)
    assert (denied.exit_code, denied.failed, denied.decisions[0].status, denied.decisions[0].reason) == (
        1, 1, "permission_denied", "http_403",
    )
    assert denied.missing_releases == ()
    assert [method for method, _, _ in client.calls] == ["GET"]


def test_raised_search_after_single_series_observation_does_not_relax_a_later_search() -> None:
    failed = Client({
        "search/?word=Northfield+Library&np=0": (FIXTURES / "series.html").read_bytes(),
        "search/?word=Northfield+Library&page=1&qcat=3": b"<html>Changed page layout</html>",
    })
    with pytest.raises(BookWalkerError) as caught:
        search_bookwalker(failed.http, "Northfield Library", "novel", number_policy=NumberPolicy(),
                          required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "bookwalker_search_dom_missing")
    later = Client({
        "search/?word=Northfield+Library&np=0": b"<ul class='o-tile-list'><li class='o-tile'></li><li class='o-tile'></li></ul>",
        "search/?word=Northfield+Library&page=1&qcat=3": search_page("Northfield Library Volume 2", novel=True),
        "item-1/": detail_page("Northfield Libraryx"),
    })
    result = search_bookwalker(later.http, "Northfield Library", "novel", number_policy=NumberPolicy(),
                              required_similarity_score=0.979, clock=lambda: TODAY, request_interval_seconds=0)
    assert result == {"books": (), "complete": True, "diagnostics": ()}
    assert [relative for _, relative, _ in later.calls] == [
        "search/?word=Northfield+Library&np=0", "search/?word=Northfield+Library&page=1&qcat=3", "item-1/",
    ]

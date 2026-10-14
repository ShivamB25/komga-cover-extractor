from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from komga_cover_extractor.integrations.bookwalker import (
    BookWalkerError,
    normalize_book_title,
    parse_detail_page,
    parse_search_page,
    parse_series_count,
    shortened_title,
)
from komga_cover_extractor.parsing.numbers import NumberPolicy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bookwalker"


def detail(rows: str, price: str = "") -> bytes:
    return ("<html><head>" + price + "</head><body><div class='product-detail-inner'>"
            "<table class='product-detail'>" + rows + "</table></div></body></html>").encode()


@pytest.mark.parametrize(("title", "series", "kind", "values", "part", "premium"), (
    ("Café Series Volume 1.01", "Café Series", "scalar", ("1.01",), None, False),
    ("Series Vol. 1.10", "Series", "scalar", ("1.10",), None, False),
    ("Series Volume 1 Part 12 (Premium)", "Series", "scalar", ("1",), 12, True),
    ("Series Volume 1 [Part 10]", "Series", "scalar", ("1",), 10, False),
    ("Series Volume 1-3", "Series", "range", ("1", "3"), None, False),
    ("Series Volume 1-3-5", "Series", "set", ("1", "3", "5"), None, False),
    ("Series 01 - 03 - 05", "Series", "set", ("01", "03", "05"), None, False),
    ("A Standalone Novel", "A Standalone Novel", "scalar", ("1",), None, False),
    ("Series 2026", "Series", "missing", (), None, False),
    ("Series Bonus", "Series Bonus", "missing", (), None, False),
    ("Premium Series Volume 2", "Premium Series", "scalar", ("2",), None, False),
))
def test_title_observations_keep_precision_parts_sets_and_display_unicode(
    title: str, series: str, kind: str, values: tuple[str, ...], part: int | None, premium: bool,
) -> None:
    display, number, observed_premium = normalize_book_title(title, number_policy=NumberPolicy())
    assert (display, number.kind, number.raw_tokens, number.part, observed_premium) == (series, kind, values, part, premium)
    assert tuple(str(value) for value in number.values) == tuple(str(Decimal(token)) for token in values)


@pytest.mark.parametrize("title", ("Series Volume 1 Part 1.2", "Series Volume 1 Part 0", "Series Volume 1 Part 2 Part 3"))
def test_invalid_parts_are_typed_instead_of_volume_fractions(title: str) -> None:
    with pytest.raises(BookWalkerError) as caught:
        normalize_book_title(title, number_policy=NumberPolicy())
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "bookwalker_unsupported_part_number")


def test_optional_volume_keywords_do_not_break_bare_numbers_or_leak_between_policies() -> None:
    custom = NumberPolicy(volume_keywords=("Installments?",))
    display, number, _ = normalize_book_title("Series Installment 02", number_policy=custom)
    assert (display, number.raw_tokens) == ("Series", ("02",))
    display, number, _ = normalize_book_title("Series 02", number_policy=NumberPolicy(volume_keywords=()))
    assert (display, number.raw_tokens) == ("Series", ("02",))
    display, number, _ = normalize_book_title("Series Volume 03", number_policy=NumberPolicy())
    assert (display, number.raw_tokens) == ("Series", ("03",))


def test_search_page_preserves_links_tags_thumbnail_and_pagination() -> None:
    items, pages = parse_search_page((FIXTURES / "search-1.html").read_bytes())
    assert pages == 2
    assert [(item["original_title"], item["url"], item["book_type"], item["chapter"]) for item in items] == [
        ("Series Volume 1", "https://global.bookwalker.jp/book-1/", "manga", False),
        ("Series Volume 2", "https://global.bookwalker.jp/book-2/", "manga", False),
        ("Series Chapter 4", "https://global.bookwalker.jp/chapter-4/", "manga", True),
    ]
    assert items[0]["thumbnail"] == "https://example.invalid/1-large.jpg"
    assert items[1]["thumbnail"] == "https://example.invalid/2.jpg"
    assert parse_series_count((FIXTURES / "series.html").read_bytes()) == 1


@pytest.mark.parametrize(("content", "reason"), (
    (b"", "bookwalker_empty_body"),
    (b"<html><body>Changed site</body></html>", "bookwalker_search_dom_missing"),
    (b"<ul class='o-tile-list'>Different result rows</ul>", "bookwalker_search_dom_changed"),
    (b"<ul class='o-tile-list'><li class='changed-tile'><img src='/cover.jpg'></li></ul>", "bookwalker_search_dom_changed"),
    (b"<ul class='o-tile-list'></ul><div class='pager-area'><li>Next</li></div>", "bookwalker_invalid_pagination"),
    (b"<ul class='o-tile-list'><li class='o-tile'></li></ul>", "bookwalker_book_info_missing"),
))
def test_empty_changed_or_malformed_search_pages_are_not_success(content: bytes, reason: str) -> None:
    with pytest.raises(BookWalkerError) as caught:
        parse_search_page(content)
    assert (caught.value.status, caught.value.reason) == ("invalid_input", reason)


@pytest.mark.parametrize("url", (
    "https://global.bookwalker.jp.evil.invalid/book/", "https://person:secret@global.bookwalker.jp/book/",
    "http://global.bookwalker.jp/book/", "https://other.invalid/book/", "https://[broken/",
))
def test_product_links_cannot_escape_the_fetch_origin(url: str) -> None:
    html = ("<ul class='o-tile-list'><li class='o-tile'><div class='o-tile-book-info'>"
            f"<a class='a-tile-thumb-img' href='{url}'></a><div class='a-tag-manga'>Manga</div>"
            "<h2 class='a-tile-ttl'>Series Volume 1</h2></div></li></ul>").encode()
    with pytest.raises(BookWalkerError) as caught:
        parse_search_page(html)
    assert caught.value.status == "invalid_input"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(("value", "expected_date", "expected_state"), (
    ("October 13, 2026", date(2026, 10, 13), "known"),
    ("Oct 3, 2026", date(2026, 10, 3), "known"),
    ("2026-10-13", date(2026, 10, 13), "known"),
    ("TBA", None, "unknown"),
    ("", None, "unknown"),
    ("February 31, 2026", None, "invalid"),
    ("Changed date syntax", None, "invalid"),
    ("October 13, 2026 or October 14, 2026", None, "ambiguous"),
))
def test_dates_distinguish_unknown_invalid_and_ambiguous(value: str, expected_date: date | None, expected_state: str) -> None:
    result = parse_detail_page(detail(f"<tr><th>Release Date</th><td>{value}</td></tr>"))
    assert (result["date"], result["date_status"]) == (expected_date, expected_state)
    assert (result["price"], result["price_status"]) == (None, "unknown")


@pytest.mark.parametrize(("price", "expected", "state"), (
    ("", None, "unknown"),
    ("<meta itemprop='price' content='0.00'>", Decimal("0.00"), "known"),
    ("<span itemprop='price'>$1,234.50</span>", Decimal("1234.50"), "known"),
    ("<meta itemprop='price' content='free soon'>", None, "invalid"),
    ("<meta itemprop='price' content='4.99'><meta itemprop='price' content='6.99'>", None, "ambiguous"),
))
def test_price_is_observed_not_fabricated(price: str, expected: Decimal | None, state: str) -> None:
    result = parse_detail_page(detail("<tr><th>Series Title</th><td>Series</td></tr>", price))
    assert (result["price"], result["price_status"]) == (expected, state)
    assert (result["date"], result["date_status"]) == (None, "unknown")


def test_description_optional_classes_and_mature_preview_fallback() -> None:
    result = parse_detail_page((FIXTURES / "released.html").read_bytes())
    assert result["description"] == "The first book's description.\nSecond paragraph."
    assert (result["price"], result["currency"]) == (Decimal("6.99"), "USD")
    result = parse_detail_page((FIXTURES / "today.html").read_bytes())
    assert result["description"] == "Released today."
    assert result["preview_image_url"] == "https://example.invalid/today-cover.jpg"
    assert result["titles"] == ("Series", "Another Series Name")


@pytest.mark.parametrize(("html", "reason"), (
    (b"<div>Changed detail</div>", "bookwalker_detail_dom_missing"),
    (b"<div class='product-detail-inner'><table class='product-detail'></table></div>", "bookwalker_detail_rows_missing"),
    (detail("<tr><td>October 13, 2026</td></tr>"), "bookwalker_detail_row_malformed"),
))
def test_missing_detail_nodes_are_explicit_errors(html: bytes, reason: str) -> None:
    with pytest.raises(BookWalkerError) as caught:
        parse_detail_page(html)
    assert caught.value.reason == reason


def test_shortened_title_requires_the_original_separator_shape() -> None:
    assert shortened_title("Series - A Subtitle") == "Series"
    assert shortened_title("Series: A Subtitle") == "Series"
    assert shortened_title("Series-with-hyphens") == ""


def test_missing_html_capability_is_named_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    original_import = builtins.__import__
    def without_bs4(name: str, *args: object, **kwargs: object) -> object:
        if name == "bs4":
            raise ImportError("fixture capability unavailable")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_bs4)
    with pytest.raises(BookWalkerError) as caught:
        parse_detail_page(b"<html><body>Fixture</body></html>")
    assert (caught.value.status, caught.value.reason) == ("missing_dependency", "bookwalker_requires_integrations_extra")

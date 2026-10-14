"""BookWalker HTML observations and pure release decisions.

Selectors come from the frozen application's search and detail readers. The
checked-in pages are synthetic; they do not establish live-site compatibility.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib import import_module
from threading import Event
from typing import TYPE_CHECKING, Literal, TypedDict
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import regex

from ..domain.decisions import Status
from ..domain.releases import ReleaseNumber
from ..parsing.normalization import (
    clean_str,
    remove_brackets,
    remove_dual_space,
    similar,
)
from ..parsing.numbers import NumberPolicy, parse_release_number

if TYPE_CHECKING:
    from bs4 import Tag

    from .http import HttpClient

BOOKWALKER_ORIGIN = "https://global.bookwalker.jp/"
BOOKWALKER_COOKIES = (("glSafeSearch", "1"), ("safeSearch", "111"))
BOOKWALKER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
)
_CATEGORIES = {"manga": 2, "novel": 3}
_INTERNATIONAL_MANGA_CATEGORY = 11
_SINGLE_SERIES_RELAXATION = 0.03
_VOLUME_YEAR_CUTOFF = Decimal(2000)
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_DATE_PATTERN = regex.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})\b",
    regex.IGNORECASE,
)
_ISO_DATE_PATTERN = regex.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TRAILING_NUMBER = regex.compile(r"(?:[,\s]+)(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)*)\s*$")
_PART_PATTERN = regex.compile(r"\bPart[-_. ]*(\d+(?:[._-]\d+)*)\b", regex.IGNORECASE)
_PREMIUM_PATTERN = regex.compile(r"[\[({]\s*(?:J[- ]Novel Club\s+)?Premium(?:\s+Edition)?\s*[\])}]", regex.IGNORECASE)

ObservationState = Literal["known", "unknown", "invalid", "ambiguous"]


class SearchItem(TypedDict):
    original_title: str
    url: str
    thumbnail: str | None
    book_type: str
    chapter: bool


class BookDetails(TypedDict):
    titles: tuple[str, ...]
    date: date | None
    date_status: ObservationState
    price: Decimal | None
    price_status: ObservationState
    currency: str | None
    description: str
    preview_image_url: str | None
    diagnostics: tuple[str, ...]


class Book(TypedDict):
    title: str
    original_title: str
    number: ReleaseNumber
    premium: bool
    book_type: str
    url: str
    thumbnail: str | None
    date: date | None
    date_status: ObservationState
    release_state: Literal["released", "preorder", "unknown"]
    price: Decimal | None
    price_status: ObservationState
    currency: str | None
    description: str
    preview_image_url: str | None
    alternative_titles: tuple[str, ...]
    diagnostics: tuple[str, ...]


class SearchResult(TypedDict):
    books: tuple[Book, ...]
    complete: bool
    diagnostics: tuple[str, ...]


class BookWalkerError(Exception):
    def __init__(self, reason: str, status: Status = "invalid_input", *, candidates: Sequence[str] = ()):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.candidates = tuple(candidates)


def _soup(content: bytes) -> Tag:
    if not content.strip():
        raise BookWalkerError("bookwalker_empty_body")
    try:
        from bs4 import BeautifulSoup
        import_module("lxml.etree")
    except ImportError:
        raise BookWalkerError("bookwalker_requires_integrations_extra", "missing_dependency") from None
    return BeautifulSoup(content, "lxml")


def _find(parent: Tag, name: str, attrs: dict[str, str]) -> Tag | None:
    from bs4 import Tag
    found = parent.find(name, attrs=attrs)
    return found if isinstance(found, Tag) else None


def _all(parent: Tag, name: str, attrs: dict[str, str] | None = None) -> tuple[Tag, ...]:
    from bs4 import Tag
    return tuple(item for item in parent.find_all(name, attrs=attrs or {}) if isinstance(item, Tag))


def _attribute(tag: Tag | None, name: str) -> str | None:
    if tag is None:
        return None
    value = tag.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _asset_url(value: str | None) -> str | None:
    if not value or "\\" in value or any(ord(character) < 33 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(urljoin(BOOKWALKER_ORIGIN, value))
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return None
    return urlunsplit(parsed._replace(fragment=""))


def _product_url(value: str | None) -> str:
    resolved = _asset_url(value)
    if resolved is None:
        raise BookWalkerError("bookwalker_invalid_product_url")
    parsed = urlsplit(resolved)
    try:
        port = parsed.port
    except ValueError:
        raise BookWalkerError("bookwalker_invalid_product_url") from None
    if parsed.scheme != "https" or parsed.hostname != "global.bookwalker.jp" or port not in (None, 443):
        raise BookWalkerError("bookwalker_cross_origin_product_url")
    return urlunsplit(parsed._replace(netloc="global.bookwalker.jp"))


def fetch_html(client: HttpClient, relative: str) -> bytes:
    """Perform one bounded read through the shared transport; never retry here."""
    from .http import HttpError
    try:
        response = client.request("GET", relative, read_only=True, expected_statuses=(200,))
    except HttpError as error:
        raise BookWalkerError(error.reason, error.status) from None
    try:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
            raise BookWalkerError("bookwalker_non_html_response")
        content = response.content
        if not content.strip():
            raise BookWalkerError("bookwalker_empty_body")
        return content
    finally:
        response.close()


def _tiles(soup: Tag) -> Tag:
    area = _find(soup, "div", {"class": "book-list-area book-result-area book-result-area-1"})
    lists = _all(area if area is not None else soup, "ul", {"class": "o-tile-list"})
    if not lists:
        raise BookWalkerError("bookwalker_search_dom_missing")
    if len(lists) != 1:
        raise BookWalkerError("bookwalker_search_dom_ambiguous", "ambiguous")
    if not _all(lists[0], "li", {"class": "o-tile"}) and (
        lists[0].get_text(strip=True) or lists[0].find(True) is not None
    ):
        raise BookWalkerError("bookwalker_search_dom_changed")
    return lists[0]


def parse_series_count(content: bytes) -> int:
    return len(_all(_tiles(_soup(content)), "li", {"class": "o-tile"}))


def _thumbnail(info: Tag) -> str | None:
    image = _find(info, "img", {})
    source_set = _attribute(image, "data-srcset") or _attribute(image, "srcset")
    if source_set:
        candidates = [candidate.strip().split() for candidate in source_set.split(",")]
        for candidate in candidates:
            if len(candidate) == 2 and candidate[1] == "2x":
                result = _asset_url(candidate[0])
                if result:
                    return result
        for candidate in candidates:
            if candidate and (result := _asset_url(candidate[0])):
                return result
    return _asset_url(_attribute(image, "data-src") or _attribute(image, "src"))


def parse_search_page(content: bytes) -> tuple[tuple[SearchItem, ...], int]:
    soup = _soup(content)
    tiles = _tiles(soup)
    rows = _all(tiles, "li", {"class": "o-tile"})
    page_count = 1
    pager = _find(soup, "div", {"class": "pager-area"})
    if pager is not None:
        pages: list[int] = []
        for entry in _all(pager, "li"):
            text = entry.get_text(strip=True)
            if text.isascii() and text.isdigit() and len(text) <= 6:
                pages.append(int(text))
        for link in _all(pager, "a"):
            href = _attribute(link, "href")
            if href:
                try:
                    query = urlsplit(href).query
                except ValueError:
                    raise BookWalkerError("bookwalker_invalid_pagination") from None
                for value in parse_qs(query).get("page", ()):
                    if value.isascii() and value.isdigit() and len(value) <= 6:
                        pages.append(int(value))
        if not pages or min(pages) < 1:
            raise BookWalkerError("bookwalker_invalid_pagination")
        page_count = max(pages)
    items: list[SearchItem] = []
    for row in rows:
        info = _find(row, "div", {"class": "o-tile-book-info"})
        if info is None:
            raise BookWalkerError("bookwalker_book_info_missing")
        title_tag = _find(info, "h2", {"class": "a-tile-ttl"})
        title = title_tag.get_text(" ", strip=True) if title_tag is not None else ""
        if not title:
            raise BookWalkerError("bookwalker_title_missing")
        thumb = _find(info, "a", {"class": "a-tile-thumb-img"})
        url = _product_url(_attribute(thumb, "href"))
        chapter = any(_find(info, "div", {"class": name}) is not None for name in ("a-tag-chapter", "a-tag-simulpub"))
        kinds = tuple(kind for tag, kind in (("a-tag-manga", "manga"), ("a-tag-light-novel", "novel"), ("a-tag-other", "other"))
                      if _find(info, "div", {"class": tag}) is not None)
        if len(kinds) != 1:
            raise BookWalkerError("bookwalker_book_type_missing" if not kinds else "bookwalker_book_type_ambiguous")
        items.append({"original_title": title, "url": url, "thumbnail": _thumbnail(info), "book_type": kinds[0], "chapter": chapter})
    return tuple(items), page_count


def _release_date(values: Sequence[str]) -> tuple[date | None, ObservationState]:
    dates: set[date] = set()
    invalid = False
    for value in values:
        english = tuple(_DATE_PATTERN.finditer(value))
        iso = tuple(_ISO_DATE_PATTERN.finditer(value))
        for match in english:
            try:
                dates.add(date(int(match[3]), _MONTHS[match[1][:3].lower()], int(match[2])))
            except ValueError:
                invalid = True
        for match in iso:
            try:
                dates.add(date(int(match[1]), int(match[2]), int(match[3])))
            except ValueError:
                invalid = True
        if value.strip() and not english and not iso and value.strip().lower() not in {"tba", "tbd", "unknown", "not available", "-"}:
            invalid = True
    if invalid:
        return None, "invalid"
    if len(dates) > 1:
        return None, "ambiguous"
    return (next(iter(dates)), "known") if dates else (None, "unknown")


def _price(soup: Tag) -> tuple[Decimal | None, ObservationState, str | None]:
    values: set[Decimal] = set()
    invalid = False
    for tag in (_all(soup, "meta", {"itemprop": "price"})
                + _all(soup, "span", {"itemprop": "price"})
                + _all(soup, "div", {"itemprop": "price"})):
        text = _attribute(tag, "content") or tag.get_text(" ", strip=True)
        match = regex.fullmatch(r"\s*(?:[A-Z]{3}\s*|[$€£¥]\s*)?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:[A-Z]{3})?\s*", text)
        if match is None:
            invalid = True
            continue
        try:
            values.add(Decimal(match[1].replace(",", "")))
        except InvalidOperation:
            invalid = True
    currencies = {
        value for tag in _all(soup, "meta", {"itemprop": "priceCurrency"})
        if (value := _attribute(tag, "content")) is not None
    }
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    if invalid or any(not regex.fullmatch(r"[A-Z]{3}", value) for value in currencies):
        return None, "invalid", currency
    if len(values) > 1 or len(currencies) > 1:
        return None, "ambiguous", None
    return (next(iter(values)), "known", currency) if values else (None, "unknown", currency)


def parse_detail_page(content: bytes) -> BookDetails:
    soup = _soup(content)
    inner = _find(soup, "div", {"class": "product-detail-inner"})
    table = _find(inner, "table", {"class": "product-detail"}) if inner is not None else None
    if table is None:
        raise BookWalkerError("bookwalker_detail_dom_missing")
    rows = _all(table, "tr")
    if not rows:
        raise BookWalkerError("bookwalker_detail_rows_missing")
    titles: list[str] = []
    date_values: list[str] = []
    for row in rows:
        header, detail = _find(row, "th", {}), _find(row, "td", {})
        if header is None or detail is None:
            raise BookWalkerError("bookwalker_detail_row_malformed")
        label = header.get_text(" ", strip=True).lower()
        text = detail.get_text(" ", strip=True)
        if label in {"series title", "alternative title"} and text:
            titles.append(text)
        if label in {"release date", "publication date", "date of release", "available since"}:
            date_values.append(text)
    released_on, date_status = _release_date(date_values)
    price, price_status, currency = _price(soup)
    preview = _asset_url(_attribute(_find(soup, "meta", {"property": "og:image"}), "content"))
    if preview is None or "ogp-mature" in preview:
        image_box = _find(soup, "div", {"class": "book-img"})
        preview = _asset_url(_attribute(_find(image_box, "img", {}) if image_box is not None else None, "src"))
    description_tag = _find(soup, "div", {"itemprop": "description"})
    paragraphs = _all(description_tag, "p") if description_tag is not None else ()
    description = "\n".join(
        paragraph.get_text(" ", strip=True) for paragraph in paragraphs
        if len(paragraphs) == 1 or "synopsis-lead" not in (paragraph.get("class") or ())
    )
    diagnostics = tuple(
        f"bookwalker_{field}_{state}" for field, state in (("date", date_status), ("price", price_status)) if state != "known"
    )
    return {"titles": tuple(dict.fromkeys(titles)), "date": released_on, "date_status": date_status,
            "price": price, "price_status": price_status, "currency": currency, "description": description,
            "preview_image_url": preview, "diagnostics": diagnostics}


def shortened_title(title: str) -> str:
    if not regex.search(r"(?:\s+-|:)\s+", title):
        return ""
    return regex.sub(r"(?:\s+-|:)\s+.*", "", title).strip()


@lru_cache(maxsize=4096)
def _volume_marker(keywords: tuple[str, ...]) -> regex.Pattern[str]:
    expression = "|".join(keywords) if keywords else r"(?!)"
    return regex.compile(rf"(?<![A-Za-z])(?:{expression})[._ -]*\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)*", regex.IGNORECASE)


def normalize_book_title(title: str, *, number_policy: NumberPolicy) -> tuple[str, ReleaseNumber, bool]:
    """Retain display Unicode and exact tokens while removing release markers."""
    premium = bool(_PREMIUM_PATTERN.search(title))
    parts = tuple(_PART_PATTERN.finditer(title))
    if len(parts) > 1 or (parts and (not parts[0][1].isdigit() or int(parts[0][1]) < 1)):
        raise BookWalkerError("bookwalker_unsupported_part_number")
    part = int(parts[0][1]) if parts else None
    without_part = remove_dual_space(remove_brackets(_PART_PATTERN.sub("", title))).strip(" ,")
    numeric_name = regex.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", without_part)
    number = parse_release_number(numeric_name, "volume", number_policy)
    marker = _volume_marker(number_policy.volume_keywords).search(without_part)
    if marker is not None:
        series = without_part[:marker.start()].strip(" ,-")
    else:
        trailing = _TRAILING_NUMBER.search(without_part)
        if trailing is not None:
            tokens = tuple(regex.sub(r"\s+", "", trailing[1]).split("-"))
            if len(tokens) == 1:
                number = ReleaseNumber.scalar(tokens[0])
            elif len(tokens) == 2 and Decimal(tokens[0]) <= Decimal(tokens[1]):
                number = ReleaseNumber.range(tokens[0], tokens[1])
            else:
                number = ReleaseNumber.explicit_set(tokens)
            series = without_part[:trailing.start()].strip(" ,-")
        else:
            series = without_part
            if number.kind == "missing" and not regex.search(r"\d|\b(?:chapter|extra|omake|special|bonus|side[- ]?story)\b", without_part, regex.IGNORECASE):
                number = ReleaseNumber.scalar("1")
    if number.kind == "scalar" and number.values[0] >= _VOLUME_YEAR_CUTOFF:
        number = ReleaseNumber.missing()
    if not series:
        raise BookWalkerError("bookwalker_series_title_missing")
    return series, ReleaseNumber(number.kind, number.values, number.raw_tokens, part), premium


def _matches(title: str, query: str, threshold: float, *, allow_shortened: bool) -> bool:
    titles = (title, shortened_title(title)) if allow_shortened else (title,)
    return any(similar(clean_str(candidate), clean_str(query)) >= threshold
               for candidate in titles if candidate and query)


def group_bookwalker_books(books: Sequence[Book], *, required_similarity_score: float) -> tuple[tuple[Book, ...], ...]:
    groups: list[list[Book]] = []
    seen: dict[str, Book] = {}
    for book in books:
        previous = seen.get(book["url"])
        if previous is not None:
            if previous != book:
                raise BookWalkerError("bookwalker_conflicting_product_observations", "review_required")
            continue
        seen[book["url"]] = book
        matches = [group for group in groups if group[0]["book_type"] == book["book_type"]
                   and _matches(book["title"], group[0]["title"], required_similarity_score, allow_shortened=True)]
        if len(matches) > 1:
            raise BookWalkerError("bookwalker_ambiguous_group", "ambiguous")
        if matches:
            matches[0].append(book)
        else:
            groups.append([book])
    return tuple(tuple(sorted(group, key=lambda book: (
        not bool(book["number"].values), min(book["number"].values, default=Decimal(0)),
        book["number"].part or 0, book["url"],
    ))) for group in groups)


def search_bookwalker(
    client: HttpClient, query: str, book_type: str, *, number_policy: NumberPolicy,
    required_similarity_score: float, max_pages: int = 5, request_interval_seconds: float = 2,
    clock: Callable[[], date] = date.today, cancel: Event | None = None,
    alternative_queries: Sequence[str] = (),
) -> SearchResult:
    """Search categories and aliases without mutating policy or sending messages."""
    if not query.strip() or book_type not in _CATEGORIES:
        raise BookWalkerError("bookwalker_invalid_search")
    if (not 0 <= required_similarity_score <= 1 or type(max_pages) is not int or max_pages < 1
            or not math.isfinite(request_interval_seconds) or request_interval_seconds < 0):
        raise BookWalkerError("bookwalker_invalid_search_policy")
    today = clock()
    fetched = False
    details: dict[str, BookDetails] = {}
    complete = True
    diagnostics: list[str] = []

    def fetch(relative: str) -> bytes:
        nonlocal fetched
        if cancel is not None and cancel.is_set():
            raise BookWalkerError("bookwalker_cancelled", "cancelled")
        if fetched:
            if cancel is not None:
                if cancel.wait(request_interval_seconds):
                    raise BookWalkerError("bookwalker_cancelled", "cancelled")
            else:
                time.sleep(request_interval_seconds)
        fetched = True
        return fetch_html(client, relative)

    def search_variant(search: str, category: int, *, shortened: bool = False) -> tuple[Book, ...]:
        nonlocal complete
        count = parse_series_count(fetch("search/?" + urlencode({"word": search, "np": 0})))
        if shortened and count != 1:
            diagnostics.append("bookwalker_shortened_search_not_unique")
            return ()
        threshold = max(0.0, required_similarity_score - _SINGLE_SERIES_RELAXATION) if count == 1 else required_similarity_score
        books: list[Book] = []
        page, total_pages = 1, 1
        while page <= min(total_pages, max_pages):
            items, total_pages = parse_search_page(fetch("search/?" + urlencode({"word": search, "page": page, "qcat": category})))
            if total_pages > max_pages:
                complete = False
                if "bookwalker_page_limit" not in diagnostics:
                    diagnostics.append("bookwalker_page_limit")
            for item in items:
                if item["chapter"] or item["book_type"] != book_type:
                    continue
                raw_title = item["original_title"]
                if (regex.search(r"chapter|\s#\d+\b", raw_title, regex.IGNORECASE)
                        and not regex.search(r"re[-_. :]*zero", raw_title, regex.IGNORECASE)):
                    continue
                title, number, premium = normalize_book_title(raw_title, number_policy=number_policy)
                detail = details.get(item["url"])
                if detail is None:
                    parsed = urlsplit(item["url"])
                    relative = urlunsplit(("", "", parsed.path.lstrip("/"), parsed.query, ""))
                    detail = parse_detail_page(fetch(relative))
                    details[item["url"]] = detail
                names = detail["titles"] or (title,)
                if not any(_matches(name, search, threshold, allow_shortened=count == 1) for name in names):
                    continue
                state: Literal["released", "preorder", "unknown"] = "unknown"
                if detail["date"] is not None:
                    state = "released" if detail["date"] <= today else "preorder"
                books.append({"title": title, "original_title": raw_title, "number": number, "premium": premium,
                              "book_type": item["book_type"], "url": item["url"], "thumbnail": item["thumbnail"],
                              "date": detail["date"], "date_status": detail["date_status"], "release_state": state,
                              "price": detail["price"], "price_status": detail["price_status"], "currency": detail["currency"],
                              "description": detail["description"], "preview_image_url": detail["preview_image_url"],
                              "alternative_titles": detail["titles"], "diagnostics": detail["diagnostics"]})
            page += 1
        return tuple(books)

    books: list[Book] = []
    queries = tuple(dict.fromkeys((query, *alternative_queries)))
    for search in queries:
        found = search_variant(search, _CATEGORIES[book_type])
        if not found and book_type == "manga":
            found = search_variant(search, _INTERNATIONAL_MANGA_CATEGORY)
        books.extend(found)
        shortened = shortened_title(search)
        if shortened:
            short_found = search_variant(shortened, _CATEGORIES[book_type], shortened=True)
            if not short_found and book_type == "manga":
                short_found = search_variant(shortened, _INTERNATIONAL_MANGA_CATEGORY, shortened=True)
            books.extend(short_found)
    groups = group_bookwalker_books(books, required_similarity_score=required_similarity_score)
    if len(groups) > 1:
        raise BookWalkerError("bookwalker_multiple_series", "ambiguous", candidates=tuple(group[0]["title"] for group in groups))
    result_books = groups[0] if groups else ()
    diagnostics.extend(reason for book in result_books for reason in book["diagnostics"])
    return {"books": result_books, "complete": complete, "diagnostics": tuple(dict.fromkeys(diagnostics))}

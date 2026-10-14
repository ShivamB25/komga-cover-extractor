"""Read-only BookWalker checks over already observed library releases."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from unidecode import unidecode

from ..domain.decisions import Decision, RunAuthorization, RunResult
from ..domain.releases import ReleaseNumber, ReleaseSnapshot
from ..integrations.bookwalker import Book, BookWalkerError, search_bookwalker
from ..parsing.normalization import normalize_str
from ..parsing.numbers import NumberPolicy

if TYPE_CHECKING:
    from ..integrations.http import HttpClient

_FOLDER_TYPE_PERCENTAGE = 70
_MANGA_EXTENSIONS = frozenset((".cbz", ".zip"))
_NOVEL_EXTENSIONS = frozenset((".epub",))


def serialize_book(book: Book) -> dict[str, object]:
    number = book["number"]
    serialized_number = number.to_dict()
    return {
        "title": book["title"], "original_title": book["original_title"], "number": serialized_number,
        "volume_number": "-".join(number.raw_tokens) if number.kind != "set" else ", ".join(number.raw_tokens),
        "part": serialized_number["part"], "premium": book["premium"], "book_type": book["book_type"],
        "url": book["url"], "thumbnail": book["thumbnail"],
        "date": book["date"].isoformat() if book["date"] is not None else None,
        "date_status": book["date_status"], "release_state": book["release_state"],
        "price": str(book["price"]) if book["price"] is not None else None,
        "price_status": book["price_status"], "currency": book["currency"],
        "description": book["description"], "preview_image_url": book["preview_image_url"],
        "alternative_titles": list(book["alternative_titles"]), "diagnostics": list(book["diagnostics"]),
    }


def _eligible_local(release: ReleaseSnapshot, book: Book, book_type: str) -> bool:
    if book["premium"] and not release.is_premium:
        return False
    return release.number.part == book["number"].part or (
        book_type == "novel" and release.number.part is None and book["number"].part is not None
    )


def _coverage(local: Sequence[ReleaseSnapshot], book: Book, book_type: str, max_expansion: int) -> tuple[str, str, tuple[str, ...]]:
    remote = book["number"]
    if remote.kind == "missing":
        return "review_required", "unknown_remote_number", ()
    eligible = tuple(release for release in local if release.number.kind != "missing" and _eligible_local(release, book, book_type))
    unparted = ReleaseNumber(remote.kind, remote.values, remote.raw_tokens)
    for release in eligible:
        observed = ReleaseNumber(release.number.kind, release.number.values, release.number.raw_tokens)
        if observed.covers(unparted):
            if release.number.part != remote.part:
                reason = "complete_volume_covers_part"
            elif observed.kind == remote.kind and observed.values == remote.values:
                reason = "exact_match"
            else:
                reason = "covered_range" if observed.kind == "range" else "covered_set"
            return "existing", reason, (str(release.path),)
    try:
        members = remote.expand(max_expansion=max_expansion)
    except ValueError:
        reason = "fractional_range" if remote.kind == "range" and any(value != value.to_integral_value() for value in remote.values) else "range_limit"
        return "review_required", reason, ()
    covered = tuple(value for value in members if any(release.number.contains(value) for release in eligible))
    if len(covered) == len(members):
        return "existing", "covered_by_members", tuple(str(release.path) for release in eligible if any(release.number.contains(value) for value in members))
    if any(release.number.kind == "missing" for release in local):
        return "review_required", "unknown_local_number", ()
    if book["premium"] and any(release.number.contains(value) for release in local for value in members):
        return "missing", "premium_edition_available", ()
    if remote.part is None and any(release.number.part is not None and release.number.contains(value) for release in local for value in members):
        return "missing", "complete_volume_available", ()
    return "missing", "incomplete_collection" if covered else "new_release", ()


def compare_bookwalker_releases(
    local: Sequence[ReleaseSnapshot], books: Sequence[Book], *, book_type: str,
    max_expansion: int = 10000,
) -> tuple[dict[str, object], ...]:
    """Compare exact domains and parts; partial ownership never hides a collection."""
    if book_type not in {"manga", "novel"} or type(max_expansion) is not int or max_expansion < 1:
        raise ValueError("invalid BookWalker comparison policy")
    comparisons: list[dict[str, object]] = []
    for book in books:
        status, reason, paths = _coverage(local, book, book_type, max_expansion)
        if status == "missing" and book["date_status"] != "known":
            status, reason = "review_required", f"date_{book['date_status']}"
        comparisons.append({"status": status, "reason": reason, "local_paths": list(paths), "book": serialize_book(book)})
    return tuple(comparisons)


def _folder_type(volumes: Sequence[ReleaseSnapshot]) -> str | None:
    if not volumes:
        return None
    manga = sum(release.path.suffix.lower() in _MANGA_EXTENSIONS for release in volumes)
    novels = sum(release.path.suffix.lower() in _NOVEL_EXTENSIONS for release in volumes)
    if manga * 100 >= _FOLDER_TYPE_PERCENTAGE * len(volumes):
        return "manga"
    if novels * 100 >= _FOLDER_TYPE_PERCENTAGE * len(volumes):
        return "novel"
    return None


def check_bookwalker_releases(
    releases: Sequence[ReleaseSnapshot], *, client: HttpClient | None,
    number_policy: NumberPolicy, required_similarity_score: float,
    authorization: RunAuthorization, clock: Callable[[], date] = date.today,
    max_pages: int = 5, request_interval_seconds: float = 2,
    cancel: Event | None = None,
) -> RunResult:
    """Return JSON-safe reports for the parent's CLI and durable delivery owner.

    GET observations need no mutation permission. This entry does not send,
    persist, scan or alter local files. ``authorization`` is recorded only as
    the caller's current delivery permission, never as executable plan state.
    """
    folders: dict[Path, dict[Path, ReleaseSnapshot]] = {}
    for release in releases:
        if release.kind != "volume" or release.observation.root_role in {"download", "both"}:
            continue
        folders.setdefault(release.parent, {})[release.path] = release
    if not folders:
        return RunResult((Decision("no_match", "bookwalker_no_library_volumes"),), releases=tuple(releases))
    if client is None:
        return RunResult((Decision("missing_dependency", "bookwalker_client_unavailable"),), releases=tuple(releases))
    today = clock()
    decisions: list[Decision] = []
    reports: list[dict[str, object]] = []
    for parent, observed in sorted(folders.items()):
        if cancel is not None and cancel.is_set():
            decisions.append(Decision("cancelled", "bookwalker_cancelled", parent))
            break
        volumes = tuple(observed.values())
        book_type = _folder_type(volumes)
        if book_type is None:
            decisions.append(Decision("review_required", "bookwalker_mixed_folder_type", parent))
            continue
        query = unidecode(normalize_str(
            parent.name, skip_common_words=True, skip_japanese_particles=True,
            skip_misc_words=True, skip_editions=True,
        )).strip()
        if not query:
            decisions.append(Decision("invalid_input", "bookwalker_empty_series_query", parent))
            continue
        alternatives = tuple(dict.fromkeys(
            value for release in volumes for key, value in release.observation.metadata
            if key.lower() in {"alternative title", "alternative_title"} and value.strip() and value != query
        ))
        try:
            search = search_bookwalker(
                client, query, book_type, number_policy=number_policy,
                required_similarity_score=required_similarity_score, max_pages=max_pages,
                request_interval_seconds=request_interval_seconds, clock=lambda: today,
                cancel=cancel, alternative_queries=alternatives,
            )
            comparisons = compare_bookwalker_releases(volumes, search["books"], book_type=book_type)
        except BookWalkerError as error:
            decisions.append(Decision(error.status, error.reason, parent, error.candidates))
            if error.status == "cancelled":
                break
            continue
        if not search["complete"]:
            decisions.append(Decision("review_required", "bookwalker_page_limit", parent))
        if not comparisons:
            decisions.append(Decision("no_match", "bookwalker_no_matching_releases", parent))
        for comparison in comparisons:
            book = comparison["book"]
            assert isinstance(book, dict)
            candidates = (str(book["url"]),)
            reason = str(comparison["reason"])
            if comparison["status"] == "existing":
                decisions.append(Decision("skipped", f"bookwalker_{reason}", parent, candidates))
            elif comparison["status"] == "review_required":
                decisions.append(Decision("review_required", f"bookwalker_{reason}", parent, candidates))
            else:
                decisions.append(Decision("observed", f"bookwalker_{reason}", parent, candidates))
        reports.append({
            "source": "bookwalker", "path": str(parent), "series": parent.name, "query": query,
            "book_type": book_type, "complete": search["complete"],
            "delivery_authorized": authorization.allow_remote,
            "diagnostics": list(search["diagnostics"]), "comparisons": list(comparisons),
        })
    return RunResult(tuple(decisions), releases=tuple(releases), missing_releases=tuple(reports),
                     skipped=sum(decision.status == "skipped" for decision in decisions),
                     failed=sum(decision.status in {"failed", "permission_denied", "publication_failed"} for decision in decisions))


def bookwalker_notifications(reports: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """Create channel-ID items; the owning delivery service formats and sends them."""
    released: list[dict[str, object]] = []
    preorders: list[dict[str, object]] = []
    seen: set[str] = set()
    for report in reports:
        if report.get("source") != "bookwalker" or report.get("complete") is not True:
            continue
        comparisons = report.get("comparisons")
        if not isinstance(comparisons, (tuple, list)):
            continue
        for comparison in comparisons:
            if not isinstance(comparison, dict) or comparison.get("status") != "missing":
                continue
            book = comparison.get("book")
            if not isinstance(book, dict) or book.get("date_status") != "known":
                continue
            url = book.get("url")
            state = book.get("release_state")
            if not isinstance(url, str) or url in seen or state not in {"released", "preorder"}:
                continue
            seen.add(url)
            notification: dict[str, object] = {"channel": "bookwalker_released" if state == "released" else "bookwalker_preorder", "item": dict(book)}
            (released if state == "released" else preorders).append(notification)
    def release_date(notification: dict[str, object]) -> str:
        item = notification["item"]
        assert isinstance(item, dict)
        return str(item["date"])
    released.sort(key=release_date)
    preorders.sort(key=release_date, reverse=True)
    return (*released, *preorders)

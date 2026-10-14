from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from komga_cover_extractor.domain.releases import (
    FileObservation,
    ReleaseNumber,
    ReleaseSnapshot,
)
from komga_cover_extractor.integrations import discord
from komga_cover_extractor.integrations.discord import (
    batch_discord_payloads,
    format_bookwalker_notification,
    format_embed,
    format_fields,
    format_release_notification,
    group_chapter_notifications,
    prepare_discord_payload,
)
from komga_cover_extractor.integrations.http import HttpError


def release(name: str, number: ReleaseNumber, *, kind: str = "chapter", series: str = "Series") -> ReleaseSnapshot:
    return ReleaseSnapshot(FileObservation(Path("/library") / series / name), number=number, kind=kind,
                           display_series=series, canonical_series_key=series.lower())


def test_field_truncation_keeps_code_fences_and_never_mutates_input() -> None:
    fields = [
        {"name": "n" * 300, "value": "```" + "v" * 2000 + "```", "inline": True},
        *[{"name": f"Field {index}", "value": "value", "inline": False} for index in range(25)],
    ]
    before = deepcopy(fields)
    actual = format_fields(fields)
    assert actual[0] == {"name": "n" * 253 + "...", "value": "```" + "v" * 1015 + "...```", "inline": True}
    assert [item["name"] for item in actual[1:]] == [f"Field {index}" for index in range(24)]
    assert fields == before


def test_volume_notification_keeps_part_twelve_out_of_decimal_number() -> None:
    item = release("Series v01 Part 12_extra.cbz", ReleaseNumber.scalar("1", part=12), kind="volume")
    assert format_release_notification(item) == {
        "title": "New Volume(s) Added", "color": 65280,
        "fields": [
            {"name": "Volume Number(s)", "value": "```1```", "inline": False},
            {"name": "Volume Part", "value": "```12```", "inline": False},
            {"name": "Volume Name(s)", "value": "```Series v01 Part 12_extra.cbz```", "inline": False},
        ],
    }


def test_chapters_are_grouped_by_display_series_and_ordered_by_exact_number() -> None:
    items = (
        release("Series c003.cbz", ReleaseNumber.scalar("3")),
        release("Other c002.cbz", ReleaseNumber.scalar("2"), series="Other"),
        release("Series c001.cbz", ReleaseNumber.scalar("1")),
        release("Series c000.cbz", ReleaseNumber.scalar("000")),
        release("Series c002.01.cbz", ReleaseNumber.scalar("2.01")),
    )
    actual = group_chapter_notifications(items)
    assert actual == (
        {"title": "New Chapter(s) Added", "color": 65280, "fields": [
            {"name": "Series Name", "value": "```Series```", "inline": False},
            {"name": "Chapter Number(s)", "value": "```0-1, 2.01, 3```", "inline": False},
            {"name": "Chapter Name(s)", "value": "```Series c000.cbz\nSeries c001.cbz\nSeries c002.01.cbz\nSeries c003.cbz```", "inline": False},
        ]},
        {"title": "New Chapter(s) Added", "color": 65280, "fields": [
            {"name": "Series Name", "value": "```Other```", "inline": False},
            {"name": "Chapter Number(s)", "value": "```2```", "inline": False},
            {"name": "Chapter Name(s)", "value": "```Other c002.cbz```", "inline": False},
        ]},
    )
    individual = group_chapter_notifications(items[:1] + items[2:], include_covers=True)
    assert [embed["fields"][1]["value"] for embed in individual] == ["```Series c000.cbz```", "```Series c001.cbz```", "```Series c002.01.cbz```", "```Series c003.cbz```"]


def test_contiguous_range_and_explicit_set_have_different_notification_membership() -> None:
    contiguous = group_chapter_notifications((release("Series c001-003.cbz", ReleaseNumber.range("1", "3")),))
    explicit = group_chapter_notifications((release("Series c001-003-extra.cbz", ReleaseNumber.explicit_set(("1", "3"))),))
    assert contiguous[0]["fields"][1]["value"] == "```1-3```"
    assert explicit[0]["fields"][1]["value"] == "```1, 3```"
    decimal_range = group_chapter_notifications((release("Series c1.01-1.10.cbz", ReleaseNumber.range("1.01", "1.10")),))
    assert decimal_range[0]["fields"][1]["value"] == "```1.01-1.10```"


def test_notification_range_limit_is_not_a_truncated_success() -> None:
    at_limit = group_chapter_notifications((release("Series c1-10000.cbz", ReleaseNumber.range("1", "10000")),))
    assert at_limit[0]["fields"][1]["value"] == "```1-10000```"
    with pytest.raises(HttpError) as caught:
        group_chapter_notifications((release("Series c1-10001.cbz", ReleaseNumber.range("1", "10001")),))
    assert (caught.value.status, caught.value.reason) == ("review_required", "notification_range_limit")


def test_batches_split_at_embed_count_and_total_text_without_dropping_messages() -> None:
    small = [format_embed(title=title) for title in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10")]
    batches = batch_discord_payloads(small)
    assert [[embed["title"] for embed in batch["embeds"]] for batch in batches] == [["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"], ["10"]]
    large = batch_discord_payloads((format_embed(description="a" * 3000), format_embed(description="b" * 3000), format_embed(description="c")))
    assert [[embed["description"] for embed in batch["embeds"]] for batch in large] == [["a" * 3000, "b" * 3000], ["c"]]
    text = batch_discord_payloads((), message="a" * 2000 + "remaining")
    assert text == ({"allowed_mentions": {"parse": []}, "content": "a" * 2000}, {"allowed_mentions": {"parse": []}, "content": "remaining"})


def test_single_oversized_embed_is_rejected_before_transport() -> None:
    too_large = format_embed(description="d" * 4096, fields=[{"name": f"F{index}", "value": "x" * 1024} for index in range(2)])
    with pytest.raises(HttpError) as caught:
        batch_discord_payloads((too_large,))
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "discord_embed_text_limit")
    with pytest.raises(HttpError) as caught:
        prepare_discord_payload(embeds=tuple(format_embed(title="book") for _ in range(11)))
    assert caught.value.reason == "discord_batch_limit"


def test_prepared_embeds_preserve_injected_timestamp_and_version_without_shared_mutation() -> None:
    source = format_embed(title="Committed")
    payload, files = prepare_discord_payload(embeds=(source,), version="v2.5.39", timestamp=datetime(2026, 10, 13, 12, 30, tzinfo=UTC))
    assert payload == {"allowed_mentions": {"parse": []}, "embeds": [{"title": "Committed", "footer": {"text": "v2.5.39"}, "timestamp": "2026-10-13T12:30:00+00:00"}]}
    assert files == () and source == {"title": "Committed"}
    source["title"] = "Later change"
    assert payload["embeds"][0]["title"] == "Committed"


def test_external_image_precedence_and_attachment_limit_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\nX"
    monkeypatch.setattr(discord, "DISCORD_UPLOAD_LIMIT", len(png))
    payload, files = prepare_discord_payload(embeds=(format_embed(title="Book"),), attachments=(png,))
    assert payload["embeds"][0]["image"] == {"url": "attachment://cover.png"}
    assert files == (("cover.png", png, "image/png"),)
    with pytest.raises(HttpError) as caught:
        prepare_discord_payload(embeds=(format_embed(title="Book"),), attachments=(png + b"X",))
    assert caught.value.reason == "discord_attachment_limit"
    external, files = prepare_discord_payload(embeds=(format_embed(title="Book"),), image_url="https://images.invalid/cover.png", attachments=(png,))
    assert external["embeds"][0]["image"] == {"url": "https://images.invalid/cover.png"} and files == ()


def test_bookwalker_released_and_preorder_embeds_keep_fields_parts_and_unknown_price() -> None:
    item = {"title": "Future Story", "volume_number": "1", "part": 12, "premium": True, "book_type": "Light Novel",
            "date": "2026-10-20", "price": None, "description": "Café story", "url": "https://bookwalker.invalid/de001/",
            "preview_image_url": "https://images.invalid/cover.jpg"}
    actual = format_bookwalker_notification(item, preorder=True)
    assert actual == {
        "title": "Future Story Volume 1 Part 12 [Premium]", "color": 5919485,
        "fields": [{"name": "Type", "value": "Light Novel", "inline": False}, {"name": "Release Date", "value": "2026-10-20", "inline": False},
                   {"name": "Description", "value": "Cafe story", "inline": False}],
        "url": "https://bookwalker.invalid/de001/", "image": {"url": "https://images.invalid/cover.jpg"},
        "thumbnail": {"url": "https://images.invalid/cover.jpg"},
        "author": {"name": "Bookwalker", "url": "https://bookwalker.invalid/de001/",
                   "icon_url": "https://play-lh.googleusercontent.com/a7jUyjTxWrl_Kl1FkUSv2FHsSu3Swucpem2UIFDRbA1fmt5ywKBf-gcwe6_zalOqIR7V=w240-h480-rw"},
    }
    assert "0.00" not in str(actual)
    assert format_bookwalker_notification(item)["color"] == 8421504


def test_notification_text_redacts_webhooks_and_userinfo_without_recursive_sending() -> None:
    actual, _ = prepare_discord_payload("Failed https://discord.com/api/webhooks/123/fictional-token and https://reader:fictional-password@service.invalid/library")
    assert actual == {"allowed_mentions": {"parse": []}, "content": "Failed [redacted webhook] and https://[redacted]@service.invalid/library"}
    with pytest.raises(HttpError) as caught:
        format_embed(title="Bad image", url="https://reader:fictional-password@service.invalid/book")
    assert (caught.value.status, caught.value.reason) == ("invalid_input", "invalid_discord_url")
    assert "fictional-password" not in repr(caught.value)

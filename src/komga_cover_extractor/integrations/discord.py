"""Pure notification formatting and a scoped Discord delivery/rotation owner."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.decisions import Decision, RunAuthorization
from ..domain.releases import ReleaseNumber, ReleaseSnapshot
from .http import Cancel, HttpClient, HttpError

DISCORD_EMBED_LIMIT = 10
DISCORD_FIELD_LIMIT = 25
DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_FIELD_NAME_LIMIT = 256
DISCORD_FIELD_VALUE_LIMIT = 1024
DISCORD_FOOTER_LIMIT = 2048
DISCORD_AUTHOR_LIMIT = 256
DISCORD_EMBED_TEXT_LIMIT = 6000
DISCORD_CONTENT_LIMIT = 2000
DISCORD_UPLOAD_LIMIT = 20 * 1024 * 1024
GREEN_COLOR = 65280
GREY_COLOR = 8421504
PREORDER_BLUE_COLOR = 5919485
BOOKWALKER_LOGO_URL = "https://play-lh.googleusercontent.com/a7jUyjTxWrl_Kl1FkUSv2FHsSu3Swucpem2UIFDRbA1fmt5ywKBf-gcwe6_zalOqIR7V=w240-h480-rw"
_CHANNEL_KEY = re.compile(r"discord/(?:normal/[0-9]+|new-release|bookwalker/(?:released|preorder))\Z")
_WEBHOOK_TEXT = re.compile(r"https?://[^\s<>]*?/api(?:/v[0-9]+)?/webhooks/[^\s<>]+", re.IGNORECASE)
_USERINFO_TEXT = re.compile(r"(https?://)[^\s/@]+:[^\s/@]*@", re.IGNORECASE)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise HttpError("invalid_input", "invalid_discord_text")
    value = _WEBHOOK_TEXT.sub("[redacted webhook]", value)
    value = _USERINFO_TEXT.sub(r"\1[redacted]@", value)
    return "".join(char for char in value if ord(char) >= 32 or char in "\n\t")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:-3][:limit - 6] + "...```" if value.endswith("```") else value[:limit - 3] + "..."


def _display_url(value: object, *, attachment: bool = False) -> str:
    text = _text(value)
    if text != value or any(ord(char) < 33 for char in text) or "\\" in text:
        raise HttpError("invalid_input", "invalid_discord_url")
    invalid = False
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        invalid = True
        parts = urlsplit("")
        port = None
    if (attachment and parts.scheme == "attachment" and not parts.path and not parts.query and not parts.fragment
            and re.fullmatch(r"cover(?:_[1-9][0-9]*)?\.(?:jpg|png|webp|gif)", parts.netloc)):
        return text
    if (invalid or parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or port is not None and not 1 <= port <= 65535):
        raise HttpError("invalid_input", "invalid_discord_url")
    return text


def format_fields(fields: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for field in fields[:DISCORD_FIELD_LIMIT]:
        name, value = _text(field.get("name")), _text(field.get("value"))
        inline = field.get("inline", False)
        if not name.strip() or not value.strip() or type(inline) is not bool:
            raise HttpError("invalid_input", "invalid_discord_field")
        result.append({"name": _truncate(name, DISCORD_FIELD_NAME_LIMIT),
                       "value": _truncate(value, DISCORD_FIELD_VALUE_LIMIT), "inline": inline})
    return result


def format_embed(
    *, title: str = "", color: int | None = None, fields: Sequence[Mapping[str, object]] = (),
    description: str | None = None, url: str | None = None,
) -> dict[str, object]:
    embed: dict[str, object] = {}
    if title:
        embed["title"] = _truncate(_text(title), DISCORD_TITLE_LIMIT)
    if color is not None:
        if type(color) is not int or not 0 <= color <= 0xFFFFFF:
            raise HttpError("invalid_input", "invalid_discord_color")
        embed["color"] = color
    if fields:
        embed["fields"] = format_fields(fields)
    if description:
        embed["description"] = _truncate(_text(description), DISCORD_DESCRIPTION_LIMIT)
    if url:
        embed["url"] = _display_url(url)
    return embed


def _number_text(number: ReleaseNumber) -> str:
    if number.kind == "missing":
        return "Unknown"
    if number.kind == "range":
        return "-".join(str(value) for value in number.values)
    return ", ".join(str(value) for value in number.values)


def format_release_notification(release: ReleaseSnapshot) -> dict[str, object]:
    label = release.kind.capitalize()
    fields: list[dict[str, object]] = [
        {"name": f"{label} Number(s)", "value": f"```{_number_text(release.number)}```", "inline": False},
        {"name": f"{label} Name(s)", "value": f"```{release.name}```", "inline": False},
    ]
    if release.kind == "volume" and release.number.part is not None:
        fields.insert(1, {"name": "Volume Part", "value": f"```{release.number.part}```", "inline": False})
    return format_embed(title=f"New {label}(s) Added", color=GREEN_COLOR, fields=fields)


def _release_order(release: ReleaseSnapshot) -> tuple[bool, Decimal, int, str]:
    return not bool(release.number.values), min(release.number.values, default=Decimal(0)), release.number.part or 0, release.name


def _abbreviate_numbers(numbers: Sequence[Decimal]) -> str:
    runs: list[list[Decimal]] = []
    for number in sorted(set(numbers)):
        if (runs and number == number.to_integral_value()
                and runs[-1][-1] == runs[-1][-1].to_integral_value()
                and number == runs[-1][-1] + 1):
            runs[-1].append(number)
        else:
            runs.append([number])
    return ", ".join(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]) for run in runs)


def group_chapter_notifications(
    releases: Sequence[ReleaseSnapshot], *, include_covers: bool = False,
) -> tuple[dict[str, object], ...]:
    groups: dict[str, list[ReleaseSnapshot]] = {}
    for release in releases:
        if release.kind != "chapter":
            raise HttpError("invalid_input", "chapter_notification_requires_chapters")
        groups.setdefault(release.display_series, []).append(release)
    embeds: list[dict[str, object]] = []
    for series, group in groups.items():
        ordered = sorted(group, key=_release_order)
        if include_covers:
            embeds.extend(format_release_notification(release) for release in ordered)
            continue
        numbers: list[Decimal] = []
        spans: list[str] = []
        unknown = False
        limited = False
        for release in ordered:
            if release.number.kind == "missing":
                unknown = True
                continue
            if release.number.kind == "range" and any(value != value.to_integral_value() for value in release.number.values):
                spans.append(_number_text(release.number))
                continue
            try:
                numbers.extend(release.number.expand(max_expansion=10_000 - len(numbers)))
            except ValueError:
                limited = True
                break
        if limited or len(numbers) > 10_000:
            raise HttpError("review_required", "notification_range_limit")
        summary = _abbreviate_numbers(numbers)
        summary = ", ".join(filter(None, (summary, *spans)))
        if unknown:
            summary = f"{summary}, Unknown" if summary else "Unknown"
        fields = [
            {"name": "Series Name", "value": f"```{series}```", "inline": False},
            {"name": "Chapter Number(s)", "value": f"```{summary}```", "inline": False},
            {"name": "Chapter Name(s)", "value": "```" + "\n".join(release.name for release in ordered) + "```", "inline": False},
        ]
        embeds.append(format_embed(title="New Chapter(s) Added", color=GREEN_COLOR, fields=fields))
    return tuple(embeds)


def format_bookwalker_notification(item: Mapping[str, object], *, preorder: bool = False) -> dict[str, object]:
    from unidecode import unidecode

    title, number = _text(item.get("title")), _text(item.get("volume_number"))
    heading = f"{title} Volume {number}"
    if item.get("part") is not None:
        heading += f" Part {item['part']}"
    if item.get("premium") and "premium" not in heading.lower():
        heading += " [Premium]"
    date = item.get("date")
    fields: list[dict[str, object]] = [
        {"name": "Type", "value": _text(item.get("book_type")), "inline": False},
        {"name": "Release Date", "value": _text(date) if date is not None else "Unknown", "inline": False},
    ]
    if item.get("description"):
        fields.append({"name": "Description", "value": unidecode(_text(item["description"])), "inline": False})
    url = _display_url(item.get("url"))
    embed = format_embed(title=heading, color=PREORDER_BLUE_COLOR if preorder else GREY_COLOR, fields=fields, url=url)
    if item.get("preview_image_url"):
        image = {"url": _display_url(item["preview_image_url"])}
        embed["image"], embed["thumbnail"] = dict(image), dict(image)
    embed["author"] = {"name": "Bookwalker", "url": url, "icon_url": BOOKWALKER_LOGO_URL}
    return embed


def _copy_embed(source: Mapping[str, object], *, version: str = "", timestamp: datetime | None = None) -> dict[str, object]:
    if set(source) - {"title", "description", "color", "fields", "url", "image", "thumbnail", "author", "footer", "timestamp"}:
        raise HttpError("invalid_input", "invalid_discord_embed")
    title = _text(source.get("title", ""))
    description = _text(source["description"]) if source.get("description") is not None else None
    color = source.get("color")
    if color is not None and type(color) is not int:
        raise HttpError("invalid_input", "invalid_discord_color")
    raw_fields = source.get("fields", [])
    if not isinstance(raw_fields, (list, tuple)) or any(not isinstance(field, Mapping) for field in raw_fields):
        raise HttpError("invalid_input", "invalid_discord_field")
    embed = format_embed(title=title, description=description, color=color, fields=raw_fields,
                         url=_text(source["url"]) if source.get("url") else None)
    for key in ("image", "thumbnail"):
        if key in source:
            value = source[key]
            if not isinstance(value, Mapping) or set(value) != {"url"}:
                raise HttpError("invalid_input", "invalid_discord_image")
            embed[key] = {"url": _display_url(value["url"], attachment=True)}
    for key, text_key, limit in (("footer", "text", DISCORD_FOOTER_LIMIT), ("author", "name", DISCORD_AUTHOR_LIMIT)):
        value = source.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping) or text_key not in value or set(value) - {text_key, "url", "icon_url"}:
            raise HttpError("invalid_input", "invalid_discord_embed")
        block = {text_key: _truncate(_text(value[text_key]), limit)}
        for url_key in ("url", "icon_url"):
            if value.get(url_key):
                block[url_key] = _display_url(value[url_key])
        embed[key] = block
    if version:
        embed["footer"] = {"text": _truncate(_text(version), DISCORD_FOOTER_LIMIT)}
    stamp = source.get("timestamp")
    if stamp:
        embed["timestamp"] = _text(stamp)
    elif timestamp is not None:
        if timestamp.tzinfo is None:
            raise HttpError("invalid_input", "invalid_discord_timestamp")
        embed["timestamp"] = timestamp.isoformat()
    if not any(key in embed for key in ("title", "description", "fields", "image", "thumbnail")):
        raise HttpError("invalid_input", "empty_discord_embed")
    if _embed_length(embed) > DISCORD_EMBED_TEXT_LIMIT:
        raise HttpError("invalid_input", "discord_embed_text_limit")
    return embed


def _embed_length(embed: Mapping[str, object]) -> int:
    total = sum(len(str(embed.get(key, ""))) for key in ("title", "description"))
    fields = embed.get("fields", [])
    if isinstance(fields, list):
        total += sum(len(field["name"]) + len(field["value"]) for field in fields)
    for key, text_key in (("footer", "text"), ("author", "name")):
        value = embed.get(key)
        if isinstance(value, dict):
            total += len(value[text_key])
    return total


def _image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    raise HttpError("invalid_input", "invalid_discord_attachment")


def prepare_discord_payload(
    message: str | None = None, embeds: Sequence[Mapping[str, object]] = (), *, version: str = "",
    timestamp: datetime | None = None, image_url: str | None = None,
    attachments: Sequence[bytes | None] = (),
) -> tuple[dict[str, object], tuple[tuple[str, bytes, str], ...]]:
    if len(embeds) > DISCORD_EMBED_LIMIT or attachments and len(attachments) != len(embeds):
        raise HttpError("invalid_input", "discord_batch_limit")
    copies = [_copy_embed(embed, version=version, timestamp=timestamp) for embed in embeds]
    files: list[tuple[str, bytes, str]] = []
    for index, embed in enumerate(copies):
        if image_url:
            embed["image"] = {"url": _display_url(image_url)}
        elif attachments and attachments[index] is not None:
            data = attachments[index]
            if not isinstance(data, bytes) or len(data) > DISCORD_UPLOAD_LIMIT:
                raise HttpError("invalid_input", "discord_attachment_limit")
            suffix, media_type = _image_type(data)
            filename = f"cover.{suffix}" if len(copies) == 1 else f"cover_{index + 1}.{suffix}"
            files.append((filename, data, media_type))
            embed["image"] = {"url": f"attachment://{filename}"}
    if sum(_embed_length(embed) for embed in copies) > DISCORD_EMBED_TEXT_LIMIT:
        raise HttpError("invalid_input", "discord_embed_text_limit")
    payload: dict[str, object] = {"allowed_mentions": {"parse": []}}
    if copies:
        payload["embeds"] = copies
    elif message:
        content = _text(message)
        if len(content) > DISCORD_CONTENT_LIMIT:
            raise HttpError("invalid_input", "discord_content_limit")
        payload["content"] = content
    else:
        raise HttpError("invalid_input", "empty_discord_payload")
    if files:
        payload["attachments"] = [{"id": index, "filename": filename} for index, (filename, _, _) in enumerate(files)]
    return payload, tuple(files)


def batch_discord_payloads(
    embeds: Sequence[Mapping[str, object]], *, message: str | None = None, embed_limit: int = DISCORD_EMBED_LIMIT,
    version: str = "", timestamp: datetime | None = None,
) -> tuple[dict[str, object], ...]:
    if type(embed_limit) is not int or not 1 <= embed_limit <= DISCORD_EMBED_LIMIT:
        raise HttpError("invalid_input", "discord_batch_limit")
    if not embeds:
        content = _text(message) if message is not None else ""
        return tuple(prepare_discord_payload(content[index:index + DISCORD_CONTENT_LIMIT])[0]
                     for index in range(0, len(content), DISCORD_CONTENT_LIMIT))
    batches: list[dict[str, object]] = []
    group: list[dict[str, object]] = []
    total = 0
    for source in embeds:
        embed = _copy_embed(source, version=version, timestamp=timestamp)
        length = _embed_length(embed)
        if group and (len(group) == embed_limit or total + length > DISCORD_EMBED_TEXT_LIMIT):
            batches.append(prepare_discord_payload(embeds=group)[0])
            group, total = [], 0
        group.append(embed)
        total += length
    if group:
        batches.append(prepare_discord_payload(embeds=group)[0])
    return tuple(batches)


def _delivery_payload(
    payload: Mapping[str, object], attachments: Sequence[tuple[str, bytes, str]],
) -> dict[str, object]:
    if set(payload) - {"content", "embeds", "allowed_mentions", "attachments"}:
        raise HttpError("invalid_input", "invalid_discord_payload")
    embeds = payload.get("embeds", [])
    if not isinstance(embeds, (list, tuple)) or any(not isinstance(embed, Mapping) for embed in embeds):
        raise HttpError("invalid_input", "invalid_discord_embed")
    content = payload.get("content")
    if content is not None and not isinstance(content, str):
        raise HttpError("invalid_input", "invalid_discord_text")
    copied, _ = prepare_discord_payload(content, embeds)
    names: set[str] = set()
    for filename, data, media_type in attachments:
        suffix, actual_type = _image_type(data)
        if (not re.fullmatch(r"cover(?:_[1-9][0-9]*)?\." + suffix, filename)
                or filename in names or media_type != actual_type or len(data) > DISCORD_UPLOAD_LIMIT):
            raise HttpError("invalid_input", "invalid_discord_attachment")
        names.add(filename)
    if len(attachments) > DISCORD_EMBED_LIMIT:
        raise HttpError("invalid_input", "discord_batch_limit")
    expected = [{"id": index, "filename": filename} for index, (filename, _, _) in enumerate(attachments)]
    if payload.get("attachments", []) != expected:
        raise HttpError("invalid_input", "invalid_discord_attachment")
    for embed in cast(list[dict[str, object]], copied.get("embeds", [])):
        for key in ("image", "thumbnail"):
            value = cast(dict[str, str], embed.get(key, {}))
            url = value.get("url", "")
            if url.startswith("attachment://") and url.removeprefix("attachment://") not in names:
                raise HttpError("invalid_input", "invalid_discord_attachment")
    if expected:
        copied["attachments"] = expected
    return copied


class DiscordDelivery:
    """Own only live transports and selection state; the application owns the outbox."""

    def __init__(
        self, normal_webhooks: Sequence[str] = (), *, new_release_webhook: str = "",
        bookwalker_webhooks: Sequence[str] = (), connect_timeout: float = 5, read_timeout: float = 15,
        max_attempts: int = 3, cancel: Cancel = None,
    ) -> None:
        if isinstance(normal_webhooks, str) or isinstance(bookwalker_webhooks, str) or len(bookwalker_webhooks) not in {0, 2}:
            raise HttpError("invalid_input", "invalid_discord_routes")
        urls = {f"discord/normal/{index}": url for index, url in enumerate(normal_webhooks)}
        self._normal = tuple(urls)
        if new_release_webhook:
            urls["discord/new-release"] = new_release_webhook
        if bookwalker_webhooks:
            urls["discord/bookwalker/released"], urls["discord/bookwalker/preorder"] = bookwalker_webhooks
        self._clients: dict[str, HttpClient] = {}
        self._queries: dict[str, str] = {}
        self._next_normal = 0
        self._closed = False
        error = None
        try:
            for key, url in urls.items():
                parts = urlsplit(url)
                base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
                client = HttpClient(base_url, connect_timeout=connect_timeout, read_timeout=read_timeout,
                                    max_attempts=max_attempts, cancel=cancel)
                self._clients[key] = client
                query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != "wait"]
                query.append(("wait", "true"))
                self._queries[key] = "?" + urlencode(query)
        except HttpError as caught:
            error = caught
        except ValueError:
            error = HttpError("invalid_input", "invalid_discord_routes")
        if error is not None:
            self.close()
            raise error

    def __enter__(self) -> DiscordDelivery:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    @property
    def channel_keys(self) -> tuple[str, ...]:
        return tuple(self._clients)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            for client in self._clients.values():
                client.close()

    def select_channel(self, kind: str = "normal") -> str | None:
        if self._closed:
            raise HttpError("invalid_input", "discord_delivery_closed")
        if kind == "new_release":
            return "discord/new-release" if "discord/new-release" in self._clients else self.select_channel()
        if kind in {"bookwalker_released", "bookwalker_preorder"}:
            key = "discord/bookwalker/" + ("released" if kind == "bookwalker_released" else "preorder")
            return key if key in self._clients else None
        if kind != "normal":
            raise HttpError("invalid_input", "invalid_discord_channel")
        if not self._normal:
            return None
        key = self._normal[self._next_normal]
        self._next_normal = (self._next_normal + 1) % len(self._normal)
        return key

    def deliver(
        self, channel_key: str, payload: Mapping[str, object], *, authorization: RunAuthorization,
        attachments: Sequence[tuple[str, bytes, str]] = (),
    ) -> Decision:
        if not _CHANNEL_KEY.fullmatch(channel_key):
            return Decision("invalid_input", "invalid_discord_channel")
        if self._closed:
            return Decision("invalid_input", "discord_delivery_closed", candidates=(channel_key,))
        if channel_key not in self._clients:
            return Decision("missing_dependency", "discord_channel_not_configured", candidates=(channel_key,))
        error = None
        try:
            copied = _delivery_payload(payload, attachments)
        except HttpError as caught:
            error = caught
        if error is not None:
            return Decision(error.status, error.reason, candidates=(channel_key,))
        if not authorization.allow_remote:
            return Decision("planned", "remote_not_authorized", candidates=(channel_key,))
        files = {f"files[{index}]": item for index, item in enumerate(attachments)}
        try:
            response = self._clients[channel_key].request(
                "POST", self._queries[channel_key],
                data={"payload_json": json.dumps(copied, ensure_ascii=False, allow_nan=False)} if files else None,
                json=None if files else copied, files=files or None, expected_statuses=(200,),
            )
        except HttpError as caught:
            error = caught
        else:
            invalid = False
            try:
                confirmation = response.json()
            except ValueError:
                confirmation = None
                invalid = True
            finally:
                response.close()
            if (invalid or not isinstance(confirmation, dict) or not isinstance(confirmation.get("id"), str)
                    or not re.fullmatch(r"[0-9]+", confirmation["id"])):
                return Decision("review_required", "discord_delivery_pending_unknown", candidates=(channel_key,))
            return Decision("committed", "discord_delivered", candidates=(channel_key,))
        if error.pending_unknown:
            return Decision("review_required", "discord_delivery_pending_unknown", candidates=(channel_key,))
        return Decision(error.status, error.reason, candidates=(channel_key,))

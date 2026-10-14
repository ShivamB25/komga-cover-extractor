"""Commit notifications and remote scans share the existing journal, not file execution.

Preparation writes every bounded channel batch before settling local audit rows.
Each mutation has a separate atomic job claim. A running claim is never stolen;
an interrupted or ambiguously accepted POST requires review, not another POST.
Only explicit rejection or cancellation before acceptance permits pending retry.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import chain
from pathlib import Path
from threading import Event
from typing import cast

from ..covers.selection import CoverPolicy
from ..domain.decisions import Decision, RunAuthorization, RunResult, Status
from ..domain.libraries import LibraryRoot, PathMapping
from ..domain.operations import FileFingerprint
from ..domain.releases import FileObservation, ReleaseSnapshot
from ..infrastructure.filesystem import BOOK_SUFFIXES, checked_path, fingerprint
from ..infrastructure.images import ByteOutputPolicy
from ..infrastructure.state import (
    JobRecord,
    JobState,
    OutboxRecord,
    OutboxState,
    StateStore,
    StateStoreError,
)
from ..integrations.discord import (
    DISCORD_UPLOAD_LIMIT,
    GREEN_COLOR,
    GREY_COLOR,
    DiscordDelivery,
    batch_discord_payloads,
    format_bookwalker_notification,
    format_embed,
    format_release_notification,
    group_chapter_notifications,
    prepare_discord_payload,
)
from ..integrations.http import HttpClient, HttpError
from ..integrations.komga import (
    affected_libraries,
    get_komga_libraries,
    scan_komga_library,
)
from ..matching.ranking import RankingPolicy, rank_release
from ..parsing.filenames import ParsingPolicy, parse_release
from .extract_covers import extract_cover
from .release_checks import bookwalker_notifications

_OUTBOX_LIMIT = 10_000
_PREPARATION_KIND = "delivery_preparation"
_DISPATCH_KIND = "committed_delivery"
_BOOKWALKER_KIND = "bookwalker_delivery"
_RETRY_REASONS = frozenset({
    "http_rate_limited", "http_retry_budget", "http_cancelled", "delivery_cancelled_before_send",
})
_RED_COLOR = 16711680
_YELLOW_COLOR = 16776960


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                             allow_nan=False).encode()).hexdigest()


def _outbox(
    state: StateStore, *, operation_id: str | None = None, event_type: str | None = None,
    states: tuple[OutboxState, ...] = (),
) -> Iterator[OutboxRecord]:
    cursor: str | None = None
    count = 0
    page_size = 64 if event_type == "local_commit" else 1
    while count < _OUTBOX_LIMIT:
        rows = state.list_outbox(operation_id=operation_id, event_type=event_type, states=states,
                                after_event_id=cursor, limit=min(page_size, _OUTBOX_LIMIT - count))
        if not rows:
            return
        yield from rows
        count += len(rows)
        cursor = rows[-1].event_id
    if state.list_outbox(operation_id=operation_id, event_type=event_type, states=states,
                         after_event_id=cursor, limit=1):
        raise HttpError("review_required", "delivery_backlog_limit")


def _fingerprint(value: object) -> FileFingerprint | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "root", "relative_path", "device", "inode", "size", "mtime_ns", "sha256",
    }:
        raise StateStoreError("invalid delivery fingerprint")
    if any(not isinstance(value[key], str) for key in ("root", "relative_path", "sha256")) or any(
        type(value[key]) is not int for key in ("device", "inode", "size", "mtime_ns")
    ):
        raise StateStoreError("invalid delivery fingerprint")
    try:
        return FileFingerprint(Path(value["root"]), value["relative_path"], value["device"],
                               value["inode"], value["size"], value["mtime_ns"], value["sha256"])
    except (TypeError, ValueError):
        raise StateStoreError("invalid delivery fingerprint") from None


def _plan(state: StateStore, operation_id: str, events: Sequence[OutboxRecord]) -> tuple[dict[str, object], int]:
    for event in events:
        if event.payload.get("kind") not in {"move", "publish"}:
            continue
        destination = event.payload.get("destination")
        if not isinstance(destination, str):
            raise StateStoreError("invalid local delivery event")
        record = state.committed_artifact(Path(destination))
        if record is not None:
            value = json.loads(record.plan_json)
            if isinstance(value, dict) and value.get("operation_id") == operation_id:
                return value, record.created_ns
    operation = state.get_operation(operation_id)
    if operation is None or operation.phase not in {"committed", "cleanup_pending", "complete"}:
        raise HttpError("review_required", "delivery_operation_not_committed")
    return operation.plan.as_dict(), operation.created_ns


def _scope(plan: Mapping[str, object], roots: tuple[LibraryRoot, ...], state: StateStore) -> dict[str, object]:
    operations, permissions = plan.get("operations"), plan.get("roots")
    if (plan.get("schema_version") != 1 or not isinstance(operations, list)
            or not operations or len(operations) > _OUTBOX_LIMIT or not isinstance(permissions, list)):
        raise StateStoreError("invalid delivery operation metadata")
    if str(plan.get("reason", "")).startswith("rename_dictionary_") and all(
        isinstance(operation, dict) and isinstance(operation.get("destination"), str)
        and Path(operation["destination"]).is_relative_to(state.state_dir) for operation in operations
    ):
        return {"roots": [], "paths": []}
    configured = {root.path: root for root in roots}
    granted: set[Path] = set()
    for permission in permissions:
        if not isinstance(permission, dict) or set(permission) != {"path", "write"} or type(permission["write"]) is not bool:
            raise StateStoreError("invalid delivery root permission")
        path = Path(permission["path"])
        if permission["write"] and path not in configured:
            raise HttpError("review_required", "delivery_root_scope_changed")
        if permission["write"]:
            granted.add(path)
    affected_roots: set[Path] = set()
    paths: set[Path] = set()
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("destination"), str):
            raise StateStoreError("invalid delivery member metadata")
        destination = Path(operation["destination"])
        source = _fingerprint(operation.get("source"))
        prior = _fingerprint(operation.get("destination_before"))
        observed_paths = ((destination, prior),) + (((source.path, source),) if source is not None else ())
        for path, observed in observed_paths:
            matches = [root for root in roots if root.contains(path)]
            if (not path.is_absolute() or ".." in path.parts or len(matches) != 1
                    or matches[0].path not in granted or observed is not None and observed.root != matches[0].path):
                raise HttpError("review_required", "delivery_path_outside_current_scope")
            checked_path(path, matches[0].path, allow_missing=True)
            paths.add(path)
            affected_roots.add(matches[0].path)
    return {"roots": [str(path) for path in sorted(affected_roots)], "paths": [str(path) for path in sorted(paths)]}


def _local_events(state: StateStore, operation_id: str) -> tuple[OutboxRecord, ...]:
    return tuple(_outbox(state, operation_id=operation_id, event_type="local_commit"))


def _validate_events(plan: Mapping[str, object], events: Sequence[OutboxRecord]) -> list[dict[str, object]]:
    operations = cast(list[dict[str, object]], plan["operations"])
    by_member = {operation["member_id"]: operation for operation in operations}
    if len(by_member) != len(operations) or {event.member_id for event in events} != set(by_member):
        raise StateStoreError("delivery audit members do not match committed operation")
    for event in events:
        operation = by_member[event.member_id]
        expected = {key: operation[key] for key in ("member_id", "kind", "destination", "reason")}
        if event.payload != expected:
            raise StateStoreError("delivery audit payload does not match committed operation")
    return operations


def _snapshot(source: FileFingerprint, known: Mapping[Path, ReleaseSnapshot], roots: tuple[LibraryRoot, ...],
              parsing: ParsingPolicy) -> ReleaseSnapshot:
    observed = known.get(source.path)
    if observed is not None and observed.observation.fingerprint == source:
        return observed
    root = next(root for root in roots if root.path == source.root)
    role = "both" if {"library", "download"} <= set(root.roles) else "library" if "library" in root.roles else "download"
    if observed is not None and observed.observation.fingerprint is not None and (
        observed.observation.fingerprint.sha256, observed.observation.fingerprint.size
    ) == (source.sha256, source.size):
        return replace(observed, observation=replace(observed.observation, fingerprint=source, root_role=role,
                                                    at_configured_root=source.path.parent == root.path))
    return parse_release(FileObservation(source.path, siblings=tuple(path for path in known if path.parent == source.path.parent),
                                         root_role=role, fingerprint=source), parsing)


def _match_embed(
    incoming: ReleaseSnapshot, decision: Decision, before: Mapping[Path, ReleaseSnapshot],
    roots: tuple[LibraryRoot, ...],
) -> dict[str, object]:
    observed: dict[str, list[str]] = {}
    for candidate in decision.candidates:
        key, separator, value = candidate.partition("=")
        if separator:
            observed.setdefault(key, []).append(value)
    if len(observed.get("series", ())) != 1 or len(observed.get("evidence", ())) != 1:
        raise HttpError("invalid_input", "notification_match_evidence_invalid")
    directory = Path(observed["series"][0])
    if (not directory.is_absolute() or ".." in directory.parts
            or not any("library" in root.roles and root.contains(directory) for root in roots)):
        raise HttpError("review_required", "notification_match_scope_changed")
    evidence = observed["evidence"][0]
    fields: list[dict[str, object]] = [
        {"name": "Existing Series Location", "value": f"```{directory}```", "inline": False},
    ]
    if "identifier" in evidence:
        incoming_ids = tuple(value for _, value in incoming.identifiers)
        if incoming_ids:
            fields.append({"name": "Downloaded File", "value": "```" + "\n".join(incoming_ids) + "```", "inline": False})
        existing_ids = tuple(dict.fromkeys(value for release in before.values() if release.parent == directory
                                           for _, value in release.identifiers))
        if existing_ids:
            fields.append({"name": "Existing Library File", "value": "```" + "\n".join(existing_ids) + "```", "inline": False})
        title = "Found Series Match (Matching Identifier)"
    else:
        image = "cover" in evidence
        fields.extend((
            {"name": "Existing Folder Name" if image else "Downloaded File Series Name",
             "value": f"```{directory.name if image else incoming.display_series}```", "inline": True},
            {"name": "File Series Name" if image else "Existing Library Folder Name",
             "value": f"```{incoming.display_series if image else directory.name}```", "inline": image},
        ))
        score = observed.get("score", ())
        threshold = observed.get("required_image_similarity_score" if image else "required_similarity_score", ())
        for values in (score, threshold):
            try:
                if len(values) > 1 or values and (not Decimal(values[0]).is_finite() or not 0 <= Decimal(values[0]) <= 1):
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise HttpError("invalid_input", "notification_match_metrics_invalid") from None
        if score:
            fields.append({"name": "Image Similarity Score" if image else "Similarity Score",
                           "value": f"```{score[0]}```", "inline": not image})
        if threshold:
            fields.append({"name": "Required Score", "value": f"```>= {threshold[0]}```", "inline": True})
        title = "Found Series Match (Cover Match)" if image else "Found Series Match (CACHE)" if "cached" in evidence else "Found Series Match"
    return format_embed(title=title, color=GREY_COLOR, fields=fields)


def _upgrade_embed(existing: ReleaseSnapshot, incoming: ReleaseSnapshot, ranking: RankingPolicy,
                   *, upgrade: bool) -> dict[str, object]:
    fields: list[dict[str, object]] = []
    for label, release in (("From", existing), ("To", incoming)):
        ranked = rank_release(release, ranking.rules)
        fields.append({"name": label, "value": f"```{release.name}```", "inline": False})
        if ranked.status == "ranked":
            fields.extend((
                {"name": "Score", "value": f"{ranked.total_score:g}", "inline": True},
                {"name": "Tags", "value": ", ".join(f"{match.rule.name} ({match.score:g})" for match in ranked.candidates) or "None", "inline": True},
            ))
        if release.observation.fingerprint is not None:
            fields.append({"name": "Size", "value": f"{round(release.observation.fingerprint.size / 1_000_000, 1)} MB", "inline": True})
    return format_embed(title="Upgrade Process (Upgrade)" if upgrade else "Upgrade Process (Not Upgradeable)",
                        color=GREEN_COLOR if upgrade else _YELLOW_COLOR, fields=fields)


def _same_release(first: ReleaseSnapshot, second: ReleaseSnapshot) -> bool:
    return (first.kind == second.kind and first.canonical_series_key == second.canonical_series_key
            and first.number.kind != "missing" and second.number.kind != "missing"
            and first.number.part == second.number.part
            and (first.number.covers(second.number) or second.number.covers(first.number)))


def _notifications(
    operations: Sequence[dict[str, object]], *, state: StateStore, operation_id: str,
    roots: tuple[LibraryRoot, ...], parsing: ParsingPolicy, ranking: RankingPolicy,
    before: Mapping[Path, ReleaseSnapshot], after: Mapping[Path, ReleaseSnapshot],
    match_decisions: Mapping[Path, Decision],
    chapter_covers: bool, group_chapters: bool, mute_rename: bool,
) -> Iterator[tuple[str, dict[str, object], ReleaseSnapshot | None]]:
    retired: list[tuple[dict[str, object], ReleaseSnapshot]] = []
    for operation in operations:
        source = _fingerprint(operation["source"])
        if operation["kind"] == "quarantine" and source is not None and source.path.suffix.lower() in BOOK_SUFFIXES:
            retired.append((operation, _snapshot(source, before, roots, parsing)))
    members = {member.member_id: member for member in state.list_members(operation_id)} if retired else {}
    chapters: list[ReleaseSnapshot] = []
    moved_folders: set[tuple[Path, Path]] = set()
    matched_sources: set[Path] = set()
    for operation in operations:
        destination = Path(cast(str, operation["destination"]))
        source = _fingerprint(operation["source"])
        reason = str(operation["reason"])
        if operation["kind"] in {"mkdir", "rmdir"} or destination.suffix.lower() not in BOOK_SUFFIXES:
            continue
        observed_source = before.get(source.path) if source is not None else None
        if (source is not None and source.path in match_decisions and source.path not in matched_sources
                and observed_source is not None and observed_source.observation.fingerprint == source):
            matched_sources.add(source.path)
            yield "normal", _match_embed(_snapshot(source, before, roots, parsing), match_decisions[source.path], before, roots), None
        if operation["kind"] == "quarantine":
            assert source is not None
            release = _snapshot(source, before, roots, parsing)
            member = members[cast(str, operation["member_id"])]
            if reason in {"not_upgrade", "covered_lower_rank_release", "ranked_or_identical_duplicate", "identical_destination_merge"}:
                keepers = [other for other in (*before.values(), *after.values())
                           if other.path != release.path and _same_release(release, other)
                           and other.path not in {item.path for _, item in retired}]
                by_path = {keeper.path: keeper for keeper in keepers}
                if len(by_path) == 1:
                    keeper = next(iter(by_path.values()))
                    if reason in {"not_upgrade", "covered_lower_rank_release"}:
                        yield "normal", _upgrade_embed(keeper, release, ranking, upgrade=False), None
                    else:
                        identical = (keeper.observation.fingerprint is not None
                                     and keeper.observation.fingerprint.sha256 == source.sha256)
                        fields = [{"name": "Location", "value": f"```{release.parent}```", "inline": False}]
                        if identical:
                            fields.extend(({"name": "File Names", "value": f"```{keeper.name}\n{release.name}```", "inline": False},
                                           {"name": "File Hashes", "value": f"```{source.sha256} {source.sha256}```", "inline": False}))
                        else:
                            fields.extend(({"name": "Duplicate", "value": f"```{release.name}```", "inline": False},
                                           {"name": "has a lower score than", "value": f"```{keeper.name}```", "inline": False}))
                        yield "normal", format_embed(title="Duplicate Download Release (HASH MATCH)" if identical else "Duplicate Download Release (Not Upgradeable)",
                                                      color=_YELLOW_COLOR, fields=fields), None
            if reason in {"unacceptable_file", "chapter_filter"}:
                fields = [{"name": "In" if reason == "unacceptable_file" else "File", "value": f"```{release.name}```", "inline": False},
                          {"name": "Location", "value": f"```{release.parent}```", "inline": False}]
                if reason == "chapter_filter":
                    fields.append({"name": "Checks", "value": "```Contains chapter keywords/lone numbers ✓\nDoes not contain any volume keywords ✓\nDoes not contain any exclusion keywords ✓```", "inline": False})
                yield "normal", format_embed(title="Unacceptable Match Found" if reason == "unacceptable_file" else "Chapter Release Found",
                                              color=_YELLOW_COLOR if reason == "unacceptable_file" else GREY_COLOR, fields=fields), None
            yield "normal", format_embed(title="Removed File" if member.status == "complete" else "Quarantine Pending", color=_RED_COLOR,
                                          fields=({"name": "File", "value": f"```{release.name}```", "inline": False},
                                                  {"name": "Location", "value": f"```{release.parent}```", "inline": False})), None
            continue
        record = state.committed_artifact(destination)
        if record is None or json.loads(record.plan_json).get("operation_id") != operation_id:
            raise HttpError("review_required", "delivery_publication_superseded")
        expected = operation.get("artifact") if operation["kind"] == "publish" else operation.get("source")
        if (not isinstance(expected, dict) or record.source != source or record.kind != operation["kind"]
                or (expected.get("sha256"), expected.get("size")) != (record.published.sha256, record.published.size)):
            raise HttpError("review_required", "delivery_publication_metadata_changed")
        if fingerprint(destination, record.published.root) != record.published:
            raise HttpError("stale_plan", "delivery_published_bytes_changed")
        release = _snapshot(record.published, after, roots, parsing)
        incoming = _snapshot(source, before, roots, parsing) if source is not None else release
        if reason in {"verified_native_conversion", "verified_conversion_and_organization"}:
            yield "normal", format_embed(title="Converted to CBZ", color=GREY_COLOR, fields=(
                {"name": "From", "value": f"```{incoming.name}```", "inline": False},
                {"name": "To", "value": f"```{release.name}```", "inline": False},
                {"name": "Location", "value": f"```{release.parent}```", "inline": False},
            )), None
        elif source is not None and source.path.name != destination.name and not mute_rename:
            title = "Reorganized & Renamed File" if reason == "restructure_release" else "Renamed File"
            yield "normal", format_embed(title=title, color=GREY_COLOR, fields=(
                {"name": "From", "value": f"```{source.path.name}```", "inline": False},
                {"name": "To", "value": f"```{destination.name}```", "inline": False},
            )), None
        upgrades = [existing for retired_operation, existing in retired
                    if retired_operation["reason"] == "complete_collection_or_release_upgrade"
                    and _same_release(release, existing)]
        prior = _fingerprint(operation["destination_before"])
        if prior is not None:
            existing = _snapshot(prior, before, roots, parsing)
            if _same_release(release, existing):
                upgrades.append(existing)
        for existing in {item.path: item for item in upgrades}.values():
            yield "normal", _upgrade_embed(existing, incoming, ranking, upgrade=True), None
        if source is not None and source.path.parent != destination.parent:
            yield "normal", format_embed(title="Moved File", color=GREY_COLOR, fields=(
                {"name": "File", "value": f"```{release.name}```", "inline": False},
                {"name": "To", "value": f"```{release.parent}```", "inline": False},
            )), None
            pair = source.path.parent, destination.parent
            if pair not in moved_folders and any(op["kind"] == "rmdir" and op["destination"] == str(pair[0]) for op in operations):
                moved_folders.add(pair)
                routing = reason == "directory_route" or any("library" in root.roles and root.contains(source.path) for root in roots)
                fields = [{"name": "From", "value": f"```{pair[0]}```", "inline": False},
                          {"name": "To", "value": f"```{pair[1]}```", "inline": False}]
                if not routing:
                    fields.insert(0, {"name": "Folder", "value": f"```{pair[0].name}```", "inline": False})
                yield "normal", format_embed(title="Moved Series" if routing else "Moved Folder", color=GREY_COLOR, fields=fields), None
        acquired = (reason == "existing_series_new_release" or source is None
                    or incoming.observation.root_role in {"download", "both"} and source.path.parent != destination.parent)
        if (not upgrades and acquired and any("library" in root.roles and root.contains(destination) for root in roots)
                and release.kind in {"volume", "chapter"} and release.number.kind != "missing"):
            if (source is None or source.path not in matched_sources) and any(item.parent == release.parent for item in before.values()):
                yield "normal", format_embed(title="Found Series Match", color=GREY_COLOR, fields=(
                    {"name": "Existing Series Location", "value": f"```{release.parent}```", "inline": False},
                    {"name": "Downloaded File Series Name", "value": f"```{incoming.display_series}```", "inline": True},
                    {"name": "Existing Library Folder Name", "value": f"```{release.parent.name}```", "inline": False},
                )), None
            if release.kind == "chapter":
                chapters.append(release)
            else:
                yield "new_release", format_release_notification(release), release
    if chapter_covers or not group_chapters:
        for release in sorted(chapters, key=lambda item: (item.display_series, min(item.number.values, default=Decimal(0)), item.number.part or 0, item.name)):
            yield "new_release", format_release_notification(release), release if chapter_covers else None
    else:
        for embed in group_chapter_notifications(chapters):
            yield "new_release", embed, None


def _stored_discord(channel: str, payload: dict[str, object], files: Sequence[tuple[str, bytes, str]],
                    *, scope: dict[str, object] | None = None, identity: dict[str, str] | None = None) -> dict[str, object]:
    value: dict[str, object] = {"schema_version": 1, "channel_key": channel, "payload": payload,
        "attachments": [{"filename": name, "media_type": media, "data_base64": base64.b64encode(data).decode("ascii"),
                         "sha256": sha256(data).hexdigest()} for name, data, media in files]}
    if scope is not None:
        value["scope"] = scope
    if identity is not None:
        value["identity"] = identity
    return value


def _discord_data(value: Mapping[str, object]) -> tuple[str, dict[str, object], tuple[tuple[str, bytes, str], ...]]:
    channel = value.get("channel_key")
    payload = value.get("payload")
    attachments = value.get("attachments")
    if (value.get("schema_version") != 1 or not isinstance(channel, str)
            or not isinstance(payload, dict) or not isinstance(attachments, list)
            or len(attachments) > 10):
        raise StateStoreError("invalid durable Discord payload")
    files: list[tuple[str, bytes, str]] = []
    total = 0
    for item in attachments:
        if (not isinstance(item, dict) or set(item) != {"filename", "media_type", "data_base64", "sha256"}
                or any(not isinstance(item[key], str) for key in item)
                or len(item["data_base64"]) > ((DISCORD_UPLOAD_LIMIT + 2) // 3) * 4):
            raise StateStoreError("invalid durable Discord attachment")
        try:
            data = base64.b64decode(item["data_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise StateStoreError("invalid durable Discord attachment") from None
        total += len(data)
        if total > DISCORD_UPLOAD_LIMIT or sha256(data).hexdigest() != item["sha256"]:
            raise StateStoreError("invalid durable Discord attachment")
        files.append((item["filename"], data, item["media_type"]))
    return channel, payload, tuple(files)


def _enqueue_notifications(
    items: Iterator[tuple[str, dict[str, object], ReleaseSnapshot | None]], *, state: StateStore,
    operation_id: str, member_id: str, scope: dict[str, object], discord: DiscordDelivery,
    cover_policy: CoverPolicy, roots: tuple[LibraryRoot, ...], cancel: Event, version: str, created_ns: int,
) -> None:
    group: list[dict[str, object]] = []
    images: list[bytes | None] = []
    route = ""
    batch = 0
    timestamp = datetime.fromtimestamp(created_ns / 1_000_000_000, UTC)

    def flush() -> None:
        nonlocal batch, group, images
        if not group:
            return
        channel = discord.select_channel(route)
        if channel is not None:
            payload, files = prepare_discord_payload(embeds=group, attachments=images, version=version, timestamp=timestamp)
            state.enqueue_event(operation_id, member_id, f"delivery_discord_{batch}",
                                _stored_discord(channel, payload, files, scope=scope))
            batch += 1
        group, images = [], []

    for kind, embed, release in items:
        if cancel.is_set():
            raise HttpError("cancelled", "delivery_cancelled_before_send")
        eligible = ("discord/new-release" in discord.channel_keys or any(key.startswith("discord/normal/") for key in discord.channel_keys)) if kind == "new_release" else any(key.startswith("discord/normal/") for key in discord.channel_keys)
        if not eligible:
            continue
        image = None
        if release is not None:
            root = next(root for root in roots if root.contains(release.path))
            cover = extract_cover(release.path, policy=cover_policy, output=ByteOutputPolicy(), root=root.path,
                                  release_kind=release.kind, cancel=cancel)
            if cover.status == "valid":
                image = cover.data
            elif cover.status != "absent":
                if cover.status == "cancelled":
                    raise HttpError("cancelled", "delivery_cancelled_before_send")
                raise HttpError(cast(Status, cover.status), "notification_cover_" + cover.reason)
        if image is not None and len(image) > DISCORD_UPLOAD_LIMIT:
            raise HttpError("review_required", "discord_attachment_limit")
        if group and (kind != route or sum(len(data) for data in images if data is not None) + len(image or b"") > DISCORD_UPLOAD_LIMIT
                      or len(batch_discord_payloads((*group, embed), version=version, timestamp=timestamp)) > 1):
            flush()
        route = kind
        group.append(embed)
        images.append(image)
    flush()


def _job_outcome(job: JobRecord, payload: dict[str, object], identity: str) -> Decision:
    if job.payload != payload:
        return Decision("review_required", "delivery_claim_payload_changed", candidates=(identity,))
    if job.state == "complete":
        return Decision("skipped", "delivery_already_settled", candidates=(identity,))
    if job.state == "running":
        return Decision("delivery_deferred", "delivery_claim_unsettled", candidates=(identity,))
    if job.state == "needs_review":
        return Decision("review_required", job.error or "delivery_pending_unknown", candidates=(identity,))
    return Decision("delivery_deferred", job.error or "delivery_pending", candidates=(identity,))


def _send_claimed(
    *, state: StateStore, job_id: str, kind: str, payload: dict[str, object], identity: str,
    cancel: Event, send: Callable[[], Decision],
) -> Decision:
    prior = state.get_job(job_id)
    retry = prior is not None and prior.kind == kind and prior.state == "pending" and prior.error in _RETRY_REASONS
    if not state.claim_job(job_id, kind, payload, retry_pending=retry):
        job = state.get_job(job_id)
        if job is None or job.kind != kind:
            return Decision("review_required", "delivery_claim_identity_changed", candidates=(identity,))
        return _job_outcome(job, payload, identity)
    if cancel.is_set():
        outcome = Decision("cancelled", "delivery_cancelled_before_send", candidates=(identity,))
    else:
        try:
            outcome = send()
        except Exception:
            outcome = Decision("review_required", "delivery_pending_unknown", candidates=(identity,))
    status: JobState
    if outcome.status in {"committed", "skipped"}:
        status = "complete"
    elif outcome.status == "review_required":
        status = "needs_review"
    elif outcome.reason in _RETRY_REASONS and outcome.status in {"cancelled", "delivery_deferred"}:
        status = "pending"
    else:
        status = "failed"
    state.put_job(job_id, kind, payload, state=status, error=None if status == "complete" else outcome.reason)
    return outcome


def _settle_row(state: StateStore, event: OutboxRecord, outcome: Decision) -> bool:
    if outcome.status in {"committed", "skipped"}:
        state.update_outbox(event.event_id, "complete", increment_attempts=outcome.status == "committed")
        return True
    if outcome.reason == "delivery_claim_unsettled":
        return False
    job = state.get_job("delivery-" + event.event_id)
    target: OutboxState = "unknown" if job is not None and job.state == "needs_review" else "pending" if job is not None and job.state == "pending" else "failed"
    state.update_outbox(event.event_id, target, error=outcome.reason,
                        increment_attempts=outcome.reason not in {"delivery_cancelled_before_send", "delivery_claim_payload_changed", "delivery_claim_identity_changed"})
    return False


def _settled_result(
    result: RunResult, decisions: Sequence[Decision], deferred: set[str], completed: set[str],
) -> RunResult:
    prior = tuple(decision for decision in result.decisions if not (
        decision.path is None and decision.candidates and set(decision.candidates) <= completed
        and decision.status not in {"committed", "observed", "planned", "skipped"}
    ))
    return replace(result, decisions=prior + tuple(decisions), deferred_deliveries=tuple(sorted(deferred)))


def settle_committed_deliveries(
    result: RunResult, *, state: StateStore, roots: tuple[LibraryRoot, ...], parsing: ParsingPolicy,
    ranking: RankingPolicy, cover_policy: CoverPolicy, discord: DiscordDelivery | None,
    komga: HttpClient | None, mappings: tuple[PathMapping, ...], authorization: RunAuthorization,
    cancel: Event, before: Sequence[ReleaseSnapshot] = (), chapter_covers: bool = False,
    mute_rename: bool = False, version: str = "",
) -> RunResult:
    if not authorization.apply and not authorization.allow_remote:
        return result
    decisions: list[Decision] = []
    deferred = set(result.deferred_deliveries)
    completed: set[str] = set()
    match_decisions = {decision.path: decision for decision in result.decisions
                       if decision.status == "observed" and decision.reason == "series_match" and decision.path is not None}
    known_before = {release.path: release for release in before}
    known_after = {release.path: release for release in result.releases}
    seen: set[str] = set()
    scopes: dict[str, dict[str, object]] = {}
    try:
        for event_id in tuple(deferred):
            if state.outbox_state(event_id) == "complete":
                deferred.discard(event_id)
                completed.add(event_id)
        for event in _outbox(state, event_type="local_commit", states=("queued", "pending", "unknown", "failed")):
            if event.operation_id in seen:
                continue
            seen.add(event.operation_id)
            events = _local_events(state, event.operation_id)
            deferred.update(item.event_id for item in events if item.state != "complete")
            plan, created_ns = _plan(state, event.operation_id, events)
            try:
                scope = _scope(plan, roots, state)
            except HttpError as error:
                decisions.append(Decision(error.status, error.reason,
                                          candidates=tuple(item.event_id for item in events if item.state != "complete")))
                continue
            operations = _validate_events(plan, events)
            if len(scopes) == 4096:
                scopes.pop(next(iter(scopes)))
            scopes[event.operation_id] = scope
            payload = {"schema_version": 1, "operation_id": event.operation_id, "scope": scope}
            job_id = "delivery-prepare-" + event.operation_id
            prior = state.get_job(job_id)
            retry = prior is not None and prior.state == "pending" and prior.error == "delivery_cancelled_before_send"
            claimed = state.claim_job(job_id, _PREPARATION_KIND, payload, retry_pending=retry)
            if not claimed:
                job = state.get_job(job_id)
                if job is None or job.kind != _PREPARATION_KIND or job.payload != payload or job.state != "complete":
                    decisions.append(Decision("review_required" if job is not None and job.state in {"needs_review", "failed"} else "delivery_deferred",
                                              job.error if job is not None and job.error else "delivery_preparation_unsettled",
                                              candidates=tuple(item.event_id for item in events if item.state != "complete")))
                    deferred.update(item.event_id for item in events if item.state != "complete")
                    continue
            else:
                try:
                    if cancel.is_set():
                        raise HttpError("cancelled", "delivery_cancelled_before_send")
                    anchor = min(events, key=lambda item: item.member_id).member_id
                    if scope["paths"] and discord is not None and any(
                        key.startswith("discord/normal/") or key == "discord/new-release" for key in discord.channel_keys
                    ):
                        items = _notifications(operations, state=state, operation_id=event.operation_id, roots=roots,
                                               parsing=parsing, ranking=ranking, before=known_before, after=known_after,
                                               chapter_covers=chapter_covers, mute_rename=mute_rename,
                                               group_chapters="discord/new-release" in discord.channel_keys,
                                               match_decisions=match_decisions)
                        _enqueue_notifications(items, state=state, operation_id=event.operation_id, member_id=anchor,
                                               scope=scope, discord=discord, cover_policy=cover_policy, roots=roots,
                                               cancel=cancel, version=version, created_ns=created_ns)
                    affected = [path for path in cast(list[str], scope["paths"])
                                if any("library" in root.roles and root.contains(Path(path)) for root in roots)]
                    if affected and komga is not None:
                        state.enqueue_event(event.operation_id, anchor, "delivery_komga_discovery",
                                            {"schema_version": 1, "scope": scope, "paths": affected})
                    state.put_job(job_id, _PREPARATION_KIND, payload, state="complete")
                except (HttpError, OSError, StateStoreError) as error:
                    reason = error.reason if isinstance(error, HttpError) else "delivery_preparation_failed"
                    status: JobState = "pending" if reason == "delivery_cancelled_before_send" else "needs_review"
                    state.put_job(job_id, _PREPARATION_KIND, payload, state=status, error=reason)
                    decisions.append(Decision(error.status if isinstance(error, HttpError) else "review_required", reason,
                                              candidates=tuple(item.event_id for item in events if item.state != "complete")))
                    deferred.update(item.event_id for item in events if item.state != "complete")
                    continue
            for item in events:
                state.update_outbox(item.event_id, "complete")
                deferred.discard(item.event_id)
                completed.add(item.event_id)
        libraries = None
        dispatch = chain(
            _outbox(state, event_type="delivery_komga_discovery", states=("queued", "pending", "unknown", "failed")),
            (item for item in _outbox(state, states=("queued", "pending", "unknown", "failed"))
             if item.event_type != "delivery_komga_discovery"),
        )
        for event in dispatch:
            if not event.event_type.startswith("delivery_"):
                continue
            deferred.add(event.event_id)
            cached_scope = scopes.get(event.operation_id)
            if cached_scope is None:
                events = _local_events(state, event.operation_id)
                plan, _ = _plan(state, event.operation_id, events)
                try:
                    scope = _scope(plan, roots, state)
                except HttpError as error:
                    decisions.append(Decision(error.status, error.reason, candidates=(event.event_id,)))
                    continue
                _validate_events(plan, events)
                if len(scopes) == 4096:
                    scopes.pop(next(iter(scopes)))
                scopes[event.operation_id] = scope
            else:
                scope = cached_scope
            if event.payload.get("scope") != scope:
                decisions.append(Decision("review_required", "delivery_payload_scope_changed", candidates=(event.event_id,)))
                continue
            preparation = state.get_job("delivery-prepare-" + event.operation_id)
            if preparation is None or preparation.state != "complete":
                decisions.append(Decision("delivery_deferred", "delivery_preparation_unsettled", candidates=(event.event_id,)))
                continue
            if event.state == "unknown":
                decisions.append(Decision("review_required", event.error or "delivery_pending_unknown", candidates=(event.event_id,)))
                continue
            if event.state == "failed":
                decisions.append(Decision("delivery_deferred", event.error or "delivery_failed", candidates=(event.event_id,)))
                continue
            if not authorization.allow_remote or cancel.is_set():
                decisions.append(Decision("cancelled" if cancel.is_set() else "delivery_deferred",
                                          "delivery_cancelled_before_send" if cancel.is_set() else "remote_not_authorized",
                                          candidates=(event.event_id,)))
                continue
            if event.event_type == "delivery_komga_discovery":
                if komga is None:
                    decisions.append(Decision("delivery_deferred", "komga_not_configured", candidates=(event.event_id,)))
                    continue
                expected = [path for path in cast(list[str], scope["paths"])
                            if any("library" in root.roles and root.contains(Path(path)) for root in roots)]
                if event.payload != {"schema_version": 1, "scope": scope, "paths": expected}:
                    raise StateStoreError("invalid Komga discovery intent")
                try:
                    if libraries is None:
                        libraries = get_komga_libraries(komga)
                    matches = affected_libraries((Path(path) for path in expected), libraries, mappings=mappings)
                    if not matches:
                        decisions.append(Decision("review_required", "komga_affected_library_not_found", candidates=(event.event_id,)))
                        continue
                    for library in matches:
                        state.enqueue_event(event.operation_id, event.member_id, "delivery_komga_scan_" + _digest(library["id"]),
                                            {"schema_version": 1, "scope": scope, "channel_key": "komga/" + library["id"]})
                    state.update_outbox(event.event_id, "complete")
                    deferred.discard(event.event_id)
                    completed.add(event.event_id)
                except HttpError as error:
                    permanent = error.status in {"permission_denied", "invalid_input", "unsupported", "missing_dependency"}
                    state.update_outbox(event.event_id, "failed" if permanent else "pending", error=error.reason)
                    decisions.append(Decision(error.status, error.reason, candidates=(event.event_id,)))
                continue
            claim = {"schema_version": 1, "event_id": event.event_id, "payload_sha256": _digest(event.payload)}
            if event.event_type.startswith("delivery_discord_"):
                channel, payload, files = _discord_data(event.payload)
                if discord is None or channel not in discord.channel_keys:
                    decisions.append(Decision("delivery_deferred", "discord_channel_not_configured", candidates=(event.event_id,)))
                    continue
                def send_discord() -> Decision:
                    return discord.deliver(channel, payload, attachments=files, authorization=authorization)

                send = send_discord
            elif event.event_type.startswith("delivery_komga_scan_"):
                komga_channel = event.payload.get("channel_key")
                if (set(event.payload) != {"schema_version", "scope", "channel_key"} or event.payload["schema_version"] != 1
                        or not isinstance(komga_channel, str) or not komga_channel.startswith("komga/")
                        or event.event_type != "delivery_komga_scan_" + _digest(komga_channel.removeprefix("komga/"))):
                    raise StateStoreError("invalid Komga scan intent")
                if komga is None:
                    decisions.append(Decision("delivery_deferred", "komga_not_configured", candidates=(event.event_id,)))
                    continue
                try:
                    if libraries is None:
                        libraries = get_komga_libraries(komga)
                    current = affected_libraries((Path(path) for path in cast(list[str], scope["paths"])
                                                  if any("library" in root.roles and root.contains(Path(path)) for root in roots)),
                                                 libraries, mappings=mappings)
                    if komga_channel.removeprefix("komga/") not in {library["id"] for library in current}:
                        decisions.append(Decision("review_required", "komga_scan_scope_changed", candidates=(event.event_id,)))
                        continue
                except HttpError as error:
                    permanent = error.status in {"permission_denied", "invalid_input", "unsupported", "missing_dependency"}
                    state.update_outbox(event.event_id, "failed" if permanent else "pending", error=error.reason)
                    decisions.append(Decision(error.status, error.reason, candidates=(event.event_id,)))
                    continue
                def send_komga() -> Decision:
                    return scan_komga_library(komga, komga_channel.removeprefix("komga/"), authorization=authorization)

                send = send_komga
            else:
                raise StateStoreError("unknown durable delivery type")
            outcome = _send_claimed(state=state, job_id="delivery-" + event.event_id, kind=_DISPATCH_KIND,
                                    payload=claim, identity=event.event_id, cancel=cancel, send=send)
            decisions.append(replace(outcome, candidates=(event.event_id,)))
            if _settle_row(state, event, outcome):
                deferred.discard(event.event_id)
                completed.add(event.event_id)
    except (StateStoreError, OSError, ValueError, TypeError, HttpError) as error:
        decisions.append(Decision(error.status if isinstance(error, HttpError) else "review_required",
                                  error.reason if isinstance(error, HttpError) else "delivery_journal_or_scope_unavailable",
                                  candidates=tuple(sorted(deferred))))
    return _settled_result(result, decisions, deferred, completed)


def _bookwalker_identity(url: str, channel: str) -> str:
    return "bookwalker-" + _digest({"url": url, "channel": channel})


def settle_bookwalker_deliveries(
    result: RunResult, *, state: StateStore, discord: DiscordDelivery | None,
    authorization: RunAuthorization, cancel: Event, journal_ready: Callable[[], Decision], version: str = "",
) -> RunResult:
    notifications = bookwalker_notifications(result.missing_releases)
    if not authorization.allow_remote:
        return replace(result, decisions=result.decisions + ((Decision("planned", "bookwalker_delivery_not_authorized"),) if notifications and discord is not None else ()))
    if cancel.is_set():
        return replace(result, decisions=result.decisions + (Decision("cancelled", "delivery_cancelled_before_send"),))
    decisions: list[Decision] = []
    deferred = set(result.deferred_deliveries)
    completed: set[str] = set()
    try:
        for job_id in tuple(deferred):
            job = state.get_job(job_id)
            if job is not None and job.kind == _BOOKWALKER_KIND and job.state == "complete":
                deferred.discard(job_id)
                completed.add(job_id)
        pending = tuple(state.list_jobs(("pending", "running", "needs_review", "failed"), kind=_BOOKWALKER_KIND, limit=_OUTBOX_LIMIT))
        if len(pending) == _OUTBOX_LIMIT and state.list_jobs(
            ("pending", "running", "needs_review", "failed"), kind=_BOOKWALKER_KIND,
            after_job_id=pending[-1].job_id, limit=1,
        ):
            raise HttpError("review_required", "delivery_backlog_limit")
        if not pending and (discord is None or not any(key.startswith("discord/bookwalker/") for key in discord.channel_keys) or not notifications):
            return _settled_result(result, decisions, deferred, completed)
        jobs = {job.job_id: job.payload for job in pending}
        for notification in notifications:
            item = cast(dict[str, object], notification["item"])
            observed_date = item.get("date")
            try:
                if not isinstance(observed_date, str):
                    raise ValueError
                date.fromisoformat(observed_date)
            except ValueError:
                decisions.append(Decision("invalid_input", "bookwalker_notification_date_invalid"))
                continue
            channel = "discord/bookwalker/" + ("preorder" if notification["channel"] == "bookwalker_preorder" else "released")
            if discord is None or channel not in discord.channel_keys:
                continue
            identity = {"url": cast(str, item["url"]), "channel": channel}
            job_id = _bookwalker_identity(identity["url"], channel)
            existing = state.get_job(job_id)
            if existing is not None:
                if existing.kind != _BOOKWALKER_KIND or existing.payload.get("identity") != identity:
                    decisions.append(Decision("review_required", "bookwalker_delivery_identity_changed", candidates=(job_id,)))
                    deferred.add(job_id)
                elif existing.state != "complete":
                    jobs[job_id] = existing.payload
                continue
            embed = format_bookwalker_notification(item, preorder=channel.endswith("preorder"))
            payload, files = prepare_discord_payload(embeds=(embed,), version=version, timestamp=datetime.now(UTC))
            jobs[job_id] = _stored_discord(channel, payload, files, identity=identity)
        if not jobs:
            return _settled_result(result, decisions, deferred, completed)
        ready = journal_ready()
        if ready.status != "observed":
            return _settled_result(result, (*decisions, ready), deferred | {job.job_id for job in pending}, completed)
        for job_id, value in jobs.items():
            stored_identity = value.get("identity")
            channel, payload, files = _discord_data(value)
            embeds = payload.get("embeds")
            if (set(value) != {"schema_version", "channel_key", "payload", "attachments", "identity"}
                    or not isinstance(stored_identity, dict) or set(stored_identity) != {"url", "channel"}
                    or not isinstance(stored_identity["url"], str) or stored_identity["channel"] != channel
                    or channel not in {"discord/bookwalker/released", "discord/bookwalker/preorder"}
                    or job_id != _bookwalker_identity(stored_identity["url"], channel)
                    or files or not isinstance(embeds, list) or len(embeds) != 1 or not isinstance(embeds[0], dict)
                    or embeds[0].get("url") != stored_identity["url"]):
                raise StateStoreError("invalid BookWalker delivery identity")
            deferred.add(job_id)
            if discord is None or channel not in discord.channel_keys:
                decisions.append(Decision("delivery_deferred", "discord_channel_not_configured", candidates=(job_id,)))
                continue
            outcome = _send_claimed(state=state, job_id=job_id, kind=_BOOKWALKER_KIND, payload=value,
                                    identity=job_id, cancel=cancel,
                                    send=lambda: discord.deliver(channel, payload, attachments=files, authorization=authorization))
            decisions.append(replace(outcome, candidates=(job_id,)))
            if outcome.status in {"committed", "skipped"}:
                deferred.discard(job_id)
                completed.add(job_id)
    except (StateStoreError, OSError, ValueError, TypeError, HttpError) as error:
        decisions.append(Decision(error.status if isinstance(error, HttpError) else "review_required",
                                  error.reason if isinstance(error, HttpError) else "bookwalker_delivery_journal_unavailable",
                                  candidates=tuple(sorted(deferred))))
    return _settled_result(result, decisions, deferred, completed)

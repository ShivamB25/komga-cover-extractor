from __future__ import annotations

import base64
import io
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast
from zipfile import ZipFile

import pytest
from PIL import Image

from komga_cover_extractor.application.delivery import (
    settle_bookwalker_deliveries,
    settle_committed_deliveries,
)
from komga_cover_extractor.covers.selection import CoverPolicy
from komga_cover_extractor.domain.decisions import Decision, RunAuthorization, RunResult
from komga_cover_extractor.domain.libraries import LibraryRoot, PathMapping
from komga_cover_extractor.domain.operations import (
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.domain.releases import FileObservation, ReleaseSnapshot
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy
from komga_cover_extractor.integrations.discord import DiscordDelivery
from komga_cover_extractor.integrations.http import HttpClient, HttpError
from komga_cover_extractor.matching.ranking import RankingPolicy, RankRule
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release

PARSING = ParsingPolicy(chapter_support_toggle=True)
COVERS = CoverPolicy(ArchivePolicy(), chapter_support_toggle=True, extract_chapter_covers=True,
                     output_covers_as_webp=True)


class RecordingDiscord(DiscordDelivery):
    def __init__(self, *, normal: int = 1, new_release: bool = True, bookwalker: bool = False) -> None:
        super().__init__(tuple(f"https://discord.invalid/api/webhooks/{index}/fictional-token-{index}" for index in range(normal)),
                         new_release_webhook="https://discord.invalid/api/webhooks/99/fictional-new-token" if new_release else "",
                         bookwalker_webhooks=("https://discord.invalid/api/webhooks/100/fictional-released-token",
                                              "https://discord.invalid/api/webhooks/101/fictional-preorder-token") if bookwalker else ())
        self.calls: list[tuple[str, dict[str, object], tuple[tuple[str, bytes, str], ...]]] = []
        self.outcomes: deque[Decision] = deque()
        self.entered: Event | None = None
        self.release: Event | None = None

    def deliver(self, channel_key, payload, *, authorization, attachments=()) -> Decision:
        assert authorization.allow_remote is True
        self.calls.append((channel_key, json.loads(json.dumps(payload)), tuple(attachments)))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(10), "test dispatcher was not released"
        return self.outcomes.popleft() if self.outcomes else Decision("committed", "discord_delivered")


class KomgaFixture:
    def __init__(self, library_root: str) -> None:
        self.libraries = [{"id": "manga", "name": "Manga", "root": library_root}]
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.scan_outcome: HttpError | None = None
        self.discovery_outcome: HttpError | None = None

    def request(self, method: str, relative: str, **kwargs: object):
        self.calls.append((method, relative, kwargs))
        if method == "GET" and self.discovery_outcome is not None:
            raise self.discovery_outcome
        if method == "POST" and self.scan_outcome is not None:
            raise self.scan_outcome
        owner = self

        class Response:
            def json(self):
                return owner.libraries

            def close(self):
                pass

        return Response()

    @property
    def http(self) -> HttpClient:
        return cast(HttpClient, self)


def roots(tmp_path: Path) -> tuple[LibraryRoot, ...]:
    library, download = tmp_path / "library", tmp_path / "download"
    library.mkdir()
    download.mkdir()
    return LibraryRoot(library), LibraryRoot(download, roles=("download",))


def book(root: LibraryRoot, name: str, color: str = "red", *, invalid_image: bool = False) -> ReleaseSnapshot:
    path = root.path / "Series" / name
    path.parent.mkdir(exist_ok=True)
    with io.BytesIO() as buffer:
        with Image.new("RGB", (16, 16), color) as image:
            image.save(buffer, format="PNG")
        encoded = buffer.getvalue()
    with ZipFile(path, "w") as archive:
        archive.writestr("01.png", b"not an image" if invalid_image else encoded)
        with io.BytesIO() as buffer:
            with Image.new("RGB", (16, 16), "blue") as image:
                image.save(buffer, format="PNG")
            archive.writestr("cover.png", buffer.getvalue())
    role = "library" if "library" in root.roles else "download"
    return parse_release(FileObservation(path, root_role=role, fingerprint=fingerprint(path, root.path)), PARSING)


def commit_moves(
    state: StateStore, configured: tuple[LibraryRoot, ...], sources: tuple[ReleaseSnapshot, ...],
    destinations: tuple[Path, ...], *, operation_id: str = "observed-moves", reason: str = "organization",
) -> RunResult:
    for destination in destinations:
        destination.parent.mkdir(exist_ok=True)
    plan = OperationPlan("delivery-tests-v1", operation_id,
                         tuple(Operation(f"book-{index}", "move", destination, source=source.observation.fingerprint, reason=reason)
                               for index, (source, destination) in enumerate(zip(sources, destinations, strict=True))),
                         tuple(RootPermission(root.path) for root in configured))
    result = TransactionExecutor(state.state_dir, tuple(root.path for root in configured)).apply(
        plan, authorization=RunAuthorization(apply=True))
    assert result.committed == 1 and result.failed == 0
    releases = tuple(parse_release(FileObservation(destination, root_role="library",
                                                   fingerprint=fingerprint(destination, next(root.path for root in configured if root.contains(destination)))),
                                   PARSING) for destination in destinations)
    return replace(result, releases=releases)


def settle(result: RunResult, state: StateStore, configured: tuple[LibraryRoot, ...], *, discord=None,
           komga=None, before=(), authorization=RunAuthorization(apply=True, allow_remote=True),
           cancel=None, chapter_covers=False, ranking=RankingPolicy(), mute_rename=False, mappings=()) -> RunResult:
    return settle_committed_deliveries(result, state=state, roots=configured, parsing=PARSING, ranking=ranking,
                                      cover_policy=COVERS, discord=discord, komga=komga, mappings=mappings,
                                      authorization=authorization, cancel=cancel or Event(), before=before,
                                      chapter_covers=chapter_covers, mute_rename=mute_rename, version="v2.5.39")


def manifest(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def discord_rows(state: StateStore):
    return tuple(event for event in state.list_outbox() if event.event_type.startswith("delivery_discord_"))


def test_native_preview_does_not_open_or_create_delivery_journal(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[1], "Series v01.cbz")
    state = StateStore(tmp_path / "state")
    result = RunResult((Decision("planned", "new_release", source.path),), releases=(source,))
    with RecordingDiscord() as discord:
        observed = settle(result, state, configured, discord=discord, before=(source,), authorization=RunAuthorization())
        assert observed == result
        assert discord.calls == []
    assert not state.state_dir.exists()
    assert source.path.read_bytes().startswith(b"PK")


def test_unauthorized_delivery_queues_real_cover_bytes_and_discovery_without_http_then_resumes(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[1], "Series v01.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (configured[0].path / "Series" / source.name,))
    files = manifest(configured[0].path)
    komga = KomgaFixture("/remote/books")
    with RecordingDiscord() as discord:
        queued = settle(result, state, configured, discord=discord, komga=komga.http, before=(source,),
                        authorization=RunAuthorization(apply=True), mappings=(PathMapping(configured[0].path, "/remote/books"),))
        rows = discord_rows(state)
        assert (queued.committed, queued.failed, queued.skipped) == (1, 0, 0)
        assert len(rows) == 2 and {row.state for row in rows} == {"queued"}
        assert set(queued.deferred_deliveries) == {row.event_id for row in state.list_outbox() if row.state != "complete"}
        assert discord.calls == [] and komga.calls == []
        new = next(row for row in rows if row.payload["channel_key"] == "discord/new-release")
        embed = new.payload["payload"]["embeds"][0]
        assert (embed["title"], embed["footer"], embed["image"]) == (
            "New Volume(s) Added", {"text": "v2.5.39"}, {"url": "attachment://cover.webp"})
        assert embed["fields"] == [
            {"name": "Volume Number(s)", "value": "```1```", "inline": False},
            {"name": "Volume Name(s)", "value": "```Series v01.cbz```", "inline": False},
        ]
        stored = new.payload["attachments"][0]
        data = base64.b64decode(stored["data_base64"])
        assert (stored["filename"], stored["media_type"]) == ("cover.webp", "image/webp")
        with Image.open(io.BytesIO(data)) as image:
            pixel = image.convert("RGB").getpixel((0, 0))
            assert (image.format, image.size) == ("WEBP", (16, 16))
            assert pixel[0] > 240 and pixel[1] < 12 and pixel[2] < 12
        delivered = settle(queued, state, configured, discord=discord, komga=komga.http,
                           mappings=(PathMapping(configured[0].path, "/remote/books"),))
        assert (delivered.committed, delivered.failed, delivered.skipped, delivered.deferred_deliveries) == (1, 0, 0, ())
        assert delivered.exit_code == 0
        assert {call[0] for call in discord.calls} == {"discord/normal/0", "discord/new-release"}
        assert next(call[2] for call in discord.calls if call[0] == "discord/new-release") == (("cover.webp", data, "image/webp"),)
        assert [call[:2] for call in komga.calls] == [("GET", "api/v1/libraries"), ("POST", "api/v1/libraries/manga/scan")]
        replay = settle(delivered, state, configured, discord=discord, komga=komga.http)
        assert replay.deferred_deliveries == () and len(discord.calls) == 2 and len(komga.calls) == 2
    assert manifest(configured[0].path) == files
    assert {row.state for row in state.list_outbox()} == {"complete"}
    assert "fictional" not in state.path.read_bytes().decode("latin1")


@pytest.mark.parametrize(("outcome", "expected_state"), [
    (Decision("permission_denied", "http_403"), "failed"),
    (Decision("review_required", "discord_delivery_pending_unknown"), "unknown"),
])
def test_remote_failure_preserves_local_counts_and_never_replays_unknown_or_failed_post(tmp_path: Path, outcome: Decision, expected_state: str) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    result = replace(result, skipped=4, failed=2)
    files = manifest(configured[0].path)
    with RecordingDiscord(new_release=False) as discord:
        discord.outcomes.append(outcome)
        failed = settle(result, state, configured, discord=discord, before=(source,))
        row, = discord_rows(state)
        assert (failed.committed, failed.skipped, failed.failed) == (1, 4, 2)
        assert (row.state, row.attempts, row.error) == (expected_state, 1, outcome.reason)
        assert failed.deferred_deliveries == (row.event_id,)
        assert failed.decisions[-1].status == outcome.status
        replay = settle(failed, state, configured, discord=discord)
        assert replay.deferred_deliveries == (row.event_id,) and len(discord.calls) == 1
    assert manifest(configured[0].path) == files


def test_explicit_rate_limit_rejection_retries_same_durable_batch_once_next_invocation(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    with RecordingDiscord(new_release=False) as discord:
        discord.outcomes.append(Decision("delivery_deferred", "http_rate_limited"))
        pending = settle(result, state, configured, discord=discord, before=(source,))
        row, = discord_rows(state)
        assert (row.state, row.attempts, pending.committed) == ("pending", 1, 1)
        completed = settle(pending, state, configured, discord=discord)
        row, = discord_rows(state)
        assert (row.state, row.attempts, completed.deferred_deliveries) == ("complete", 2, ())
        assert completed.exit_code == 0
        assert discord.calls[0] == discord.calls[1]
        assert settle(completed, state, configured, discord=discord).committed == 1
        assert len(discord.calls) == 2


def test_atomic_dispatch_claim_does_not_send_twice_or_settle_another_running_claim(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    with RecordingDiscord(new_release=False) as discord, ThreadPoolExecutor(max_workers=2) as pool:
        discord.entered, discord.release = Event(), Event()
        first = pool.submit(settle, result, state, configured, discord=discord, before=(source,))
        assert discord.entered.wait(10)
        second = pool.submit(settle, result, StateStore(state.state_dir), configured, discord=discord).result(10)
        row, = discord_rows(state)
        job = state.get_job("delivery-" + row.event_id)
        assert (row.state, row.attempts, job.state) == ("queued", 0, "running")
        assert second.deferred_deliveries == (row.event_id,)
        assert second.decisions[-1].reason == "delivery_claim_unsettled"
        assert len(discord.calls) == 1
        discord.release.set()
        completed = first.result(10)
        assert completed.deferred_deliveries == ()
        assert state.get_job(job.job_id).state == "complete"


def test_disabled_delivery_settles_local_audit_without_remote_or_fabricated_counts(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    result = replace(result, deferred_deliveries=tuple(row.event_id for row in state.list_outbox()))
    observed = settle(result, state, configured)
    assert (observed.committed, observed.failed, observed.deferred_deliveries, observed.exit_code) == (1, 0, (), 0)
    assert {row.state for row in state.list_outbox()} == {"complete"}
    assert discord_rows(state) == ()


def test_root_scope_change_refuses_delivery_before_claim_or_network(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    adjacent = configured[0].path.with_name("library-old")
    adjacent.mkdir()
    with RecordingDiscord() as discord:
        blocked = settle(result, state, (LibraryRoot(adjacent), configured[1]), discord=discord)
        assert blocked.committed == 1 and blocked.decisions[-1].reason == "delivery_root_scope_changed"
        assert set(blocked.deferred_deliveries) == {row.event_id for row in state.list_outbox()}
        assert discord.calls == []
    assert state.list_jobs() == () and {row.state for row in state.list_outbox()} == {"queued"}


def test_source_symlink_and_stale_published_bytes_do_not_generate_cover_or_success(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[1], "Series v01.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (configured[0].path / "Series" / source.name,))
    published = result.releases[0].path
    published.write_bytes(b"external replacement")
    with RecordingDiscord() as discord:
        blocked = settle(result, state, configured, discord=discord, before=(source,))
        assert blocked.committed == 1 and blocked.decisions[-1].reason == "delivery_published_bytes_changed"
        assert blocked.deferred_deliveries and discord.calls == []
        assert published.read_bytes() == b"external replacement"
    assert {row.state for row in state.list_outbox()} == {"queued"}
    assert state.get_job("delivery-prepare-observed-moves").state == "needs_review"


def test_linked_source_parent_refuses_future_network_delivery(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[1], "Series v01.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (configured[0].path / "Series" / source.name,))
    outside = tmp_path / "unrelated"
    outside.mkdir()
    source.parent.rename(outside / "moved-parent")
    source.parent.symlink_to(outside / "moved-parent", target_is_directory=True)
    with RecordingDiscord() as discord:
        blocked = settle(result, state, configured, discord=discord, before=(source,))
        assert (blocked.committed, blocked.exit_code) == (1, 3)
        assert blocked.decisions[-1].reason == "delivery_journal_or_scope_unavailable"
        assert discord.calls == []
    assert state.list_jobs() == ()


def test_metadata_path_does_not_load_committed_artifact_buffers_to_format_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    def unexpected(*args, **kwargs):
        pytest.fail("byte-free committed metadata should format this notification")
    monkeypatch.setattr(state, "get_operation", unexpected)
    monkeypatch.setattr(state, "list_members", unexpected)
    with RecordingDiscord(new_release=False) as discord:
        observed = settle(result, state, configured, discord=discord, before=(source,))
        assert observed.deferred_deliveries == ()
        assert discord.calls[0][1]["embeds"][0]["fields"] == [
            {"name": "From", "value": "```Series v1.cbz```", "inline": False},
            {"name": "To", "value": "```Series v01.cbz```", "inline": False},
        ]


def test_batches_advance_real_round_robin_and_do_not_drop_eleventh_rename(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    sources = tuple(book(configured[0], f"Series v{index}.cbz") for index in range(1, 12))
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, sources, tuple(source.path.with_name(f"Series Volume {index:02}.cbz") for index, source in enumerate(sources, 1)))
    with RecordingDiscord(normal=2, new_release=False) as discord:
        observed = settle(result, state, configured, discord=discord, before=sources)
        assert (observed.committed, observed.deferred_deliveries) == (1, ())
        ordered = sorted(discord.calls, key=lambda call: call[0])
        assert [(call[0], len(call[1]["embeds"]), call[2]) for call in ordered] == [
            ("discord/normal/0", 10, ()), ("discord/normal/1", 1, ()),
        ]
        assert [embed["fields"][0]["value"] for call in ordered for embed in call[1]["embeds"]] == [
            f"```Series v{index}.cbz```" for index in range(1, 12)
        ]


@pytest.mark.parametrize("include_covers", [False, True])
def test_grouped_chapters_keep_zero_numeric_order_routing_and_optional_actual_covers(tmp_path: Path, include_covers: bool) -> None:
    configured = roots(tmp_path)
    sources = tuple(book(configured[1], name, color) for name, color in (
        ("Series c003.cbz", "blue"), ("Series c000.cbz", "red"), ("Series c001.cbz", "green"),
    ))
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, sources, tuple(configured[0].path / "Series" / source.name for source in sources))
    with RecordingDiscord() as discord:
        observed = settle(result, state, configured, discord=discord, before=sources, chapter_covers=include_covers)
        assert (observed.committed, observed.deferred_deliveries) == (1, ())
        new, = [call for call in discord.calls if call[0] == "discord/new-release"]
        if include_covers:
            assert [embed["fields"][1]["value"] for embed in new[1]["embeds"]] == [
                "```Series c000.cbz```", "```Series c001.cbz```", "```Series c003.cbz```",
            ]
            assert [(name, media) for name, _, media in new[2]] == [
                ("cover_1.webp", "image/webp"), ("cover_2.webp", "image/webp"), ("cover_3.webp", "image/webp"),
            ]
            for (_, data, _), channel in zip(new[2], (0, 1, 2), strict=True):
                with Image.open(io.BytesIO(data)) as image:
                    pixel = image.convert("RGB").getpixel((0, 0))
                    assert image.format == "WEBP" and pixel[channel] > 110
                    assert all(pixel[index] < 12 for index in range(3) if index != channel)
        else:
            assert new[2] == ()
            assert new[1]["embeds"][0]["fields"] == [
                {"name": "Series Name", "value": "```Series```", "inline": False},
                {"name": "Chapter Number(s)", "value": "```0-1, 3```", "inline": False},
                {"name": "Chapter Name(s)", "value": "```Series c000.cbz\nSeries c001.cbz\nSeries c003.cbz```", "inline": False},
            ]


@pytest.mark.parametrize("include_covers", [False, True])
def test_normal_only_chapter_notifications_keep_individual_fields_and_cover_policy(
    tmp_path: Path, include_covers: bool,
) -> None:
    configured = roots(tmp_path)
    sources = (book(configured[1], "Series c003.cbz", "blue"), book(configured[1], "Series c002.cbz", "red"))
    originals = {source.name: source.path.read_bytes() for source in sources}
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, sources, tuple(configured[0].path / "Series" / source.name for source in sources))
    with RecordingDiscord(new_release=False) as discord:
        observed = settle(result, state, configured, discord=discord, before=sources, chapter_covers=include_covers)
        chapters = [embed for _, payload, _ in discord.calls for embed in payload["embeds"]
                    if embed["title"] == "New Chapter(s) Added"]
        assert (observed.committed, observed.failed, observed.deferred_deliveries, observed.exit_code) == (1, 0, (), 0)
        assert [embed["fields"] for embed in chapters] == [
            [{"name": "Chapter Number(s)", "value": "```2```", "inline": False},
             {"name": "Chapter Name(s)", "value": "```Series c002.cbz```", "inline": False}],
            [{"name": "Chapter Number(s)", "value": "```3```", "inline": False},
             {"name": "Chapter Name(s)", "value": "```Series c003.cbz```", "inline": False}],
        ]
        assert {channel for channel, _, _ in discord.calls} == {"discord/normal/0"}
        files = [attachment for _, _, attachments in discord.calls for attachment in attachments]
        if include_covers:
            assert [(name, media) for name, _, media in files] == [
                ("cover_1.webp", "image/webp"), ("cover_2.webp", "image/webp"),
            ]
            for (_, data, _), channel in zip(files, (0, 2), strict=True):
                with Image.open(io.BytesIO(data)) as image:
                    pixel = image.convert("RGB").getpixel((0, 0))
                    assert (image.format, image.size) == ("WEBP", (16, 16))
                    assert pixel[channel] > 240 and all(pixel[index] < 12 for index in range(3) if index != channel)
        else:
            assert files == []
            assert all("image" not in embed for embed in chapters)
    assert {source.name: (configured[0].path / "Series" / source.name).read_bytes() for source in sources} == originals


def test_upgrade_uses_observed_scores_tags_sizes_and_does_not_announce_new_volume(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    existing = book(configured[0], "Series v01 [Scan].cbz")
    incoming = book(configured[1], "Series v01 [Digital].cbz", "green")
    state = StateStore(tmp_path / "state")
    target = existing.parent / incoming.name
    plan = OperationPlan("delivery-tests-v1", "strict-upgrade", (
        Operation("book", "move", target, source=incoming.observation.fingerprint, reason="organization"),
        Operation("old-book", "quarantine", existing.path, source=existing.observation.fingerprint,
                  depends_on=("book",), reason="complete_collection_or_release_upgrade"),
    ), tuple(RootPermission(root.path) for root in configured))
    result = TransactionExecutor(state.state_dir, tuple(root.path for root in configured)).apply(plan, authorization=RunAuthorization(apply=True))
    after = parse_release(FileObservation(target, root_role="library", fingerprint=fingerprint(target, configured[0].path)), PARSING)
    result = replace(result, releases=(after,))
    with RecordingDiscord() as discord:
        observed = settle(result, state, configured, discord=discord, before=(existing, incoming),
                          ranking=RankingPolicy((RankRule("Scan", -1), RankRule("Digital", 3))))
        assert (observed.committed, observed.deferred_deliveries) == (1, ())
        embeds = [embed for _, payload, _ in discord.calls for embed in payload["embeds"]]
        upgrade, = [embed for embed in embeds if embed["title"] == "Upgrade Process (Upgrade)"]
        assert upgrade["fields"] == [
            {"name": "From", "value": "```Series v01 [Scan].cbz```", "inline": False},
            {"name": "Score", "value": "-1", "inline": True},
            {"name": "Tags", "value": "Scan (-1)", "inline": True},
            {"name": "Size", "value": "0.0 MB", "inline": True},
            {"name": "To", "value": "```Series v01 [Digital].cbz```", "inline": False},
            {"name": "Score", "value": "3", "inline": True},
            {"name": "Tags", "value": "Digital (3)", "inline": True},
            {"name": "Size", "value": "0.0 MB", "inline": True},
        ]
        assert not any(embed["title"] == "New Volume(s) Added" for embed in embeds)
        assert "Removed File" in [embed["title"] for embed in embeds]


def test_komga_unknown_acceptance_is_retained_and_not_automatically_rescanned(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    komga = KomgaFixture(configured[0].path.as_posix())
    komga.scan_outcome = HttpError("delivery_deferred", "http_transport_failed", pending_unknown=True)
    pending = settle(result, state, configured, komga=komga.http)
    scan, = [row for row in state.list_outbox() if row.event_type.startswith("delivery_komga_scan_")]
    assert (scan.state, scan.error, pending.committed, pending.deferred_deliveries) == (
        "unknown", "komga_scan_pending_unknown", 1, (scan.event_id,),
    )
    komga.scan_outcome = None
    replay = settle(pending, state, configured, komga=komga.http)
    assert replay.deferred_deliveries == (scan.event_id,)
    assert [call[:2] for call in komga.calls] == [("GET", "api/v1/libraries"), ("POST", "api/v1/libraries/manga/scan")]


def test_bad_cover_does_not_turn_into_original_byte_success_or_repeat_local_mutation(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[1], "Series v01.cbz", invalid_image=True)
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (configured[0].path / "Series" / source.name,))
    files = manifest(configured[0].path)
    with RecordingDiscord() as discord:
        blocked = settle(result, state, configured, discord=discord, before=(source,))
        assert (blocked.committed, blocked.failed, blocked.exit_code) == (1, 0, 2)
        assert blocked.decisions[-1].status == "invalid_input"
        assert blocked.decisions[-1].reason.startswith("notification_cover_")
        assert discord.calls == []
        assert state.get_job("delivery-prepare-observed-moves").state == "needs_review"
    assert manifest(configured[0].path) == files
    assert {row.state for row in state.list_outbox()} == {"queued"}


def report(*, url: str = "https://global.bookwalker.jp/book-2/", preorder: bool = False,
           complete: bool = True, date_status: str = "known", price: str | None = "6.99") -> RunResult:
    item = {"title": "Series", "volume_number": "2", "part": 12, "premium": True, "book_type": "Light Novel",
            "url": url, "date": "2026-12-25" if preorder else "2026-10-13", "date_status": date_status,
            "release_state": "preorder" if preorder else "released", "price": price, "price_status": "known" if price is not None else "unknown",
            "description": "Café story", "preview_image_url": "https://images.invalid/cover.png"}
    return RunResult((Decision("observed", "bookwalker_new_release", candidates=(url,)),), committed=2, skipped=3,
                     missing_releases=({"source": "bookwalker", "complete": complete,
                                        "comparisons": [{"status": "missing", "book": item}]},))


def bookwalker(result: RunResult, state: StateStore, discord: RecordingDiscord, *, ready=None,
               authorization=RunAuthorization(allow_remote=True), cancel=None) -> RunResult:
    def initialize() -> Decision:
        state.initialize()
        return Decision("observed", "journal_ready")
    return settle_bookwalker_deliveries(result, state=state, discord=discord, authorization=authorization,
                                       cancel=cancel or Event(), journal_ready=ready or initialize, version="v2.5.39")


def test_bookwalker_preview_unknown_date_and_incomplete_search_never_initialize_a_fake_file_operation(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    callbacks = []
    def ready():
        callbacks.append(True)
        return Decision("observed", "journal_ready")
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord:
        preview = bookwalker(report(), state, discord, ready=ready, authorization=RunAuthorization())
        assert (preview.committed, preview.skipped, preview.decisions[-1].status) == (2, 3, "planned")
        for result in (report(complete=False), report(date_status="unknown")):
            assert bookwalker(result, state, discord, ready=ready) == result
        assert discord.calls == [] and callbacks == []
    assert not state.state_dir.exists()


@pytest.mark.parametrize("failure", [Decision("permission_denied", "journal_not_writable"), Decision("cancelled", "journal_cancelled")])
def test_bookwalker_requires_actual_observed_journal_preparation_result(tmp_path: Path, failure: Decision) -> None:
    state = StateStore(tmp_path / "state")
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord:
        result = bookwalker(report(), state, discord, ready=lambda: failure)
        assert (result.committed, result.skipped, result.failed, result.decisions[-1]) == (2, 3, 0, failure)
        assert discord.calls == []
    assert not state.state_dir.exists()


def test_bookwalker_identity_is_exact_url_and_channel_not_price_date_or_current_credentials(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    callbacks = []
    def ready():
        callbacks.append(True)
        state.initialize()
        return Decision("observed", "journal_ready")
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord:
        first = bookwalker(report(preorder=True, price=None), state, discord, ready=ready)
        assert (first.committed, first.skipped, first.failed, first.deferred_deliveries) == (2, 3, 0, ())
        assert discord.calls[0][0] == "discord/bookwalker/preorder"
        embed = discord.calls[0][1]["embeds"][0]
        assert (embed["title"], embed["fields"]) == (
            "Series Volume 2 Part 12 [Premium]", [
                {"name": "Type", "value": "Light Novel", "inline": False},
                {"name": "Release Date", "value": "2026-12-25", "inline": False},
                {"name": "Description", "value": "Cafe story", "inline": False},
            ],
        )
        second = bookwalker(report(preorder=True, price="1.99"), state, discord, ready=ready)
        assert second.deferred_deliveries == () and len(discord.calls) == 1 and callbacks == [True]
        released = bookwalker(report(), state, discord, ready=ready)
        assert released.deferred_deliveries == ()
        assert [call[0] for call in discord.calls] == ["discord/bookwalker/preorder", "discord/bookwalker/released"]
        assert {job.state for job in state.list_jobs()} == {"complete"}
        assert len(state.list_jobs()) == 2 and state.list_operations() == () and state.list_outbox() == ()
        assert "fictional" not in state.path.read_bytes().decode("latin1")


def test_bookwalker_explicit_rejection_retries_stored_payload_but_unknown_acceptance_never_replays(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord:
        discord.outcomes.append(Decision("delivery_deferred", "http_rate_limited"))
        pending = bookwalker(report(), state, discord)
        job, = state.list_jobs()
        assert (job.state, job.error, pending.deferred_deliveries) == ("pending", "http_rate_limited", (job.job_id,))
        completed = bookwalker(report(price="1.99"), state, discord)
        assert completed.deferred_deliveries == () and discord.calls[0] == discord.calls[1]
        discord.outcomes.append(Decision("review_required", "discord_delivery_pending_unknown"))
        unknown = bookwalker(report(url="https://global.bookwalker.jp/other/"), state, discord)
        unsettled, = state.list_jobs(("needs_review",))
        assert unknown.deferred_deliveries == (unsettled.job_id,)
        replay = bookwalker(report(url="https://global.bookwalker.jp/other/", price="0.00"), state, discord)
        assert replay.deferred_deliveries == (unsettled.job_id,) and len(discord.calls) == 3
        assert replay.decisions[-1].reason == "discord_delivery_pending_unknown"


def test_bookwalker_standalone_atomic_claim_blocks_concurrent_dispatch(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    state.initialize()
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord, ThreadPoolExecutor(max_workers=2) as pool:
        discord.entered, discord.release = Event(), Event()
        first = pool.submit(bookwalker, report(), state, discord, ready=lambda: Decision("observed", "journal_ready"))
        assert discord.entered.wait(10)
        second = pool.submit(bookwalker, report(), StateStore(state.state_dir), discord,
                             ready=lambda: Decision("observed", "journal_ready")).result(10)
        job, = state.list_jobs()
        assert (job.state, len(discord.calls), second.deferred_deliveries) == ("running", 1, (job.job_id,))
        assert second.decisions[-1].reason == "delivery_claim_unsettled"
        discord.release.set()
        assert first.result(10).deferred_deliveries == ()
        assert state.get_job(job.job_id).state == "complete"


@pytest.mark.parametrize(("reason", "mute", "expected_titles"), [
    ("restructure_release", False, ["Reorganized & Renamed File"]),
    ("rename_release", False, ["Renamed File"]),
    ("observed_container_extension", False, ["Renamed File"]),
    ("restructure_release", True, []),
])
def test_semantic_rename_modes_and_mute_do_not_mute_other_delivery(tmp_path: Path, reason: str, mute: bool, expected_titles: list[str]) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),), reason=reason)
    komga = KomgaFixture(configured[0].path.as_posix())
    with RecordingDiscord(new_release=False) as discord:
        observed = settle(result, state, configured, discord=discord, komga=komga.http, before=(source,), mute_rename=mute)
        assert (observed.committed, observed.deferred_deliveries) == (1, ())
        assert [embed["title"] for _, payload, _ in discord.calls for embed in payload["embeds"]] == expected_titles
        assert [call[:2] for call in komga.calls] == [("GET", "api/v1/libraries"), ("POST", "api/v1/libraries/manga/scan")]


def test_cleanup_pending_does_not_report_removed_file_or_block_independent_delivery(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series c001.cbz")
    state = StateStore(tmp_path / "state")
    cancellation = Event()
    def checkpoint(stage: str, member: str) -> None:
        if stage == "group_committed":
            cancellation.set()
    plan = OperationPlan("delivery-tests-v1", "pending-cleanup",
                         (Operation("book", "quarantine", source.path, source=source.observation.fingerprint,
                                    reason="chapter_filter"),),
                         tuple(RootPermission(root.path) for root in configured))
    result = TransactionExecutor(state.state_dir, tuple(root.path for root in configured),
                                 cancel=cancellation, checkpoint=checkpoint).apply(plan, authorization=RunAuthorization(apply=True))
    assert result.committed == 1 and source.path.exists()
    with RecordingDiscord(new_release=False) as discord:
        observed = settle(result, state, configured, discord=discord, before=(source,))
        assert (observed.committed, observed.deferred_deliveries) == (1, ())
        assert [embed["title"] for _, payload, _ in discord.calls for embed in payload["embeds"]] == [
            "Chapter Release Found", "Quarantine Pending",
        ]
        assert not any(embed["title"] == "Removed File" for _, payload, _ in discord.calls for embed in payload["embeds"])
    assert state.get_operation(plan.operation_id).phase == "cleanup_pending" and source.path.exists()


def test_cancelled_preparation_is_pending_and_resumes_without_local_reexecution(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    cancellation = Event()
    cancellation.set()
    with RecordingDiscord(new_release=False) as discord:
        pending = settle(result, state, configured, discord=discord, before=(source,), cancel=cancellation)
        job = state.get_job("delivery-prepare-observed-moves")
        assert (job.state, job.error, pending.committed) == ("pending", "delivery_cancelled_before_send", 1)
        assert pending.deferred_deliveries and discord.calls == []
        completed = settle(pending, state, configured, discord=discord, before=(source,))
        assert (completed.committed, completed.deferred_deliveries, len(discord.calls)) == (1, (), 1)


def test_preparation_running_claim_is_not_stolen_after_an_interrupted_owner(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    events = state.list_outbox()
    payload = {"schema_version": 1, "operation_id": "observed-moves",
               "scope": {"roots": [str(configured[0].path)],
                         "paths": sorted([str(source.path), str(result.releases[0].path)])}}
    assert state.claim_job("delivery-prepare-observed-moves", "delivery_preparation", payload) is True
    with RecordingDiscord(new_release=False) as discord:
        blocked = settle(result, StateStore(state.state_dir), configured, discord=discord, before=(source,))
        assert blocked.deferred_deliveries == tuple(sorted(event.event_id for event in events))
        assert blocked.decisions[-1].reason == "delivery_preparation_unsettled"
        assert discord.calls == []
    assert state.get_job("delivery-prepare-observed-moves").state == "running"
    assert {row.state for row in state.list_outbox()} == {"queued"}


@pytest.mark.parametrize("bad_value", [None, "not-a-date", "2026-99-99"])
def test_bookwalker_malformed_known_date_is_not_a_notification_or_journal_request(tmp_path: Path, bad_value: object) -> None:
    state = StateStore(tmp_path / "state")
    result = report()
    result.missing_releases[0]["comparisons"][0]["book"]["date"] = bad_value
    callbacks = []
    def ready():
        callbacks.append(True)
        return Decision("observed", "journal_ready")
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord:
        blocked = bookwalker(result, state, discord, ready=ready)
        assert (blocked.committed, blocked.skipped, blocked.decisions[-1].status, blocked.decisions[-1].reason) == (
            2, 3, "invalid_input", "bookwalker_notification_date_invalid",
        )
        assert callbacks == [] and discord.calls == []
    assert not state.state_dir.exists()


def test_deferred_ids_settled_by_another_instance_do_not_remain_false_pending_work(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    with RecordingDiscord(new_release=False) as discord:
        queued = settle(result, state, configured, discord=discord, before=(source,),
                        authorization=RunAuthorization(apply=True))
        row, = discord_rows(state)
        assert queued.deferred_deliveries == (row.event_id,)
        settled = settle(result, StateStore(state.state_dir), configured, discord=discord)
        assert settled.deferred_deliveries == () and state.outbox_state(row.event_id) == "complete"
        reconciled = settle(queued, state, configured, discord=discord)
        assert reconciled.deferred_deliveries == () and len(discord.calls) == 1
        assert reconciled.exit_code == 0
    with RecordingDiscord(normal=0, new_release=False, bookwalker=True) as discord:
        discord.outcomes.append(Decision("delivery_deferred", "http_rate_limited"))
        pending = bookwalker(report(), state, discord)
        assert pending.deferred_deliveries
        assert bookwalker(report(), StateStore(state.state_dir), discord).deferred_deliveries == ()
        observed = bookwalker(replace(pending, missing_releases=()), state, discord)
        assert observed.deferred_deliveries == () and len(discord.calls) == 2
        assert observed.exit_code == 0


@pytest.mark.parametrize("failure", [HttpError("permission_denied", "http_401"), HttpError("invalid_input", "invalid_komga_library_schema")])
def test_permanent_komga_discovery_failure_is_not_empty_success_or_retried_in_loop(tmp_path: Path, failure: HttpError) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    komga = KomgaFixture(configured[0].path.as_posix())
    komga.discovery_outcome = failure
    failed = settle(result, state, configured, komga=komga.http)
    discovery, = [row for row in state.list_outbox() if row.event_type == "delivery_komga_discovery"]
    assert (discovery.state, discovery.error, failed.committed) == ("failed", failure.reason, 1)
    assert failed.decisions[-1].status == failure.status
    assert failed.deferred_deliveries == (discovery.event_id,)
    komga.discovery_outcome = None
    deferred = settle(failed, state, configured, komga=komga.http)
    assert deferred.deferred_deliveries == (discovery.event_id,)
    assert [call[:2] for call in komga.calls] == [("GET", "api/v1/libraries")]


def test_routing_scan_coalesces_every_changed_member_for_both_component_mapped_libraries(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    other_path = tmp_path / "other-library"
    other_path.mkdir()
    configured += (LibraryRoot(other_path),)
    sources = (book(configured[0], "Series v01.cbz"), book(configured[0], "Series v02.cbz", "green"))
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, sources, tuple(other_path / "Series" / source.name for source in sources), reason="directory_route")
    komga = KomgaFixture("/books")
    komga.libraries = [
        {"id": "old", "name": "Old", "root": "/books"},
        {"id": "new", "name": "New", "root": "/books-old"},
    ]
    observed = settle(result, state, configured, komga=komga.http, before=sources,
                      mappings=(PathMapping(configured[0].path, "/books"), PathMapping(other_path, "/books-old")))
    assert (observed.committed, observed.deferred_deliveries) == (1, ())
    assert [call[:2] for call in komga.calls if call[0] == "GET"] == [("GET", "api/v1/libraries")]
    assert sorted(call[1] for call in komga.calls if call[0] == "POST") == [
        "api/v1/libraries/new/scan", "api/v1/libraries/old/scan",
    ]
    scan_rows = [row for row in state.list_outbox() if row.event_type.startswith("delivery_komga_scan_")]
    assert len(scan_rows) == 2 and {row.state for row in scan_rows} == {"complete"}


@pytest.mark.parametrize(("evidence", "score", "expected_title", "score_field", "threshold"), [
    ("full_name_match", "0.93", "Found Series Match", "Similarity Score", "0.9"),
    ("cover_similarity_match", "0.97", "Found Series Match (Cover Match)", "Image Similarity Score", "0.95"),
    ("cached_cover_similarity_match", "0.98", "Found Series Match (Cover Match)", "Image Similarity Score", "0.95"),
])
def test_matching_modes_use_source_bound_observed_scores_and_actual_thresholds(tmp_path: Path, evidence: str, score: str,
                                                                            expected_title: str, score_field: str, threshold: str) -> None:
    configured = roots(tmp_path)
    existing = book(configured[0], "Series v01.cbz")
    incoming = book(configured[1], "Series v02.cbz", "green")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (incoming,), (existing.parent / incoming.name,), reason="existing_series_new_release")
    result = replace(result, decisions=result.decisions + (Decision("observed", "series_match", incoming.path, (
        "evidence=" + evidence, "series=" + str(existing.parent), "score=" + score,
        "required_similarity_score=0.9", "required_image_similarity_score=0.95", "required_matching_percentage=90",
    )),))
    with RecordingDiscord() as discord:
        observed = settle(result, state, configured, discord=discord, before=(existing, incoming))
        embeds = [embed for _, payload, _ in discord.calls for embed in payload["embeds"]]
        matched, = [embed for embed in embeds if embed["title"] == expected_title]
        assert matched["fields"][0] == {"name": "Existing Series Location", "value": f"```{existing.parent}```", "inline": False}
        assert matched["fields"][-2:] == [
            {"name": score_field, "value": f"```{score}```", "inline": score_field == "Similarity Score"},
            {"name": "Required Score", "value": f"```>= {threshold}```", "inline": True},
        ]
        assert (observed.committed, observed.deferred_deliveries, observed.exit_code) == (1, (), 0)


def test_identifier_mode_preserves_observed_identifier_values_without_numeric_scores(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    existing = replace(book(configured[0], "Series v01.cbz"), identifiers=(("series", "series-id"), ("isbn", "other-isbn")))
    incoming = replace(book(configured[1], "Series v02.cbz"), identifiers=(("isbn", "incoming-isbn"), ("series", "series-id")))
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (incoming,), (existing.parent / incoming.name,), reason="existing_series_new_release")
    result = replace(result, decisions=result.decisions + (
        Decision("observed", "series_match", incoming.path, ("evidence=unique_identifier_match", "series=" + str(existing.parent))),
    ))
    with RecordingDiscord() as discord:
        observed = settle(result, state, configured, discord=discord, before=(existing, incoming))
        embed, = [embed for _, payload, _ in discord.calls for embed in payload["embeds"]
                  if embed["title"] == "Found Series Match (Matching Identifier)"]
        assert embed["fields"] == [
            {"name": "Existing Series Location", "value": f"```{existing.parent}```", "inline": False},
            {"name": "Downloaded File", "value": "```incoming-isbn\nseries-id```", "inline": False},
            {"name": "Existing Library File", "value": "```series-id\nother-isbn```", "inline": False},
        ]
        assert observed.deferred_deliveries == () and observed.committed == 1


def test_projected_snapshot_keeps_observed_part_and_metadata_when_publication_has_new_inode(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    incoming = book(configured[1], "Series v01 Part 12.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (incoming,), (configured[0].path / "Series" / "Unreadable title.cbz",))
    projected = replace(incoming, observation=replace(incoming.observation, path=result.releases[0].path))
    result = replace(result, releases=(projected,))
    with RecordingDiscord() as discord:
        observed = settle(result, state, configured, discord=discord, before=(incoming,))
        new, = [call for call in discord.calls if call[0] == "discord/new-release"]
        assert new[1]["embeds"][0]["fields"] == [
            {"name": "Volume Number(s)", "value": "```1```", "inline": False},
            {"name": "Volume Part", "value": "```12```", "inline": False},
            {"name": "Volume Name(s)", "value": "```Unreadable title.cbz```", "inline": False},
        ]
        assert observed.deferred_deliveries == () and observed.committed == 1


def test_queued_scan_revalidates_current_server_library_scope_before_retry(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    komga = KomgaFixture(configured[0].path.as_posix())
    komga.scan_outcome = HttpError("delivery_deferred", "http_rate_limited")
    pending = settle(result, state, configured, komga=komga.http)
    scan, = [row for row in state.list_outbox() if row.event_type.startswith("delivery_komga_scan_")]
    assert (scan.state, scan.attempts) == ("pending", 1)
    komga.scan_outcome = None
    komga.libraries[0]["root"] = configured[1].path.as_posix()
    blocked = settle(pending, state, configured, komga=komga.http)
    assert blocked.decisions[-1].reason == "komga_scan_scope_changed"
    assert blocked.deferred_deliveries == (scan.event_id,)
    assert [call[:2] for call in komga.calls] == [
        ("GET", "api/v1/libraries"), ("POST", "api/v1/libraries/manga/scan"), ("GET", "api/v1/libraries"),
    ]
    assert state.get_job("delivery-" + scan.event_id).state == "pending"


def test_delivery_receipt_committed_before_outbox_update_reconciles_without_resending(tmp_path: Path) -> None:
    configured = roots(tmp_path)
    source = book(configured[0], "Series v1.cbz")
    state = StateStore(tmp_path / "state")
    result = commit_moves(state, configured, (source,), (source.path.with_name("Series v01.cbz"),))
    with RecordingDiscord(new_release=False) as discord:
        first = settle(result, state, configured, discord=discord, before=(source,))
        row, = discord_rows(state)
        assert first.deferred_deliveries == () and state.get_job("delivery-" + row.event_id).state == "complete"
        state.update_outbox(row.event_id, "queued")
        recovered = settle(result, StateStore(state.state_dir), configured, discord=discord)
        assert (recovered.committed, recovered.skipped, recovered.deferred_deliveries, recovered.exit_code) == (1, 0, (), 0)
        assert len(discord.calls) == 1 and state.outbox_state(row.event_id) == "complete"


@pytest.mark.parametrize("prepared", [False, True])
def test_out_of_scope_operation_does_not_block_selected_root_delivery(tmp_path: Path, prepared: bool) -> None:
    configured = roots(tmp_path)
    outside, selected = configured[0], LibraryRoot(configured[1].path)
    state = StateStore(tmp_path / "state")
    old_source = book(outside, "Series v01.cbz")
    new_source = book(selected, "Series v02.cbz")
    old_result = commit_moves(
        state, (outside,), (old_source,), (outside.path / "Series" / "Series Volume 1.cbz",),
        operation_id="outside-scope",
    )
    new_result = commit_moves(
        state, (selected,), (new_source,), (selected.path / "Series" / "Series Volume 2.cbz",),
        operation_id="selected-scope",
    )
    with RecordingDiscord(new_release=False) as discord:
        if prepared:
            settle(old_result, state, (outside,), discord=discord, before=(old_source,),
                   authorization=RunAuthorization(apply=True))
        result = settle(new_result, state, (selected,), discord=discord, before=(new_source,))
        assert result.committed == 1
        assert len(discord.calls) == 1
        selected_rows = state.list_outbox(operation_id="selected-scope")
        assert all(row.state == "complete" for row in selected_rows)
        outside_rows = state.list_outbox(operation_id="outside-scope")
        assert any(row.state == "queued" for row in outside_rows)
        outside_ids = {row.event_id for row in outside_rows}
        failures = [item for item in result.decisions if item.reason == "delivery_root_scope_changed"]
        assert failures and all(set(item.candidates) <= outside_ids for item in failures)
        repeat = settle(new_result, state, (selected,), discord=discord, before=(new_source,))
        assert repeat.committed == 1 and len(discord.calls) == 1


@pytest.mark.parametrize("channel", ["discord", "komga", "bookwalker"])
def test_lazy_transport_dependency_failure_preserves_local_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str,
) -> None:
    import builtins

    from komga_cover_extractor.application.pipeline import RunRequest
    from komga_cover_extractor.bootstrap import open_pipeline
    from komga_cover_extractor.config import validate_config

    configured = roots(tmp_path)
    source = book(configured[0], "Series v01.cbz")
    original = source.path.read_bytes()
    values = {
        "paths": [str(configured[0].path)], "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"), "manual_rename": False, "log_to_file": False,
        "compress_image_option": False, "compare_detected_cover_to_blank_images": False,
        "bookwalker_check": False, "send_scan_request_to_komga_libraries_toggle": False,
    }
    if channel == "discord":
        values["discord_webhook_url"] = ["https://discord.invalid/api/webhooks/proof/proof"]
    elif channel == "komga":
        values.update(
            check_for_existing_series_toggle=True, send_scan_request_to_komga_libraries_toggle=True,
            komga_ip="http://127.0.0.1:65534/komga", komga_port="",
            komga_login_email="proof", komga_login_password="proof",
        )
    else:
        values.update(
            bookwalker_check=True, generate_release_group_list_toggle=False,
            check_for_missing_volumes_toggle=False, match_through_image_similarity=False,
        )
    config = validate_config(values, base_dir=tmp_path)
    original_import = builtins.__import__

    def missing_requests(name, *args, **kwargs):
        if name == "requests" or name.startswith("requests."):
            raise ImportError("missing optional integrations")
        return original_import(name, *args, **kwargs)

    with open_pipeline(config) as pipeline:
        with monkeypatch.context() as unavailable:
            unavailable.setattr(builtins, "__import__", missing_requests)
            result = pipeline.run(RunRequest(
                "run" if channel == "bookwalker" else "extract",
                books=(source.path,), authorization=RunAuthorization(apply=True),
            ))
            if channel != "bookwalker":
                repeated = pipeline.run(RunRequest(
                    "recover", operation_id=result.plan_reports[0].operation_id,
                    authorization=RunAuthorization(apply=True),
                ))
                assert repeated.committed == 0
                assert any(item.status == "missing_dependency" for item in repeated.decisions)
    assert result.committed == 1
    assert any(item.status == "missing_dependency" and item.reason == "http_requires_integrations"
               for item in result.decisions)
    assert source.path.read_bytes() == original
    assert source.path.with_suffix(".png").is_file()
    events = StateStore(config.state_dir).list_outbox(event_type="local_commit", limit=64)
    expected_state = "complete" if channel == "bookwalker" else "queued"
    assert tuple(event.state for event in events) == (expected_state, expected_state)
    assert result.failed == 0 and len(result.plan_reports) == 1

"""Pure torrent selection and one-attempt, journaled remote effects."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from functools import lru_cache
from pathlib import Path, PurePosixPath
from threading import Event
from typing import cast

import regex

from ..domain.decisions import Decision, RunAuthorization, RunResult
from ..domain.decisions import Status as DecisionStatus
from ..domain.libraries import LibrarySnapshot
from ..domain.releases import FileObservation, ReleaseNumber, ReleaseSnapshot
from ..infrastructure.state import JobRecord, StateStore, StateStoreError
from ..integrations.http import HttpClient, HttpError
from ..integrations.qbittorrent import (
    authenticate,
    parse_webapi_version,
    read_files,
    read_torrents,
    remove_torrent,
    set_file_priority,
    start_torrent,
)
from ..matching.ranking import RankingPolicy, decide_upgrade
from ..matching.series import MatchingPolicy, match_series
from ..parsing.filenames import ParsingPolicy, parse_release

OPTIONS = (
    "qbittorrent_username", "qbittorrent_password", "qbittorrent_target_category",
    "qbit_target_states", "qbit_poll_interval_seconds", "qbit_reconnect_interval_seconds",
    "unacceptable_keywords", "delete_unacceptable_files_toggle",
    "delete_unacceptable_torrent_titles_in_qbit", "check_for_existing_series_toggle",
    "uncheck_non_qbit_upgrades_toggle",
)
_BOOK_EXTENSIONS = frozenset({".cbz", ".zip", ".epub"})
_CONVERTIBLE_EXTENSIONS = frozenset({".7z", ".rar", ".cbr"})
_STARTED_STATES = frozenset({
    "downloading", "metaDL", "forcedDL", "stalledDL", "queuedDL", "checkingDL",
    "uploading", "forcedUP", "stalledUP", "queuedUP", "checkingUP",
})
_JOB_KIND = "qbit_action"
_JOB_PAGE_SIZE = 256


@lru_cache(maxsize=4096)
def _unacceptable_patterns(keywords: tuple[str, ...]) -> tuple[regex.Pattern[str], ...]:
    return tuple(regex.compile(keyword, regex.IGNORECASE) for keyword in keywords)


def _states(options: Mapping[str, object]) -> tuple[str, ...]:
    return cast(tuple[str, ...], options.get("qbit_target_states", ("pausedDL", "stoppedDL")))


def _identity(torrent: dict[str, object], file: dict[str, object]) -> tuple[str, ...]:
    return str(torrent["hash"]), str(file["name"]), str(file["index"])


def _decision(
    torrent: dict[str, object], file: dict[str, object], status: DecisionStatus, reason: str
) -> Decision:
    return Decision(status, reason, Path(str(file["name"])), _identity(torrent, file))


def _numbers_overlap(left: ReleaseNumber, right: ReleaseNumber) -> bool:
    if left.kind == "missing" or right.kind == "missing" or left.part != right.part:
        return False
    if left.kind == right.kind == "range":
        return left.values[0] <= right.values[1] and right.values[0] <= left.values[1]
    return any(left.contains(value) for value in right.values) or any(
        right.contains(value) for value in left.values
    )


def _incoming_release(
    torrent: dict[str, object], file: dict[str, object], parsing: ParsingPolicy,
) -> ReleaseSnapshot:
    relative = PurePosixPath(str(file["name"]))
    # The add-on parses each remote name in isolation, not as a local directory.
    observation = FileObservation(Path(str(relative)), siblings=(Path(str(relative)),),
                                  root_role="download")
    if relative.suffix.casefold() in _CONVERTIBLE_EXTENSIONS:
        observation = replace(observation, path=observation.path.with_suffix(".cbz"))
    incoming = parse_release(observation, parsing)
    if not incoming.display_series and len(relative.parts) == 1:
        title = str(torrent["name"]).replace("/", " ").replace("\\", " ")
        title_release = parse_release(
            FileObservation(Path(title + observation.path.suffix), root_role="download",
                            at_configured_root=True), parsing,
        )
        incoming = replace(incoming, display_series=title_release.display_series,
                           canonical_series_key=title_release.canonical_series_key)
    return incoming


def _release_choice(
    incoming: ReleaseSnapshot, library: LibrarySnapshot, matching: MatchingPolicy,
    ranking: RankingPolicy,
) -> tuple[DecisionStatus, str]:
    if incoming.number.kind == "missing" or not incoming.display_series:
        return "review_required", "qbit_release_identity_unknown"
    matched = match_series(incoming, library, matching)
    if matched.status == "no_match":
        return "observed", "qbit_keep_new_series"
    if matched.status != "matched" or matched.selected is None:
        status: DecisionStatus = "review_required" if matched.status == "ambiguous" else cast(DecisionStatus, matched.status)
        return status, "qbit_" + matched.reason
    existing = tuple(item for item in matched.selected.releases if item.kind == incoming.kind)
    if any(item.number.kind == "missing" for item in existing):
        return "review_required", "qbit_existing_release_identity_unknown"
    overlaps = tuple(item for item in existing if _numbers_overlap(incoming.number, item.number))
    if not overlaps:
        return "observed", "qbit_keep_new_release"
    choices = tuple(decide_upgrade(incoming, item, ranking) for item in overlaps)
    if any(choice.status not in {"upgrade", "not_upgrade"} for choice in choices):
        uncertain = next(choice for choice in choices if choice.status not in {"upgrade", "not_upgrade"})
        uncertain_status: DecisionStatus = "invalid_input" if uncertain.status == "invalid_input" else "review_required"
        return uncertain_status, "qbit_" + uncertain.reason
    if all(choice.is_upgrade for choice in choices):
        return "observed", "qbit_keep_strict_upgrade"
    if all(choice.is_upgrade is False for choice in choices) and any(
        item.number.covers(incoming.number) for item in overlaps
    ):
        return "planned", "qbit_exclude_lower_rank"
    return "review_required", "qbit_collection_or_rank_ambiguous"


def _library_problem(library: LibrarySnapshot) -> str | None:
    if not library.available:
        return "qbit_library_unavailable"
    if not any("library" in root.roles for root in library.roots):
        return "qbit_library_roots_unavailable"
    return None


def plan_torrent(
    torrent: dict[str, object], files: tuple[dict[str, object], ...], library: LibrarySnapshot, *,
    parsing: ParsingPolicy, matching: MatchingPolicy, ranking: RankingPolicy,
    options: Mapping[str, object],
) -> tuple[Decision, ...]:
    """Inputs are adapter-validated JSON; identities never use a basename key."""
    torrent_hash = str(torrent["hash"])
    if torrent["category"] != options.get("qbittorrent_target_category", "") or torrent["state"] not in _states(options):
        return (Decision("skipped", "qbit_ineligible_torrent", candidates=(torrent_hash,)),)
    problem = _library_problem(library)
    if problem is not None:
        return (Decision("missing_dependency", problem, candidates=(torrent_hash,)),)
    if not files:
        return (Decision("missing_dependency", "qbit_file_metadata_unavailable", candidates=(torrent_hash,)),)
    matching = replace(matching, match_through_image_similarity=False, match_through_identifiers=False)
    patterns = _unacceptable_patterns(cast(tuple[str, ...], options.get("unacceptable_keywords", ())))
    filter_unacceptable = bool(options.get("delete_unacceptable_files_toggle", False))
    title_excluded = filter_unacceptable and bool(options.get("delete_unacceptable_torrent_titles_in_qbit", False)) and any(
        pattern.search(str(torrent["name"])) for pattern in patterns
    )
    decisions: list[Decision] = []
    excluded: set[int] = set()
    for file in files:
        index = cast(int, file["index"])
        basename = PurePosixPath(str(file["name"])).name
        if title_excluded or filter_unacceptable and any(pattern.search(basename) for pattern in patterns):
            excluded.add(index)
            decisions.append(_decision(torrent, file, "skipped" if file["priority"] == 0 else "planned",
                                       "qbit_exclude_unacceptable_title" if title_excluded else "qbit_exclude_unacceptable_basename"))
            continue
        if PurePosixPath(basename).suffix.casefold() not in _BOOK_EXTENSIONS | _CONVERTIBLE_EXTENSIONS:
            decisions.append(_decision(torrent, file, "observed", "qbit_keep_auxiliary_file"))
            continue
        incoming = _incoming_release(torrent, file, parsing)
        status, reason = _release_choice(incoming, library, matching, ranking)
        if status == "planned":
            excluded.add(index)
            if file["priority"] == 0:
                status = "skipped"
        decisions.append(_decision(torrent, file, status, reason))
    if any(decision.status not in {"observed", "planned", "skipped"} for decision in decisions):
        return tuple(replace(decision, status="skipped", reason="qbit_torrent_undecided")
                     if decision.status == "planned" else decision for decision in decisions)
    terminal = "qbit_remove_all_excluded" if len(excluded) == len(files) else "qbit_start_retained"
    decisions.append(Decision("planned", terminal, candidates=(torrent_hash,)))
    return tuple(decisions)


def _generation(torrent: dict[str, object], files: tuple[dict[str, object], ...]) -> str:
    observed: dict[str, object] = {
        "torrent": {key: torrent[key] for key in (
            "hash", "name", "category", "added_on", "save_path", "content_path", "total_size",
        )},
        "files": [{key: file[key] for key in ("index", "name", "size")} for file in files],
    }
    return hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _priorities(files: tuple[dict[str, object], ...]) -> tuple[tuple[int, int], ...]:
    return tuple((cast(int, file["index"]), cast(int, file["priority"])) for file in files)


def _file_progress(files: tuple[dict[str, object], ...]) -> tuple[float | None, ...]:
    return tuple(cast(float | None, file.get("progress")) for file in files)


def _observe(
    client: HttpClient, torrent_hash: str,
) -> tuple[dict[str, object] | None, tuple[dict[str, object], ...]]:
    torrents = read_torrents(client, torrent_hash=torrent_hash)
    if not torrents:
        return None, ()
    return torrents[0], read_files(client, torrent_hash)


def _action(decision: Decision) -> dict[str, object]:
    if decision.reason == "qbit_start_retained":
        return {"kind": "start"}
    if decision.reason == "qbit_remove_all_excluded":
        return {"kind": "remove"}
    return {"kind": "priority", "name": decision.candidates[1], "index": int(decision.candidates[2])}


def _job_id(endpoint: str, generation: str, action: dict[str, object]) -> str:
    data = json.dumps([endpoint, generation, action], sort_keys=True, separators=(",", ":"))
    return "qbit-" + hashlib.sha256(data.encode()).hexdigest()


def _payload(
    client: HttpClient, torrent: dict[str, object], files: tuple[dict[str, object], ...],
    action: dict[str, object], options: Mapping[str, object], policy_version: str,
) -> dict[str, object]:
    return {"schema_version": 1, "endpoint": client.endpoint_key,
            "generation": _generation(torrent, files), "torrent": dict(torrent),
            "files": [dict(file) for file in files], "action": action,
            "target_states": list(_states(options)), "policy_version": policy_version}


def _effect_observed(
    payload: dict[str, object], current: dict[str, object] | None,
    files: tuple[dict[str, object], ...],
) -> Decision:
    original = cast(dict[str, object], payload["torrent"])
    action = cast(dict[str, object], payload["action"])
    identities = (str(original["hash"]),)
    if current is None:
        return Decision("committed" if action["kind"] == "remove" else "review_required",
                        "qbit_removal_reconciled" if action["kind"] == "remove" else "qbit_torrent_disappeared",
                        candidates=identities)
    if _generation(current, files) != payload["generation"]:
        return Decision("review_required", "qbit_torrent_generation_changed", candidates=identities)
    expected = tuple(cast(list[dict[str, object]], payload["files"]))
    wanted = dict(_priorities(expected))
    if action["kind"] == "priority":
        wanted[cast(int, action["index"])] = 0
    if dict(_priorities(files)) != wanted:
        return Decision("review_required", "qbit_effect_or_priorities_not_observed", candidates=identities)
    if action["kind"] == "priority":
        if current["state"] != original["state"] or _file_progress(files) != _file_progress(expected):
            return Decision("review_required", "qbit_state_or_progress_changed", candidates=identities)
        return Decision("committed", "qbit_priority_reconciled", candidates=(str(original["hash"]), str(action["name"]), str(action["index"])))
    if action["kind"] == "start" and current["state"] in _STARTED_STATES:
        return Decision("committed", "qbit_start_reconciled", candidates=identities)
    return Decision("review_required", "qbit_effect_not_observed", candidates=identities)


def _valid_job(job: JobRecord, endpoint: str) -> bool:
    payload = job.payload
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or payload.get("endpoint") != endpoint:
        return False
    action = payload.get("action")
    torrent = payload.get("torrent")
    files = payload.get("files")
    targets = payload.get("target_states")
    if (not isinstance(action, dict) or action.get("kind") not in {"priority", "start", "remove"}
            or not isinstance(torrent, dict) or not isinstance(files, list)
            or not isinstance(targets, list) or not all(isinstance(state, str) for state in targets)):
        return False
    if (not {"hash", "name", "category", "state", "progress", "added_on",
             "save_path", "content_path", "total_size"}.issubset(torrent)
            or not files or any(not isinstance(file, dict) or
                               not {"index", "name", "size", "priority", "progress"}.issubset(file)
                               for file in files)):
        return False
    from ..integrations.qbittorrent import validate_files, validate_torrents
    invalid = False
    try:
        validated_torrents = validate_torrents([torrent])
        validated_files = validate_files(files)
        if action["kind"] == "priority":
            invalid = not any(file["index"] == action.get("index") and file["name"] == action.get("name") for file in validated_files)
        invalid = invalid or _generation(validated_torrents[0], validated_files) != payload.get("generation")
    except HttpError:
        invalid = True
    return not invalid and job.job_id == _job_id(endpoint, str(payload["generation"]), action)


def _settle(state: StateStore, job_id: str, payload: dict[str, object], result: Decision) -> None:
    state.put_job(job_id, _JOB_KIND, payload,
                  state="complete" if result.status == "committed" else "needs_review",
                  error=None if result.status == "committed" else result.reason)


def _unsettled_jobs(state: StateStore, cancel: Event) -> Iterator[JobRecord]:
    after: str | None = None
    while not cancel.is_set():
        jobs = state.list_jobs(("running", "needs_review"), kind=_JOB_KIND,
                               after_job_id=after, limit=_JOB_PAGE_SIZE)
        if not jobs:
            return
        for job in jobs:
            if cancel.is_set():
                return
            after = job.job_id
            yield job
        if len(jobs) < _JOB_PAGE_SIZE:
            return


def _reconcile_jobs(
    client: HttpClient, state: StateStore, authorization: RunAuthorization,
    journal_ready: Callable[[], Decision], cancel: Event,
) -> tuple[tuple[Decision, ...], set[str], int]:
    decisions: list[Decision] = []
    blocked: set[str] = set()
    committed = 0
    for job in _unsettled_jobs(state, cancel):
        if job.payload.get("endpoint") != client.endpoint_key:
            continue
        original = job.payload.get("torrent")
        torrent_hash = str(original.get("hash", "")) if isinstance(original, dict) else ""
        if not _valid_job(job, client.endpoint_key):
            decisions.append(Decision("review_required", "qbit_invalid_durable_action", candidates=(job.job_id,)))
            blocked.add("*")
            continue
        try:
            current, files = _observe(client, torrent_hash)
        except HttpError as error:
            blocked.add(torrent_hash)
            decisions.append(Decision(error.status, error.reason, candidates=(torrent_hash, job.job_id)))
            continue
        if job.state == "running":
            blocked.add(torrent_hash)
            decisions.append(Decision("review_required", "qbit_action_claimed_or_interrupted", candidates=(torrent_hash, job.job_id)))
            continue
        result = _effect_observed(job.payload, current, files)
        if result.status == "committed" and authorization.allow_remote:
            try:
                ready = journal_ready()
                if ready.status != "observed":
                    decisions.append(ready)
                    blocked.add(torrent_hash)
                    continue
                _settle(state, job.job_id, job.payload, result)
            except StateStoreError:
                decisions.append(Decision("review_required", "qbit_journal_unavailable", candidates=(torrent_hash, job.job_id)))
                blocked.add(torrent_hash)
                continue
            committed += 1
            decisions.append(result)
        else:
            blocked.add(torrent_hash)
            decisions.append(replace(result, status="review_required", reason="qbit_reconciliation_requires_remote_authorization")
                             if result.status == "committed" else result)
    return tuple(decisions), blocked, committed


def _fresh_preconditions(
    torrent: dict[str, object], expected: tuple[dict[str, object], ...],
    current: dict[str, object] | None, files: tuple[dict[str, object], ...],
    options: Mapping[str, object],
) -> bool:
    return (current is not None and current["state"] == torrent["state"]
            and current["state"] in _states(options)
            and current.get("progress") == torrent.get("progress")
            and _generation(torrent, expected) == _generation(current, files)
            and _priorities(expected) == _priorities(files)
            and _file_progress(expected) == _file_progress(files))


def _apply_action(
    client: HttpClient, torrent: dict[str, object], expected: tuple[dict[str, object], ...],
    planned: Decision, catalog_provider: Callable[[], LibrarySnapshot], *,
    parsing: ParsingPolicy, matching: MatchingPolicy, ranking: RankingPolicy,
    options: Mapping[str, object], state: StateStore, journal_ready: Callable[[], Decision],
    cancel: Event, version: tuple[int, int, int], policy_version: str,
) -> tuple[Decision, dict[str, object] | None, tuple[dict[str, object], ...]]:
    torrent_hash = str(torrent["hash"])
    action = _action(planned)
    ready = journal_ready()
    if ready.status != "observed":
        return ready, torrent, expected
    payload = _payload(client, torrent, expected, action, options, policy_version)
    job_id = _job_id(client.endpoint_key, str(payload["generation"]), action)
    prior = state.get_job(job_id)
    retry = (prior is not None and prior.kind == _JOB_KIND and prior.state == "pending"
             and _valid_job(prior, client.endpoint_key))
    claim_payload = prior.payload if prior is not None and retry else payload
    if not state.claim_job(job_id, _JOB_KIND, claim_payload, retry_pending=retry):
        job = state.get_job(job_id)
        reason = "qbit_action_claimed_or_interrupted" if job is not None and job.state == "running" else "qbit_action_already_attempted"
        return Decision("review_required", reason, candidates=(torrent_hash, job_id)), torrent, expected
    if retry:
        state.put_job(job_id, _JOB_KIND, payload, state="running")
    for other in _unsettled_jobs(state, cancel):
        if other.job_id == job_id or other.payload.get("endpoint") != client.endpoint_key:
            continue
        other_torrent = other.payload.get("torrent")
        if not isinstance(other_torrent, dict) or other_torrent.get("hash") == torrent_hash:
            state.put_job(job_id, _JOB_KIND, payload, state="pending", error="qbit_other_action_unsettled")
            return Decision("review_required", "qbit_other_action_unsettled", candidates=(torrent_hash, other.job_id)), torrent, expected
    preflight_error: Decision | None = None
    try:
        current, current_files = _observe(client, torrent_hash)
        if not _fresh_preconditions(torrent, expected, current, current_files, options):
            preflight_error = Decision("stale_plan", "qbit_observation_changed", candidates=(torrent_hash,))
        elif cancel.is_set():
            preflight_error = Decision("cancelled", "qbit_cancelled", candidates=(torrent_hash,))
        elif parse_webapi_version(client.request("GET", "api/v2/app/webapiVersion", read_only=True).text) != version:
            preflight_error = Decision("stale_plan", "qbit_server_version_changed", candidates=(torrent_hash,))
        else:
            fresh_plan = plan_torrent(cast(dict[str, object], current), current_files, catalog_provider(),
                                      parsing=parsing, matching=matching, ranking=ranking, options=options)
            if not any(item.status == "planned" and _action(item) == action for item in fresh_plan):
                preflight_error = Decision("stale_plan", "qbit_library_decision_changed", candidates=(torrent_hash,))
            elif cancel.is_set():
                preflight_error = Decision("cancelled", "qbit_cancelled", candidates=(torrent_hash,))
    except HttpError as error:
        preflight_error = Decision(error.status, error.reason, candidates=(torrent_hash,))
    except OSError:
        preflight_error = Decision("missing_dependency", "qbit_library_unavailable", candidates=(torrent_hash,))
    if preflight_error is not None:
        state.put_job(job_id, _JOB_KIND, payload, state="pending", error=preflight_error.reason)
        return preflight_error, torrent, expected
    mutation_error: HttpError | None = None
    try:
        if action["kind"] == "priority":
            set_file_priority(client, torrent_hash, (cast(int, action["index"]),))
        elif action["kind"] == "start":
            start_torrent(client, torrent_hash, version)
        else:
            remove_torrent(client, torrent_hash)
    except HttpError as error:
        mutation_error = error
    if mutation_error is not None and not mutation_error.pending_unknown:
        state.put_job(job_id, _JOB_KIND, payload, state="failed", error=mutation_error.reason)
        try:
            _observe(client, torrent_hash)
        except HttpError:
            pass
        return Decision(mutation_error.status, mutation_error.reason, candidates=(torrent_hash, job_id)), torrent, expected
    state.put_job(job_id, _JOB_KIND, payload, state="needs_review",
                  error=mutation_error.reason if mutation_error is not None else "qbit_confirmation_pending")
    payload["last_observation"] = None
    try:
        current, current_files = _observe(client, torrent_hash)
        payload["last_observation"] = {"torrent": current, "files": list(current_files)}
        result = _effect_observed(payload, current, current_files)
    except HttpError:
        current = None
        current_files = expected
        result = Decision("review_required", "qbit_confirmation_unavailable", candidates=(torrent_hash, job_id))
    _settle(state, job_id, payload, result)
    return result, current, current_files


def _apply_torrent(
    client: HttpClient, torrent: dict[str, object], files: tuple[dict[str, object], ...],
    plan: tuple[Decision, ...], catalog_provider: Callable[[], LibrarySnapshot], *,
    parsing: ParsingPolicy, matching: MatchingPolicy, ranking: RankingPolicy,
    options: Mapping[str, object], authorization: RunAuthorization, state: StateStore,
    journal_ready: Callable[[], Decision], cancel: Event, version: tuple[int, int, int],
    policy_version: str,
) -> RunResult:
    torrent_hash = str(torrent["hash"])
    if torrent["added_on"] is None:
        return RunResult((Decision("review_required", "qbit_observation_generation_unavailable", candidates=(torrent_hash,)),))
    if torrent.get("progress") is None or any(file.get("progress") is None for file in files):
        return RunResult((Decision("review_required", "qbit_downloader_progress_unavailable", candidates=(torrent_hash,)),))
    expected = files
    expected_torrent = torrent
    decisions = [decision for decision in plan if decision.status != "planned"]
    committed = 0
    for planned in (decision for decision in plan if decision.status == "planned"):
        if planned.reason == "qbit_remove_all_excluded" and not authorization.allow_torrent_removal:
            decisions.append(replace(planned, status="skipped", reason="qbit_all_excluded_torrent_retained"))
            continue
        if cancel.is_set():
            decisions.append(Decision("cancelled", "qbit_cancelled", candidates=(torrent_hash,)))
            break
        try:
            result, current, expected = _apply_action(client, expected_torrent, expected, planned, catalog_provider,
                                            parsing=parsing, matching=matching, ranking=ranking,
                                            options=options, state=state, journal_ready=journal_ready,
                                            cancel=cancel, version=version, policy_version=policy_version)
        except StateStoreError:
            current = None
            result = Decision("review_required", "qbit_journal_unavailable", candidates=(torrent_hash,))
        decisions.append(result)
        if result.status != "committed":
            break
        committed += 1
        if current is not None:
            expected_torrent = current
    return RunResult(tuple(decisions), committed=committed,
                     skipped=sum(item.status == "skipped" for item in decisions),
                     failed=sum(item.status in {"failed", "permission_denied", "publication_failed"} for item in decisions))


def _preview(plan: tuple[Decision, ...], authorization: RunAuthorization) -> tuple[Decision, ...]:
    return tuple(replace(decision, status="skipped", reason="qbit_all_excluded_torrent_retained")
                 if decision.reason == "qbit_remove_all_excluded" and not authorization.allow_torrent_removal
                 else decision for decision in plan)


def run_torrents(
    client: HttpClient, catalog_provider: Callable[[], LibrarySnapshot], *,
    parsing: ParsingPolicy, matching: MatchingPolicy, ranking: RankingPolicy,
    options: Mapping[str, object], authorization: RunAuthorization, state: StateStore,
    journal_ready: Callable[[], Decision], cancel: Event, once: bool = False,
    on_ready: Callable[[], None] | None = None, policy_version: str = "qbit-selection-v1",
) -> RunResult:
    """The bootstrap owns transport closure; waits and intake stop on cancellation."""
    if not options.get("uncheck_non_qbit_upgrades_toggle", True):
        return RunResult((Decision("skipped", "qbit_filter_disabled"),), skipped=1, planned_count=0)
    if not options.get("check_for_existing_series_toggle", True):
        return RunResult((Decision("invalid_input", "qbit_series_matching_disabled"),), planned_count=0)
    if not options.get("qbittorrent_target_category"):
        return RunResult((Decision("invalid_input", "qbit_target_category_required"),), planned_count=0)
    version: tuple[int, int, int] | None = None
    committed = skipped = failed = planned_count = 0
    notified = False
    decisions: tuple[Decision, ...] = ()
    while not cancel.is_set():
        planned_count = 0
        try:
            if version is None:
                version = authenticate(client, username=cast(str, options.get("qbittorrent_username", "")),
                                       password=cast(str, options.get("qbittorrent_password", "")))
            torrents = read_torrents(client)
            reconciled, blocked, reconciled_count = _reconcile_jobs(client, state, authorization, journal_ready, cancel)
            committed += reconciled_count
            current_decisions = list(reconciled)
            eligible = [torrent for torrent in torrents if torrent["category"] == options["qbittorrent_target_category"]
                        and torrent["state"] in _states(options)]
            observed: list[tuple[dict[str, object], tuple[dict[str, object], ...]]] = []
            for torrent in eligible:
                if cancel.is_set():
                    break
                if "*" in blocked or str(torrent["hash"]) in blocked:
                    continue
                try:
                    observed.append((torrent, read_files(client, str(torrent["hash"]))))
                except HttpError as error:
                    current_decisions.append(Decision(error.status, error.reason, candidates=(str(torrent["hash"]),)))
            observed.sort(key=lambda item: len(item[1]))
            library = catalog_provider()
            problem = _library_problem(library)
            if problem is not None:
                current_decisions.append(Decision("missing_dependency", problem))
                observed.clear()
            for torrent, files in observed:
                if cancel.is_set():
                    break
                plan = plan_torrent(torrent, files, library, parsing=parsing, matching=matching,
                                    ranking=ranking, options=options)
                preview = _preview(plan, authorization)
                planned_count += sum(item.status == "planned" for item in preview)
                if authorization.allow_remote and any(item.status == "planned" for item in preview):
                    result = _apply_torrent(client, torrent, files, plan, catalog_provider,
                                            parsing=parsing, matching=matching, ranking=ranking, options=options,
                                            authorization=authorization, state=state, journal_ready=journal_ready,
                                            cancel=cancel, version=version, policy_version=policy_version)
                    committed += result.committed
                    skipped += result.skipped
                    failed += result.failed
                    current_decisions.extend(result.decisions)
                else:
                    skipped += sum(item.status == "skipped" for item in preview)
                    current_decisions.extend(preview)
            if not current_decisions:
                current_decisions.append(Decision("observed", "qbit_no_eligible_torrents"))
            decisions = tuple(current_decisions)
            if not notified and not cancel.is_set():
                if any(item.status in {"permission_denied", "invalid_input", "unsupported", "missing_dependency", "failed"}
                       and item.reason != "qbit_file_metadata_unavailable" for item in decisions):
                    return RunResult(decisions, committed=committed, skipped=skipped, failed=failed, planned_count=planned_count)
                if on_ready is not None:
                    on_ready()
                notified = True
        except HttpError as error:
            decisions = (Decision(error.status, error.reason),)
            if once or error.status in {"permission_denied", "invalid_input", "unsupported", "cancelled"}:
                return RunResult(decisions, committed=committed, skipped=skipped, failed=failed, planned_count=planned_count)
            version = None
            if cancel.wait(float(cast(float, options.get("qbit_reconnect_interval_seconds", 60)))):
                break
            continue
        except StateStoreError:
            return RunResult((Decision("review_required", "qbit_journal_unavailable"),), committed=committed,
                             skipped=skipped, failed=failed, planned_count=planned_count)
        except OSError:
            decisions = (Decision("missing_dependency", "qbit_library_unavailable"),)
            if not notified:
                return RunResult(decisions, committed=committed, skipped=skipped, failed=failed, planned_count=planned_count)
        if once:
            return RunResult(decisions, committed=committed, skipped=skipped, failed=failed, planned_count=planned_count)
        if cancel.wait(float(cast(float, options.get("qbit_poll_interval_seconds", 30)))):
            break
    return RunResult((*decisions, Decision("cancelled", "qbit_cancelled")), committed=committed,
                     skipped=skipped, failed=failed, planned_count=planned_count)

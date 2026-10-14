"""Construct per-invocation dependencies and release them on every exit path."""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from typing import cast

import regex

from . import __version__
from .application.pipeline import Pipeline, RunRequest
from .application.rename_lists import DictionaryPolicy, load_dictionaries
from .config import Configuration
from .covers.selection import CoverPolicy
from .covers.series_policy import SeriesCoverPolicy
from .domain.decisions import Decision, RunAuthorization, RunResult
from .domain.libraries import (
    LibraryRoot,
    PathMapping,
    infer_book_roots,
    validate_private_paths,
)
from .domain.releases import ReleaseSnapshot
from .infrastructure.state import StateStore
from .infrastructure.transactions import TransactionExecutor
from .infrastructure.zip_archive import ArchivePolicy
from .matching.classification import ClassificationPolicy, LibraryType
from .matching.ranking import RankFileType, RankingPolicy, RankRule
from .matching.series import MatchingPolicy
from .parsing.filenames import ParsingPolicy, RenamePolicy
from .parsing.numbers import NumberPolicy


def parsing_policy(config: Configuration, *, release_groups: tuple[str, ...] = (), publishers: tuple[str, ...] = ()) -> ParsingPolicy:
    return ParsingPolicy(
        number_policy=NumberPolicy(
            volume_keywords=config.volume_keywords, chapter_keywords=config.chapter_keywords,
            exclusion_keywords=config.exclusion_keywords,
            subtitle_exclusion_keywords=config.subtitle_exclusion_keywords,
        ),
        exception_keywords=config.exception_keywords,
        release_groups=release_groups, publishers=publishers,
        average_chapter_image_count=config.average_chapter_image_count,
        chapter_support_toggle=config.chapter_support_toggle,
    )


def rename_policy(config: Configuration, *, numbers: NumberPolicy | None = None, publishers: tuple[str, ...] = ()) -> RenamePolicy:
    return RenamePolicy(
        number_policy=numbers if numbers is not None else parsing_policy(config).number_policy,
        publishers=publishers,
        publisher_similarity_score=config.publisher_similarity_score,
        release_group_similarity_score=config.release_group_similarity_score,
        required_similarity_score=config.required_similarity_score,
        preferred_volume_renaming_format=config.preferred_volume_renaming_format,
        preferred_chapter_renaming_format=config.preferred_chapter_renaming_format,
        zfill_volume_int_value=config.zfill_volume_int_value,
        zfill_volume_float_value=config.zfill_volume_float_value,
        zfill_chapter_int_value=config.zfill_chapter_int_value,
        zfill_chapter_float_value=config.zfill_chapter_float_value,
        add_volume_one_number_to_one_shots=config.add_volume_one_number_to_one_shots,
        add_issue_number_to_manga_file_name=config.add_issue_number_to_manga_file_name,
        restructure_when_renaming=config.restructure_when_renaming,
        search_and_add_premium_to_file_name=config.search_and_add_premium_to_file_name,
        add_publisher_name_to_file_name_when_renaming=config.add_publisher_name_to_file_name_when_renaming,
        move_release_group_to_end_of_file_name=config.move_release_group_to_end_of_file_name,
        replace_unicode_when_restructuring=config.replace_unicode_when_restructuring,
        rename_chapters_with_preferred_chapter_keyword=config.rename_chapters_with_preferred_chapter_keyword,
    )

def ranking_policy(config: Configuration) -> RankingPolicy:
    return RankingPolicy(tuple(RankRule(rule.name, rule.score, cast(RankFileType, rule.file_type)) for rule in config.ranked_keywords))


def matching_policy(config: Configuration) -> MatchingPolicy:
    classification = ClassificationPolicy(
        library_types=tuple(
            LibraryType(rule.name, rule.extensions, rule.must_contain, rule.must_not_contain, rule.match_percentage)
            for rule in config.library_types
        ),
        required_similarity_score=config.required_similarity_score,
    )
    return MatchingPolicy(
        required_similarity_score=config.required_similarity_score,
        match_through_image_similarity=config.match_through_image_similarity,
        required_image_similarity_score=config.required_image_similarity_score,
        match_through_identifiers=config.match_through_identifiers,
        required_matching_percentage=config.required_matching_percentage,
        classification=classification,
    )



def archive_policy(config: Configuration) -> ArchivePolicy:
    return ArchivePolicy(
        archive_max_entries=config.archive_max_entries,
        cover_max_candidates=config.cover_max_candidates,
        metadata_max_bytes=config.metadata_max_bytes,
        cover_member_max_bytes=config.cover_member_max_bytes,
        conversion_member_max_bytes=config.conversion_member_max_bytes,
        cover_read_max_bytes=config.cover_read_max_bytes,
        archive_expansion_max_bytes=config.archive_expansion_max_bytes,
        conversion_scratch_max_bytes=config.conversion_scratch_max_bytes,
        archive_max_ratio=config.archive_max_ratio,
        image_max_pixels=config.image_max_pixels,
        conversion_timeout_seconds=config.conversion_timeout_seconds,
    )


def cover_policy(config: Configuration, archive: ArchivePolicy) -> CoverPolicy:
    return CoverPolicy(
        archive_policy=archive, cover_patterns=config.cover_patterns,
        chapter_support_toggle=config.chapter_support_toggle,
        extract_chapter_covers=config.extract_chapter_covers,
        compare_detected_cover_to_blank_images=config.compare_detected_cover_to_blank_images,
        blank_cover_required_similarity_score=config.blank_cover_required_similarity_score,
        compress_image_option=config.compress_image_option, image_quality=config.image_quality,
        output_covers_as_webp=config.output_covers_as_webp,
        return_data_image_quality=config.return_data_image_quality,
    )


def series_cover_policy(config: Configuration) -> SeriesCoverPolicy:
    return SeriesCoverPolicy(
        mode="latest" if config.use_latest_volume_cover_as_series_cover else "first",
        roles=config.series_cover_file_names,
        create_roles=("cover",) if "cover" in config.series_cover_file_names else (),
        copy_existing_volume_covers_toggle=config.copy_existing_volume_covers_toggle,
        chapter_support_toggle=config.chapter_support_toggle,
        extract_chapter_covers=config.extract_chapter_covers,
    )


@contextmanager
def open_pipeline(config: Configuration, *, request: RunRequest | None = None) -> Iterator[Pipeline]:
    roots = tuple(
        [LibraryRoot(path, roles=("library",)) for path in config.paths]
        + [LibraryRoot(path, roles=("download",)) for path in config.download_folders]
    )
    if request is not None and request.roots:
        roots = request.roots
    if not roots and request is not None and request.books and request.command != "recover":
        roots = infer_book_roots(request.books)
    validate_private_paths(tuple(root.path for root in roots), state_dir=config.state_dir, log_dir=config.log_dir)
    dictionaries = load_dictionaries(config.state_dir)
    parsing = parsing_policy(
        config, release_groups=tuple(regex.escape(value) for value in dictionaries.release_groups),
        publishers=tuple(regex.escape(value) for value in dictionaries.publishers),
    )
    archive = archive_policy(config)
    matching = matching_policy(config)
    from .application.organize import OPTIONS
    options = {name: cast(bool, getattr(config, name)) for name in (
        *OPTIONS, "correct_file_extensions_toggle", "convert_to_cbz_toggle", "rename_zip_to_cbz",
        "extract_covers_toggle", "check_for_missing_volumes_toggle", "bookwalker_check",
        "auto_classify_watchdog_paths",
    )}
    pipeline = Pipeline(
        roots=roots, state_dir=config.state_dir, max_stage_bytes=config.conversion_scratch_max_bytes,
        parsing=parsing, renaming=rename_policy(config, numbers=parsing.number_policy,
                                               publishers=dictionaries.publishers),
        archive=archive, covers=cover_policy(config, archive),
        series_artwork=series_cover_policy(config), policy_version=config.policy_fingerprint(rule_version=4),
        classification=matching.classification, matching=matching, ranking=ranking_policy(config),
        options=options, unacceptable_keywords=config.unacceptable_keywords,
        ignored_folder_names=config.ignored_folder_names, log_dir=config.log_dir,
        dictionary_policy=DictionaryPolicy(
            config.generate_release_group_list_toggle, config.manual_rename,
            config.publisher_similarity_score, config.release_group_similarity_score,
            tuple(root.path for root in roots if root.formats == ("chapter",)),
        ),
        dictionary_version=dictionaries.version, dictionary_diagnostics=dictionaries.diagnostics,
    )
    with ExitStack() as resources:
        try:
            from .application.delivery import (
                settle_bookwalker_deliveries,
                settle_committed_deliveries,
            )
            from .integrations.discord import DiscordDelivery
            from .integrations.http import HttpClient, HttpError
            from .integrations.komga import komga_base_url
            connect_timeout = config.http_connect_timeout_seconds
            read_timeout = config.http_read_timeout_seconds
            max_attempts = config.http_max_attempts
            discord: DiscordDelivery | None = None
            komga: HttpClient | None = None
            bookwalker: HttpClient | None = None
            state = StateStore(config.state_dir)
            def finish(result: RunResult, invocation: RunRequest, before: tuple[ReleaseSnapshot, ...]) -> RunResult:
                nonlocal discord, komga
                if (result.committed or invocation.authorization.allow_remote
                        or invocation.authorization.apply
                        and state.list_outbox(states=("queued", "pending", "unknown", "failed"), limit=1)):
                    try:
                        if discord is None and (config.discord_webhook_url or config.new_volume_webhook or config.bookwalker_webhook_urls):
                            discord = resources.enter_context(DiscordDelivery(
                                config.discord_webhook_url,
                                new_release_webhook=config.new_volume_webhook,
                                bookwalker_webhooks=config.bookwalker_webhook_urls,
                                connect_timeout=connect_timeout, read_timeout=read_timeout,
                                max_attempts=max_attempts, cancel=pipeline.cancel,
                            ))
                        if komga is None and config.send_scan_request_to_komga_libraries_toggle:
                            komga = resources.enter_context(HttpClient(
                                komga_base_url(config.komga_ip, str(config.komga_port)),
                                auth=(config.komga_login_email, config.komga_login_password),
                                connect_timeout=connect_timeout, read_timeout=read_timeout,
                                max_attempts=max_attempts, cancel=pipeline.cancel,
                            ))
                    except HttpError as error:
                        return replace(result, decisions=(*result.decisions, Decision(error.status, error.reason)))
                return settle_committed_deliveries(
                    result, state=state, roots=pipeline.roots, parsing=pipeline.parsing,
                    ranking=pipeline.ranking, cover_policy=pipeline.covers, discord=discord, komga=komga,
                    mappings=tuple(PathMapping(value.local_root, value.remote_root) for value in config.komga_path_mappings),
                    authorization=invocation.authorization, cancel=pipeline.cancel, before=before,
                    chapter_covers=config.output_chapter_covers_to_discord,
                    mute_rename=config.mute_discord_rename_notifications,
                    version=f"v{__version__}",
                )
            def check(releases: tuple[ReleaseSnapshot, ...], authorization: RunAuthorization) -> RunResult:
                nonlocal bookwalker, discord
                from .application.release_checks import check_bookwalker_releases
                from .integrations.bookwalker import (
                    BOOKWALKER_COOKIES,
                    BOOKWALKER_ORIGIN,
                    BOOKWALKER_USER_AGENT,
                )
                if bookwalker is None:
                    try:
                        bookwalker = resources.enter_context(HttpClient(
                            BOOKWALKER_ORIGIN, connect_timeout=config.http_connect_timeout_seconds,
                            read_timeout=config.bookwalker_request_timeout_seconds,
                            max_attempts=config.http_max_attempts, cancel=pipeline.cancel,
                        ))
                        bookwalker.set_header("User-Agent", BOOKWALKER_USER_AGENT)
                        for name, value in BOOKWALKER_COOKIES:
                            bookwalker.set_cookie(name, value)
                    except HttpError as error:
                        return RunResult((Decision(error.status, error.reason),))
                result = check_bookwalker_releases(
                    releases, client=bookwalker, number_policy=pipeline.parsing.number_policy,
                    required_similarity_score=config.required_similarity_score, authorization=authorization,
                    max_pages=config.bookwalker_max_pages, request_interval_seconds=config.bookwalker_request_interval_seconds,
                    cancel=pipeline.cancel,
                )
                if authorization.allow_remote and discord is None and config.bookwalker_webhook_urls:
                    try:
                        discord = resources.enter_context(DiscordDelivery(
                            config.discord_webhook_url,
                            new_release_webhook=config.new_volume_webhook,
                            bookwalker_webhooks=config.bookwalker_webhook_urls,
                            connect_timeout=connect_timeout, read_timeout=read_timeout,
                            max_attempts=max_attempts, cancel=pipeline.cancel,
                        ))
                    except HttpError as error:
                        return replace(result, decisions=(*result.decisions, Decision(error.status, error.reason)))
                return settle_bookwalker_deliveries(
                    result, state=state, discord=discord, authorization=authorization, cancel=pipeline.cancel,
                    version=f"v{__version__}",
                    journal_ready=lambda: TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots),
                        cancel=pipeline.cancel).prepare_state_root(
                            authorization=RunAuthorization(apply=authorization.allow_remote)),
                )
            pipeline.after_commit = finish
            pipeline.release_check = check
            yield pipeline
        finally:
            pipeline.close()

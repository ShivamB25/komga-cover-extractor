from __future__ import annotations

import hashlib
import os
import shutil
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

from komga_cover_extractor.application.pipeline import RunRequest
from komga_cover_extractor.application.watch import WatchCoordinator
from komga_cover_extractor.bootstrap import open_pipeline
from komga_cover_extractor.config import Configuration, validate_config
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.state import StateStore
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor

from .test_watch_lifecycle import Clock, pump


@pytest.fixture
def multipart_parts(tmp_path: Path) -> tuple[Path, ...]:
    pytest.importorskip('rarfile', reason='real multipart watch conversion requires the conversion extra')
    pytest.importorskip('watchdog', reason='real multipart watch intake requires the watch extra')
    if shutil.which('unrar') is None:
        pytest.skip('real multipart watch conversion requires the RARLAB unrar executable')
    if sys.platform != 'linux':
        pytest.skip('real native watch intake requires Linux')
    corpus = Path(__file__).parents[1] / 'fixtures' / 'native'
    sources = tuple(sorted(corpus.glob('rar5-vols.part*.rar')))
    assert tuple(path.name for path in sources) == (
        'rar5-vols.part1.rar', 'rar5-vols.part2.rar', 'rar5-vols.part3.rar',
    )
    root = tmp_path / 'downloads'
    root.mkdir()
    parts = tuple(root / source.name for source in sources)
    for source, destination in zip(sources, parts, strict=True):
        shutil.copyfile(source, destination)
    return parts


def _config(tmp_path: Path, *, dual_role: bool) -> Configuration:
    root = tmp_path / 'downloads'
    return validate_config({
        'paths': [str(root)] if dual_role else [],
        'download_folders': [str(root)],
        'state_dir': str(tmp_path / 'state'), 'log_dir': str(tmp_path / 'logs'),
        'log_to_file': False, 'manual_rename': False, 'manual_delete': False,
        'correct_file_extensions_toggle': False, 'convert_to_cbz_toggle': True,
        'rename_zip_to_cbz': False, 'extract_covers_toggle': False,
        'check_for_existing_series_toggle': False, 'check_for_duplicate_volumes_toggle': False,
        'check_for_missing_volumes_toggle': False, 'bookwalker_check': False,
        'generate_release_group_list_toggle': False,
        'delete_unacceptable_files_toggle': False, 'delete_chapters_from_downloads_toggle': False,
        'create_folders_for_items_in_download_folder_toggle': False,
        'rename_dirs_in_download_folder_toggle': False,
        'rename_files_in_download_folders_toggle': False,
        'move_series_to_correct_library_toggle': False,
        'move_lone_files_to_similar_folder': False,
        'replace_series_name_in_file_name_with_similar_folder_name': False,
        'auto_classify_watchdog_paths': False,
        'match_through_image_similarity': False, 'match_through_identifiers': False,
        'send_scan_request_to_komga_libraries_toggle': False,
        'ranked_keywords': [{'name': 'Digital', 'score': 10, 'file_type': 'volume'}],
    }, base_dir=tmp_path)


def _pump_bounded(coordinator: WatchCoordinator, clock: Clock, seconds: int) -> None:
    for _ in range(seconds):
        pump(coordinator, clock, 1)
        assert coordinator.pending_count <= 2
        assert coordinator.queued_count <= 2
        assert coordinator.snapshot().failed == 0, coordinator.snapshot().as_dict()


def _assert_waiting(
    coordinator: WatchCoordinator, state: StateStore, originals: dict[Path, bytes],
) -> None:
    assert coordinator.snapshot().committed == 0
    assert sorted(next(iter(originals)).parent.glob('*.cbz')) == []
    for path, data in originals.items():
        assert path.read_bytes() == data
    assert state.list_operations() == ()
    jobs = state.list_jobs(kind='watch', limit=10)
    assert 1 <= sum(job.state == 'pending' for job in jobs) <= 2
    assert {job.payload['attempts'] for job in jobs} == {0}


def _assert_conversion(
    coordinator: WatchCoordinator, state: StateStore, originals: dict[Path, bytes],
) -> None:
    result = coordinator.snapshot()
    assert (result.committed, result.failed, result.exit_code) == (1, 0, 0), result.as_dict()
    primary = next(iter(originals))
    target = primary.with_suffix('.cbz')
    assert sorted(primary.parent.glob('*.cbz')) == [target]
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == ['vols/bigfile.txt', 'vols/smallfile.txt']
        big = archive.read('vols/bigfile.txt')
        small = archive.read('vols/smallfile.txt')
        assert (len(big), zlib.crc32(big)) == (205000, 0x509ad74c)
        assert (len(small), zlib.crc32(small)) == (2050, 0xd08a1f86)
    records = state.list_operations()
    assert len(records) == 1 and records[0].phase == 'complete'
    members = state.list_members(records[0].operation_id)
    retained = tuple(member for member in members if member.kind == 'quarantine')
    assert len(members) == 4 and len(retained) == 3
    assert {member.source.path for member in retained if member.source is not None} == set(originals)
    assert len({member.cleanup_path for member in retained}) == 3
    for member in retained:
        assert member.status == 'complete'
        assert member.source is not None
        assert member.cleanup_path is not None and member.cleanup_fingerprint is not None
        expected = originals[member.source.path]
        assert member.cleanup_path.read_bytes() == expected
        assert member.cleanup_fingerprint.sha256 == hashlib.sha256(expected).hexdigest()
        assert fingerprint(member.cleanup_path, member.cleanup_fingerprint.root) == member.cleanup_fingerprint
        assert not member.source.path.exists()
    assert not tuple(primary.parent.glob('.kce-convert-*'))


@pytest.mark.parametrize('dual_role', (False, True), ids=('download-only', 'dual-role'))
def test_secondary_completion_and_new_quiet_window_gate_one_real_conversion(
    tmp_path: Path, multipart_parts: tuple[Path, ...], dual_role: bool,
) -> None:
    config = _config(tmp_path, dual_role=dual_role)
    originals = {part: part.read_bytes() for part in multipart_parts}
    complete = {part: True for part in multipart_parts}
    complete[multipart_parts[2]] = False
    clock = Clock()
    with open_pipeline(config) as pipeline:
        state = StateStore(config.state_dir)
        executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
        coordinator = WatchCoordinator(
            pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
            state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
            clock=clock, downloader_complete=lambda path: complete.get(path, True),
            options={'watch_queue_capacity': 2, 'watch_max_pending_jobs': 2,
                     'watchdog_file_transferred_check_interval': 1,
                     'watchdog_discover_new_files_check_interval': 2,
                     'watch_reconcile_interval_seconds': 3},
        )
        observers = ()
        emitters = ()
        try:
            assert coordinator.start().status == 'observed'
            observers = tuple(coordinator._observers.values())
            emitters = tuple(emitter for observer in observers for emitter in observer.emitters)
            assert len(observers) == 1 and all(observer.is_alive() for observer in observers)
            for part in multipart_parts:
                coordinator.enqueue(('created', part, None, False))
            assert coordinator.queued_count == 2
            _pump_bounded(coordinator, clock, 12)
            _assert_waiting(coordinator, state, originals)

            secondary = multipart_parts[2]
            previous = fingerprint(secondary, secondary.parent)
            secondary.write_bytes(originals[secondary])
            os.utime(secondary, ns=(previous.mtime_ns, previous.mtime_ns + 1_000_000_000))
            current = fingerprint(secondary, secondary.parent)
            assert current.sha256 == previous.sha256 and current != previous
            complete[secondary] = True
            coordinator.enqueue(('modified', secondary, None, False))
            coordinator.tick()
            _pump_bounded(coordinator, clock, 1)
            _assert_waiting(coordinator, state, originals)

            _pump_bounded(coordinator, clock, 20)
            _assert_conversion(coordinator, state, originals)
            retained = tuple(member for record in state.list_operations()
                             for member in state.list_members(record.operation_id)
                             if member.source is not None and member.source.path == secondary)
            assert len(retained) == 1 and retained[0].source == current
            target = multipart_parts[0].with_suffix('.cbz')
            published = fingerprint(target, target.parent)
            _pump_bounded(coordinator, clock, 12)
            assert fingerprint(target, target.parent) == published
            _assert_conversion(coordinator, state, originals)
        finally:
            coordinator.close()
        assert not any(observer.is_alive() for observer in observers)
        assert not any(emitter.is_alive() for emitter in emitters)


def test_cancelled_multipart_wait_retains_sources_and_restart_requires_fresh_quiet(
    tmp_path: Path, multipart_parts: tuple[Path, ...],
) -> None:
    config = _config(tmp_path, dual_role=False)
    originals = {part: part.read_bytes() for part in multipart_parts}
    complete = {part: True for part in multipart_parts}
    complete[multipart_parts[2]] = False
    clock = Clock()
    state = StateStore(config.state_dir)
    for restart in (False, True):
        with open_pipeline(config) as pipeline:
            executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
            coordinator = WatchCoordinator(
                pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
                state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
                clock=clock, downloader_complete=lambda path: complete.get(path, True),
                options={'watch_queue_capacity': 2, 'watch_max_pending_jobs': 2,
                         'watchdog_file_transferred_check_interval': 1,
                         'watchdog_discover_new_files_check_interval': 2,
                         'watch_reconcile_interval_seconds': 3},
            )
            observers = ()
            emitters = ()
            try:
                assert coordinator.start().status == 'observed'
                observers = tuple(coordinator._observers.values())
                emitters = tuple(emitter for observer in observers for emitter in observer.emitters)
                if not restart:
                    _pump_bounded(coordinator, clock, 12)
                    _assert_waiting(coordinator, state, originals)
                    coordinator.request_stop()
                    complete[multipart_parts[2]] = True
                    _pump_bounded(coordinator, clock, 12)
                    _assert_waiting(coordinator, state, originals)
                    assert pipeline.cancel.is_set()
                else:
                    _pump_bounded(coordinator, clock, 1)
                    _assert_waiting(coordinator, state, originals)
                    _pump_bounded(coordinator, clock, 20)
                    _assert_conversion(coordinator, state, originals)
            finally:
                coordinator.close()
            assert not any(observer.is_alive() for observer in observers)
            assert not any(emitter.is_alive() for emitter in emitters)
            queued = coordinator.queued_count
            coordinator.enqueue(('modified', multipart_parts[0], None, False))
            assert coordinator.queued_count == queued


def test_concurrent_multipart_primaries_progress_when_associations_are_evicted(
    tmp_path: Path, multipart_parts: tuple[Path, ...],
) -> None:
    config = _config(tmp_path, dual_role=False)
    second_folder = multipart_parts[0].parent / "Second"
    second_folder.mkdir()
    second_parts = tuple(second_folder / part.name for part in multipart_parts)
    for source, destination in zip(multipart_parts, second_parts, strict=True):
        shutil.copyfile(source, destination)
    originals = {part: part.read_bytes() for part in (*multipart_parts, *second_parts)}
    primaries = (multipart_parts[0], second_parts[0])
    clock = Clock()
    with open_pipeline(config) as pipeline:
        state = StateStore(config.state_dir)
        executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
        coordinator = WatchCoordinator(
            pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
            state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
            clock=clock, candidate_filter=lambda path: path.name.endswith('part1.rar') or path.suffix == '.cbz',
            options={'watch_queue_capacity': 2, 'watch_max_pending_jobs': 2,
                     'watchdog_file_transferred_check_interval': 1,
                     'watchdog_discover_new_files_check_interval': 2,
                     'watch_reconcile_interval_seconds': 3},
        )
        try:
            assert coordinator.start().status == 'observed'
            for primary in primaries:
                coordinator.enqueue(('created', primary, None, False))
            _pump_bounded(coordinator, clock, 20)
            result = coordinator.snapshot()
            assert (result.committed, result.failed, result.exit_code) == (2, 0, 0), result.as_dict()
            for primary in primaries:
                with zipfile.ZipFile(primary.with_suffix('.cbz')) as archive:
                    assert archive.testzip() is None
                    assert sorted(archive.namelist()) == ['vols/bigfile.txt', 'vols/smallfile.txt']
                    assert zlib.crc32(archive.read('vols/bigfile.txt')) == 0x509ad74c
                    assert zlib.crc32(archive.read('vols/smallfile.txt')) == 0xd08a1f86
            records = state.list_operations()
            assert len(records) == 2 and all(record.phase == 'complete' for record in records)
            retained = tuple(
                member for record in records for member in state.list_members(record.operation_id)
                if member.kind == 'quarantine'
            )
            assert {member.source.path for member in retained if member.source is not None} == set(originals)
            for member in retained:
                assert member.source is not None and member.cleanup_path is not None
                assert member.cleanup_path.read_bytes() == originals[member.source.path]
                assert not member.source.path.exists()
            _pump_bounded(coordinator, clock, 10)
            assert coordinator.snapshot().committed == 2
        finally:
            coordinator.close()


def test_multipart_snapshot_exceeding_the_bound_requires_review_without_retiring_sources(
    tmp_path: Path, multipart_parts: tuple[Path, ...],
) -> None:
    config = _config(tmp_path, dual_role=False)
    originals = {part: part.read_bytes() for part in multipart_parts}
    clock = Clock()
    with open_pipeline(config) as pipeline:
        state = StateStore(config.state_dir)
        executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
        coordinator = WatchCoordinator(
            pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
            state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
            clock=clock, candidate_filter=lambda path: path.name.endswith('part1.rar'),
            options={'watch_queue_capacity': 1, 'watch_max_pending_jobs': 1,
                     'watchdog_file_transferred_check_interval': 1,
                     'watchdog_discover_new_files_check_interval': 2,
                     'watch_reconcile_interval_seconds': 3},
        )
        try:
            assert coordinator.start().status == 'observed'
            _pump_bounded(coordinator, clock, 10)
            result = coordinator.snapshot()
            assert result.committed == 0 and result.exit_code == 3, result.as_dict()
            assert any(decision.status == 'review_required' and decision.reason == 'watch_archive_parts_limit'
                       for decision in result.decisions)
            assert state.list_operations() == ()
            assert not multipart_parts[0].with_suffix('.cbz').exists()
            assert all(path.read_bytes() == data for path, data in originals.items())
        finally:
            coordinator.close()

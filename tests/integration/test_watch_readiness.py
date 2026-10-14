from __future__ import annotations

import zipfile
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


class Clock:
    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now


def _book(path: Path, content: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('asset.txt', content)
    return path.read_bytes()


def _config(tmp_path: Path, **changes: object) -> Configuration:
    root = tmp_path / 'downloads'
    root.mkdir(exist_ok=True)
    return validate_config({
        'paths': [str(root)], 'download_folders': [str(root)],
        'state_dir': str(tmp_path / 'state'), 'log_dir': str(tmp_path / 'logs'),
        'log_to_file': False, 'manual_rename': False,
        'manual_delete': False, 'correct_file_extensions_toggle': False,
        'convert_to_cbz_toggle': False, 'extract_covers_toggle': False,
        'check_for_existing_series_toggle': False,
        'check_for_missing_volumes_toggle': False, 'bookwalker_check': False,
        'generate_release_group_list_toggle': False,
        'delete_unacceptable_files_toggle': False,
        'delete_chapters_from_downloads_toggle': False,
        'create_folders_for_items_in_download_folder_toggle': False,
        'rename_dirs_in_download_folder_toggle': False,
        'rename_files_in_download_folders_toggle': False,
        'check_for_duplicate_volumes_toggle': True,
        'ranked_keywords': [{'name': 'Digital', 'score': 10, 'file_type': 'volume'}],
        **changes,
    }, base_dir=tmp_path)


def _pump(coordinator: WatchCoordinator, clock: Clock, seconds: int) -> None:
    for _ in range(seconds):
        clock.now += 1
        coordinator.tick()


def test_unfinished_ranked_keeper_cannot_retire_a_ready_download(tmp_path: Path) -> None:
    pytest.importorskip('lxml')
    config = _config(tmp_path)
    scan = tmp_path / 'downloads/Series/Series v01 (Scan).cbz'
    digital = scan.with_name('Series v01 (Digital).cbz')
    scan_bytes = _book(scan, b'complete scan')
    digital_bytes = _book(digital, b'not yet accepted by the downloader')
    complete = {scan: True, digital: False}
    clock = Clock()
    with open_pipeline(config) as pipeline:
        state = StateStore(config.state_dir)
        executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
        coordinator = WatchCoordinator(
            pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
            state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
            clock=clock, downloader_complete=lambda path: complete.get(path, True),
            options={'watchdog_file_transferred_check_interval': 1,
                     'watchdog_discover_new_files_check_interval': 2,
                     'watch_max_pending_jobs': 2},
        )
        try:
            assert coordinator.start().status == 'observed'
            _pump(coordinator, clock, 5)
            assert scan.read_bytes() == scan_bytes and digital.read_bytes() == digital_bytes
            assert coordinator.snapshot().committed == 0
            complete[digital] = True
            _pump(coordinator, clock, 8)
            assert not scan.exists() and digital.read_bytes() == digital_bytes
            assert coordinator.snapshot().failed == 0
            retained = [member.cleanup_path for record in state.list_operations()
                        for member in state.list_members(record.operation_id)
                        if member.kind == 'quarantine' and member.source is not None
                        and member.source.path == scan]
            assert len(retained) == 1 and retained[0] is not None and retained[0].read_bytes() == scan_bytes
        finally:
            coordinator.close()


def test_connected_series_larger_than_pending_limit_makes_progress(tmp_path: Path) -> None:
    pytest.importorskip('lxml')
    config = _config(tmp_path, rename_files_in_download_folders_toggle=True,
                     check_for_duplicate_volumes_toggle=False)
    originals = {number: _book(tmp_path / f'downloads/Series/Series Volume {number}.cbz', str(number).encode())
                 for number in range(1, 5)}
    clock = Clock()
    with open_pipeline(config) as pipeline:
        state = StateStore(config.state_dir)
        executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
        coordinator = WatchCoordinator(
            pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
            state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
            clock=clock, options={'watchdog_file_transferred_check_interval': 1,
                                 'watchdog_discover_new_files_check_interval': 2,
                                 'watch_max_pending_jobs': 2},
        )
        try:
            assert coordinator.start().status == 'observed'
            _pump(coordinator, clock, 20)
            for number, content in originals.items():
                assert not (tmp_path / f'downloads/Series/Series Volume {number}.cbz').exists()
                assert (tmp_path / f'downloads/Series/Series v{number:02d}.cbz').read_bytes() == content
            assert coordinator.snapshot().committed == 4 and coordinator.snapshot().failed == 0
        finally:
            coordinator.close()


def test_removed_internal_workspace_event_does_not_poison_visible_directory_intake(tmp_path: Path) -> None:
    pytest.importorskip('lxml')
    config = _config(tmp_path, rename_files_in_download_folders_toggle=True,
                     check_for_duplicate_volumes_toggle=False)
    root = tmp_path / 'downloads'
    clock = Clock()
    with open_pipeline(config) as pipeline:
        state = StateStore(config.state_dir)
        executor = TransactionExecutor(config.state_dir, tuple(root.path for root in pipeline.roots))
        coordinator = WatchCoordinator(
            pipeline, RunRequest('watch', authorization=RunAuthorization(apply=True)),
            state=state, journal_ready=lambda: executor.prepare_state_root(authorization=RunAuthorization(apply=True)),
            clock=clock, options={'watchdog_file_transferred_check_interval': 1,
                                 'watchdog_discover_new_files_check_interval': 2},
        )
        try:
            assert coordinator.start().status == 'observed'
            hidden = root / '.kce-artwork-run-regression'
            hidden.mkdir()
            coordinator.enqueue(('created', hidden, None, True))
            hidden.rmdir()
            coordinator.tick()
            outside = tmp_path / 'Incoming'
            original = _book(outside / 'Series Volume 1.cbz', b'visible incoming archive')
            destination = root / 'Series'
            outside.rename(destination)
            coordinator.enqueue(('moved', outside, destination, True))
            _pump(coordinator, clock, 8)
            assert (destination / 'Series v01.cbz').read_bytes() == original
            result = coordinator.snapshot()
            assert result.exit_code == 0 and result.committed == 1
            assert not any(decision.reason == 'watch_reconciliation_incomplete' for decision in result.decisions)
        finally:
            coordinator.close()


def test_stale_ready_fingerprint_does_not_authorize_new_source_bytes(tmp_path: Path) -> None:
    pytest.importorskip('lxml')
    config = _config(tmp_path, rename_files_in_download_folders_toggle=True)
    source = tmp_path / 'downloads/Series/Series Volume 1.cbz'
    _book(source, b'previously ready archive')
    ready = fingerprint(source, tmp_path / 'downloads')
    replacement = _book(source, b'new bytes still being transferred')
    with open_pipeline(config) as pipeline:
        result = pipeline.run(RunRequest(
            'run', books=(source,), watch=True, ready_downloads=(ready,),
            authorization=RunAuthorization(apply=True),
        ))
    assert result.exit_code == 3 and result.committed == 0
    assert any(decision.reason == 'watch_scope_not_ready' for decision in result.decisions)
    assert source.read_bytes() == replacement and not source.with_name('Series v01.cbz').exists()
    assert not config.state_dir.exists()

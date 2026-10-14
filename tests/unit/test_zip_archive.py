"""Generated ZIP fixtures exercise archive boundaries without a media library."""
import io
import os
import stat
import struct
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import pytest

from komga_cover_extractor.infrastructure.zip_archive import (
    ArchiveError,
    ArchivePolicy,
    ArchiveReader,
    validate_member_name,
)


def _book(tmp_path: Path, entries: list[tuple[str | zipfile.ZipInfo, bytes]], *,
          compression: int = zipfile.ZIP_STORED) -> Path:
    path = tmp_path / 'generated.cbz'
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(path, 'w', compression=compression) as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return path


def test_nested_members_keep_their_complete_paths_and_directory_identity(tmp_path: Path) -> None:
    path = _book(tmp_path, [('OPS/images/', b''), ('OPS/images/front.png', b'front'),
                            ('other/front.png', b'decoy')])
    with ArchiveReader(path, ArchivePolicy()) as reader:
        members = reader.members()
        assert [(member.name, member.is_dir, member.raw_name) for member in members] == [
            ('OPS/images', True, 'OPS/images/'),
            ('OPS/images/front.png', False, 'OPS/images/front.png'),
            ('other/front.png', False, 'other/front.png'),
        ]
        assert reader.read_member('OPS/images/front.png') == b'front'
        assert reader.read_member('other/front.png') == b'decoy'
    assert path.is_file()
    with pytest.raises(ArchiveError) as failure:
        reader.members()
    assert failure.value.reason == 'archive_closed'


@pytest.mark.parametrize('name', ['../outside.jpg', 'safe/../outside.jpg', '/absolute.jpg',
                                 'C:/drive.jpg', 'C:relative.jpg', '//server/share.jpg',
                                 r'\\server\share.jpg', r'safe\..\outside.jpg', '', '.', '/'])
def test_unsafe_raw_member_names_are_rejected_before_reading(name: str) -> None:
    with pytest.raises(ArchiveError) as failure:
        validate_member_name(name)
    assert failure.value.reason == 'unsafe_member_name'


def test_harmless_dot_segments_have_a_single_posix_identity() -> None:
    assert validate_member_name('OPS//./images/front.jpg') == 'OPS/images/front.jpg'


def test_raw_nul_name_is_rejected_instead_of_accepting_zipfile_truncation(tmp_path: Path) -> None:
    path = _book(tmp_path, [('pageX.png', b'page')])
    path.write_bytes(path.read_bytes().replace(b'pageX.png', b'page\x00.png'))
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'unsafe_member_name'
    assert path.is_file()


@pytest.mark.parametrize('names', [
    ['page.png', 'page.png'], ['OPS/page.png', 'OPS//page.png'],
    ['OPS/./page.png', 'OPS/page.png'], ['page.png/', 'page.png'],
])
def test_duplicate_raw_or_canonical_paths_fail_preflight(tmp_path: Path, names: list[str]) -> None:
    path = _book(tmp_path, [(name, b'') for name in names])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'duplicate_archive_member'


@pytest.mark.parametrize('mode', [stat.S_IFLNK, stat.S_IFSOCK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK])
def test_unix_links_and_special_entries_are_not_regular_files(tmp_path: Path, mode: int) -> None:
    entry = zipfile.ZipInfo('special')
    entry.create_system = 3
    entry.external_attr = (mode | 0o600) << 16
    path = _book(tmp_path, [(entry, b'outside')])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'special_archive_member'


@pytest.mark.parametrize('attributes', [0x400, 0x40, 0x08])
def test_reparse_device_and_volume_label_entries_fail_preflight(tmp_path: Path, attributes: int) -> None:
    entry = zipfile.ZipInfo('special')
    entry.external_attr = (stat.S_IFREG | 0o600) << 16 | attributes
    path = _book(tmp_path, [(entry, b'outside')])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'special_archive_member'


def test_unix_extra_field_link_is_rejected_without_following_it(tmp_path: Path) -> None:
    entry = zipfile.ZipInfo('link')
    payload = b'\x00' * 12 + b'../../outside'
    entry.extra = struct.pack('<HH', 0x000D, len(payload)) + payload
    path = _book(tmp_path, [(entry, b'')])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'special_archive_member'


def test_file_cannot_also_be_an_implied_parent_directory(tmp_path: Path) -> None:
    path = _book(tmp_path, [('OPS', b'file'), ('OPS/page.png', b'page')])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'file_directory_conflict'


@pytest.mark.parametrize('flag', [0x01, 0x40])
def test_encrypted_entries_are_an_explicit_unsupported_input(tmp_path: Path, flag: int) -> None:
    path = _book(tmp_path, [('page.png', b'page')])
    raw = bytearray(path.read_bytes())
    local, central = raw.index(b'PK\x03\x04'), raw.index(b'PK\x01\x02')
    struct.pack_into('<H', raw, local + 6, flag)
    struct.pack_into('<H', raw, central + 8, flag)
    path.write_bytes(raw)
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert (failure.value.status, failure.value.reason) == ('unsupported', 'encrypted_archive_member')
    assert path.read_bytes() == raw


def test_entry_budget_includes_directory_entries_at_limit_and_limit_plus_one(tmp_path: Path) -> None:
    path = _book(tmp_path, [('images/', b''), ('images/page.png', b'page')])
    with ArchiveReader(path, ArchivePolicy(archive_max_entries=2)) as reader:
        assert reader.read_member('images/page.png') == b'page'
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy(archive_max_entries=1)):
            pass
    assert failure.value.reason == 'archive_entry_limit'


@pytest.mark.parametrize('field,reason', [
    ('conversion_member_max_bytes', 'archive_member_limit'),
    ('archive_expansion_max_bytes', 'archive_expansion_limit'),
])
def test_declared_byte_limits_accept_exact_size_and_reject_one_more(
    tmp_path: Path, field: str, reason: str,
) -> None:
    path = _book(tmp_path, [('page.png', b'four')])
    with ArchiveReader(path, replace(ArchivePolicy(), **{field: 4})) as reader:
        assert reader.read_member('page.png') == b'four'
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, replace(ArchivePolicy(), **{field: 3})):
            pass
    assert failure.value.reason == reason


def test_declared_expansion_counts_all_members_not_only_selected_cover(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'abcd'), ('metadata.xml', b'efgh')])
    with ArchiveReader(path, ArchivePolicy(archive_expansion_max_bytes=8)) as reader:
        assert reader.read_member('page.png') == b'abcd'
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy(archive_expansion_max_bytes=7)):
            pass
    assert failure.value.reason == 'archive_expansion_limit'


def test_ratio_limit_is_enforced_before_member_reads_at_exact_boundary(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'a' * 256)], compression=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(path) as archive:
        entry = archive.getinfo('page.png')
        ratio = entry.file_size / entry.compress_size
    with ArchiveReader(path, ArchivePolicy(archive_max_ratio=ratio)) as reader:
        assert reader.read_member('page.png') == b'a' * 256
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy(archive_max_ratio=ratio - 0.01)):
            pass
    assert failure.value.reason == 'archive_ratio_limit'


def test_cumulative_reads_do_not_reset_on_reopening_the_same_member(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'four')])
    with ArchiveReader(path, ArchivePolicy(cover_read_max_bytes=8)) as reader:
        assert reader.read_member('page.png') == b'four'
        with reader.open_member('page.png') as stream:
            assert stream.read(2) == b'fo'
            assert stream.read(2) == b'ur'
        assert reader.read_bytes == 8
        extra = reader.open_member('page.png')
        with pytest.raises(ArchiveError) as failure:
            extra.read(1)
        assert failure.value.reason == 'archive_read_limit'
        assert extra.closed
        assert reader.read_bytes == 9
    assert path.is_file()


def test_member_read_limit_counts_chunks_and_does_not_return_partial_success(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'abcd')])
    with ArchiveReader(path, ArchivePolicy()) as reader:
        assert reader.read_member('page.png', max_bytes=4) == b'abcd'
        with pytest.raises(ArchiveError) as failure:
            reader.read_member('page.png', max_bytes=3)
        assert failure.value.reason == 'archive_member_limit'


def test_actual_decoder_bytes_cannot_exceed_declared_or_cumulative_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _book(tmp_path, [('page.png', b'four')])
    decoded = io.BytesIO(b'oversized decoder output')
    def bad_decoder(archive: zipfile.ZipFile, *args: object, **kwargs: object) -> BinaryIO:
        return decoded
    monkeypatch.setattr(zipfile.ZipFile, 'open', bad_decoder)
    with ArchiveReader(path, ArchivePolicy(cover_read_max_bytes=4, conversion_member_max_bytes=4)) as reader:
        with pytest.raises(ArchiveError) as failure:
            reader.read_member('page.png')
        assert failure.value.reason == 'archive_read_limit'
        assert decoded.closed
        assert reader.read_bytes == 5
    assert path.is_file()


def test_bad_member_checksum_closes_every_scoped_handle_and_retains_input(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'original-encoded-page'), ('second.png', b'second')])
    corrupted = path.read_bytes().replace(b'original-encoded-page', b'corrupt!-encoded-page')
    path.write_bytes(corrupted)
    with ArchiveReader(path, ArchivePolicy()) as reader:
        first, second = reader.open_member('page.png'), reader.open_member('second.png')
        with pytest.raises(ArchiveError) as failure:
            first.read()
        assert failure.value.reason == 'archive_member_checksum'
        assert first.closed and second.closed
    assert path.read_bytes() == corrupted


def test_readinto_and_member_close_preserve_other_owned_streams(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'abcdef'), ('metadata.xml', b'metadata')])
    with ArchiveReader(path, ArchivePolicy()) as reader:
        page = reader.open_member('page.png')
        buffer = bytearray(4)
        assert page.readinto(buffer) == 4 and buffer == b'abcd'
        assert page.read(None) == b'ef'
        page.close()
        assert page.closed and reader.read_member('metadata.xml') == b'metadata'


def test_truncated_zip_is_invalid_not_an_empty_valid_archive(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'page')])
    path.write_bytes(path.read_bytes()[:-15])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()):
            pass
    assert failure.value.reason == 'invalid_zip_archive'
    assert path.is_file()


def test_changed_archive_identity_is_stale_even_with_original_open_descriptor(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'page')])
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(path, ArchivePolicy()) as reader:
            replacement = tmp_path / 'replacement'
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            reader.read_member('page.png')
    assert (failure.value.status, failure.value.reason) == ('stale_plan', 'source_changed')
    assert path.is_file()


def test_symlinked_source_is_refused_before_opening(tmp_path: Path) -> None:
    target = _book(tmp_path, [('page.png', b'page')])
    link = tmp_path / 'linked.cbz'
    link.symlink_to(target)
    with pytest.raises(ArchiveError) as failure:
        with ArchiveReader(link, ArchivePolicy()):
            pass
    assert failure.value.reason == 'symlink_traversal'
    assert target.is_file() and link.is_symlink()


@pytest.mark.parametrize('change', [
    {'archive_max_entries': True}, {'metadata_max_bytes': 0},
    {'archive_max_ratio': float('nan')}, {'archive_max_ratio': -1},
    {'extra_field': 1},
])
def test_worker_policy_boundary_rejects_untrusted_shapes(change: dict[str, object]) -> None:
    request = ArchivePolicy().as_dict() | change
    with pytest.raises(ArchiveError) as failure:
        ArchivePolicy.from_dict(request)
    assert failure.value.reason == 'invalid_archive_policy'


def test_serialized_policy_controls_the_actual_reader_boundary(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'page')])
    policy = ArchivePolicy.from_dict(ArchivePolicy(cover_read_max_bytes=4).as_dict())
    with ArchiveReader(path, policy) as reader:
        assert reader.read_member('page.png') == b'page'
        with pytest.raises(ArchiveError) as failure:
            reader.read_member('page.png')
        assert failure.value.reason == 'archive_read_limit'


def test_actual_expansion_budget_is_shared_across_repeated_streams(tmp_path: Path) -> None:
    path = _book(tmp_path, [('page.png', b'four')])
    policy = ArchivePolicy(archive_expansion_max_bytes=4, cover_read_max_bytes=32)
    with ArchiveReader(path, policy) as reader:
        assert reader.read_member('page.png') == b'four'
        with pytest.raises(ArchiveError) as failure:
            reader.read_member('page.png')
        assert failure.value.reason == 'archive_read_limit' and reader.read_bytes == 5


def test_failed_preflight_releases_only_the_owned_source_descriptor(tmp_path: Path) -> None:
    path = _book(tmp_path, [('../unsafe.jpg', b'unsafe')])
    def descriptors() -> tuple[int, ...]:
        found = []
        for descriptor in Path('/proc/self/fd').iterdir():
            try:
                if os.readlink(descriptor) == str(path):
                    found.append(int(descriptor.name))
            except OSError:
                continue
        return tuple(sorted(found))
    reader = ArchiveReader(path, ArchivePolicy())
    with path.open('rb') as unrelated:
        assert descriptors() == (unrelated.fileno(),)
        with pytest.raises(ArchiveError) as failure:
            with reader:
                pass
        assert failure.value.reason == 'unsafe_member_name'
        assert descriptors() == (unrelated.fileno(),)
        assert unrelated.read(2) == b'PK'
    assert descriptors() == ()
    assert path.read_bytes().startswith(b'PK')

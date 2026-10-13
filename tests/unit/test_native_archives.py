from __future__ import annotations

import io
from dataclasses import replace
from typing import BinaryIO, cast

import pytest

from komga_cover_extractor.infrastructure.seven_zip import (
    ExtractionBudget,
    MemberStream,
    ReadBudget,
    check_members,
    make_writer_factory,
)
from komga_cover_extractor.infrastructure.zip_archive import (
    ArchiveError,
    ArchiveMember,
    ArchivePolicy,
)


def test_native_stream_counts_reads_across_members_and_reopens() -> None:
    budget = ReadBudget(5)
    page = ArchiveMember('pages/01.jpg', 3, 3, False, 'pages/01.jpg')
    metadata = ArchiveMember('ComicInfo.xml', 2, 2, False, 'ComicInfo.xml')
    first = MemberStream(cast(BinaryIO, io.BytesIO(b'abc')), page, budget, limit=3)
    second = MemberStream(cast(BinaryIO, io.BytesIO(b'xy')), metadata, budget, limit=2)
    assert first.read(2) == b'ab'
    assert first.read(2) == b'c'
    assert second.read() == b'xy'
    assert first.read() == b''
    reopened = MemberStream(cast(BinaryIO, io.BytesIO(b'abc')), page, budget, limit=3)
    with pytest.raises(ArchiveError) as error:
        reopened.read(1)
    assert error.value.status == 'invalid_input'
    assert error.value.reason == 'archive_read_limit'


def test_native_stream_rejects_truncated_declared_data() -> None:
    page = ArchiveMember('page.jpg', 3, 3, False, 'page.jpg')
    with MemberStream(cast(BinaryIO, io.BytesIO(b'ab')), page, ReadBudget(4), limit=3) as stream:
        with pytest.raises(ArchiveError) as error:
            stream.read()
    assert error.value.reason == 'archive_member_size_mismatch'


def test_native_stream_rejects_one_chunk_above_member_limit() -> None:
    page = ArchiveMember('page.jpg', 5, 5, False, 'page.jpg')
    with MemberStream(cast(BinaryIO, io.BytesIO(b'abcde')), page, ReadBudget(10), limit=4) as stream:
        with pytest.raises(ArchiveError) as error:
            stream.read()
    assert error.value.reason == 'archive_member_limit'


def test_py7zr_factory_checks_whole_chunk_before_writing() -> None:
    pytest.importorskip('py7zr', reason='conversion extra is required for the actual WriterFactory ABC')
    output = io.BytesIO()
    page = ArchiveMember('pages/01.jpg', 8, 8, False, 'pages/01.jpg')
    factory = make_writer_factory(
        (page,), policy=replace(ArchivePolicy(), conversion_member_max_bytes=4),
        open_output=lambda _: cast(BinaryIO, output), completed=lambda _member, _digest: None,
        total_limit=10,
    )
    writer = factory.create('pages/01.jpg')
    assert writer.write(b'abc') == 3
    with pytest.raises(ArchiveError):
        writer.write(b'de')
    assert output.getvalue() == b'abc'
    assert writer.size() == 3


def test_py7zr_factory_has_one_quota_for_all_members() -> None:
    pytest.importorskip('py7zr', reason='conversion extra is required for the actual WriterFactory ABC')
    outputs = {'one': io.BytesIO(), 'two': io.BytesIO()}
    members = tuple(ArchiveMember(name, 4, 4, False, name) for name in outputs)
    factory = make_writer_factory(
        members, policy=ArchivePolicy(), open_output=lambda member: cast(BinaryIO, outputs[member.name]),
        completed=lambda _member, _digest: None, total_limit=5,
    )
    first, second = factory.create('one'), factory.create('two')
    assert first.write(b'abcd') == 4
    assert second.write(b'x') == 1
    with pytest.raises(ArchiveError):
        second.write(b'y')
    assert outputs['one'].getvalue() == b'abcd'
    assert outputs['two'].getvalue() == b'x'


@pytest.mark.parametrize('name', ['../page.jpg', '/page.jpg', 'C:/page.jpg', 'other.jpg'])
def test_py7zr_factory_refuses_unregistered_destination(name: str) -> None:
    pytest.importorskip('py7zr', reason='conversion extra is required for the actual WriterFactory ABC')
    output = io.BytesIO()
    page = ArchiveMember('page.jpg', 1, 1, False, 'page.jpg')
    factory = make_writer_factory(
        (page,), policy=ArchivePolicy(), open_output=lambda _: cast(BinaryIO, output),
        completed=lambda _member, _digest: None, total_limit=1,
    )
    with pytest.raises(ArchiveError):
        factory.create(name)
    assert output.getvalue() == b''
    assert factory.create('page.jpg').write(b'x') == 1
    assert output.getvalue() == b'x'


def test_native_file_directory_collision_is_rejected_before_output() -> None:
    members = (
        ArchiveMember('pages', 1, 1, False, 'pages'),
        ArchiveMember('pages/01.jpg', 1, 1, False, 'pages/01.jpg'),
    )
    with pytest.raises(ArchiveError) as error:
        check_members(members, ArchivePolicy(), packed_size=2)
    assert error.value.reason == 'archive_path_collision'


@pytest.mark.parametrize('limit,accepted', [(2, True), (1, False)])
def test_native_preflight_entry_count_boundary(limit: int, accepted: bool) -> None:
    members = (ArchiveMember('one', 1, 1, False, 'one'), ArchiveMember('two', 1, 1, False, 'two'))
    policy = replace(ArchivePolicy(), archive_max_entries=limit)
    if accepted:
        check_members(members, policy, packed_size=2)
        with MemberStream(cast(BinaryIO, io.BytesIO(b'x')), members[0], ReadBudget(1), limit=1) as stream:
            assert stream.read() == b'x'
    else:
        with pytest.raises(ArchiveError) as error:
            check_members(members, policy, packed_size=2)
        assert error.value.reason == 'archive_entry_limit'


@pytest.mark.parametrize('limit,accepted', [(4, True), (3, False)])
def test_native_preflight_expansion_boundary(limit: int, accepted: bool) -> None:
    members = (ArchiveMember('one', 2, 2, False, 'one'), ArchiveMember('two', 2, 2, False, 'two'))
    policy = replace(ArchivePolicy(), archive_expansion_max_bytes=limit)
    if accepted:
        check_members(members, policy, packed_size=4)
        budget = ReadBudget(4)
        assert MemberStream(cast(BinaryIO, io.BytesIO(b'ab')), members[0], budget, limit=2).read() == b'ab'
        assert MemberStream(cast(BinaryIO, io.BytesIO(b'cd')), members[1], budget, limit=2).read() == b'cd'
    else:
        with pytest.raises(ArchiveError) as error:
            check_members(members, policy, packed_size=4)
        assert error.value.reason == 'archive_expansion_limit'


def test_native_factories_share_actual_bytes_after_failed_member() -> None:
    pytest.importorskip('py7zr', reason='conversion extra is required for the actual WriterFactory ABC')
    budget = ExtractionBudget(4, 5)
    first_output, second_output = io.BytesIO(), io.BytesIO()
    first_member = ArchiveMember('one', 4, 4, False, 'one')
    second_member = ArchiveMember('two', 4, 4, False, 'two')
    first_factory = make_writer_factory(
        (first_member,), policy=ArchivePolicy(), open_output=lambda _: cast(BinaryIO, first_output),
        completed=lambda _member, _digest: None, total_limit=5, extraction_budget=budget,
    )
    first = first_factory.create('one')
    assert first.write(b'abc') == 3
    with pytest.raises(ArchiveError):
        first.close()
    second_factory = make_writer_factory(
        (second_member,), policy=ArchivePolicy(), open_output=lambda _: cast(BinaryIO, second_output),
        completed=lambda _member, _digest: None, total_limit=5, extraction_budget=budget,
    )
    second = second_factory.create('two')
    assert second.write(b'xy') == 2
    with pytest.raises(ArchiveError):
        second.write(b'z')
    assert second_output.getvalue() == b'xy'

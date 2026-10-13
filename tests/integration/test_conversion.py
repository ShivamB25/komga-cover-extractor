from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import sys
import time
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from komga_cover_extractor.application.convert import convert_book, observe_header
from komga_cover_extractor.domain.decisions import RunAuthorization
from komga_cover_extractor.domain.operations import (
    ArtifactSpec,
    Operation,
    OperationPlan,
    RootPermission,
)
from komga_cover_extractor.infrastructure.filesystem import fingerprint
from komga_cover_extractor.infrastructure.transactions import TransactionExecutor
from komga_cover_extractor.infrastructure.workers import WorkerError, run_worker
from komga_cover_extractor.infrastructure.zip_archive import ArchivePolicy

NATIVE_FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'native'


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    library = tmp_path / 'library'
    scratch = tmp_path / 'preview-scratch'
    library.mkdir()
    scratch.mkdir()
    return library, scratch


def _sevenzip(path: Path, entries: dict[str, bytes], *, password: str | None = None,
              raw_names: tuple[str, ...] | None = None) -> None:
    """Generated synthetic 7z data, written only while a test executes."""
    py7zr = pytest.importorskip('py7zr', reason='actual 7z tests require the conversion extra')
    with py7zr.SevenZipFile(path, 'w', filters=[{'id': py7zr.FILTER_COPY}],
                           password=password, header_encryption=password is not None) as archive:
        for name, data in entries.items():
            archive.writestr(data, name)
        if raw_names is not None:
            for entry, name in zip(archive.header.files_info.files, raw_names, strict=True):
                entry['filename'] = name


def _rar(library: Path, name: str, *, destination: str | None = None) -> Path:
    pytest.importorskip('rarfile', reason='actual RAR tests require the conversion extra')
    path = library / (destination or name)
    shutil.copyfile(NATIVE_FIXTURES / name, path)
    return path


def _rewrite_rar5_name(book: Path, original: str, replacement: str) -> None:
    """Generate adversarial headers from licensed stored data, without a RAR compressor."""
    rarfile = pytest.importorskip('rarfile', reason='RAR header fixtures require the conversion extra')
    with rarfile.RarFile(book, errors='strict') as archive:
        info = archive.getinfo(original)
        start, end = info.header_offset, info.header_offset + info.header_size
        raw_name = info.orig_filename
    data = bytearray(book.read_bytes())
    header = data[start:end]
    offset = header.index(raw_name)
    encoded = replacement.encode('utf-8')
    assert header[4] < 128 and len(raw_name) < 128 and len(encoded) < 128
    assert header[offset - 1] == len(raw_name)
    delta = len(encoded) - len(raw_name)
    header[offset:offset + len(raw_name)] = encoded
    header[offset - 1] = len(encoded)
    header[4] += delta
    header[:4] = zlib.crc32(header[4:]).to_bytes(4, 'little')
    data[start:end] = header
    book.write_bytes(data)


def _native_result(book: Path, library: Path, scratch: Path,
                   policy: ArchivePolicy | None = None):
    result = convert_book(book, root=library, scratch_dir=scratch, policy=policy or ArchivePolicy())
    if result.status == 'missing_dependency' and shutil.which('unrar') is None:
        pytest.skip(f'Actual native RAR decoding prerequisite unavailable: {result.reason}')
    return result


def test_actual_7z_preserves_nested_images_xml_opf_and_extensionless_assets(roots) -> None:
    library, scratch = roots
    image = pytest.importorskip('PIL.Image', reason='this generated image fixture needs the covers extra')
    red = io.BytesIO()
    image.new('RGB', (8, 8), 'red').save(red, format='PNG')
    entries = {
        'pages/01.png': red.getvalue(),
        'ComicInfo.xml': b'<ComicInfo><Publisher>Fixture Press</Publisher></ComicInfo>',
        'OPS/package.opf': b'<package xmlns="http://www.idpf.org/2007/opf"/>',
        'assets/LICENSE': b'Generated fixture assets, not a real book.\n',
        'other/01.png': b'an opaque non-cover asset with the same basename',
    }
    book = library / 'Series v01.7z'
    _sevenzip(book, entries)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'valid', result.reason
    assert result.container == '7z'
    assert result.artifact is not None
    assert result.artifact.root == scratch
    assert result.sources == (fingerprint(book, library),)
    assert result.manifest == tuple(sorted((name, hashlib.sha256(data).hexdigest()) for name, data in entries.items()))
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.testzip() is None
        assert {name: archive.read(name) for name in archive.namelist()} == entries
    assert book.read_bytes() == original
    assert not book.with_suffix('.cbz').exists()
    assert sorted(path.name for path in library.iterdir()) == ['Series v01.7z']


def test_apply_stage_is_rooted_in_original_library_and_publication_retains_source(roots) -> None:
    library, _ = roots
    scratch = library / '.kce-convert-test'
    scratch.mkdir()
    book = library / 'Series v01.7z'
    _sevenzip(book, {'pages/01.txt': b'page bytes', 'ComicInfo.xml': b'<ComicInfo/>'})
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'valid', result.reason
    assert result.artifact is not None
    assert result.artifact.root == library
    target = book.with_suffix('.cbz')
    plan = OperationPlan('conversion-test', 'publish-conversion', (
        Operation('book', 'publish', target, artifact=ArtifactSpec(source=result.artifact,
                                                                  media_type='application/zip')),
    ), (RootPermission(library),))
    committed = TransactionExecutor(library.parent / 'state', (library,)).apply(
        plan, authorization=RunAuthorization(apply=True),
    )
    assert committed.committed == 1
    with zipfile.ZipFile(target) as archive:
        assert archive.read('pages/01.txt') == b'page bytes'
    assert fingerprint(book, library) == result.sources[0]
    assert result.artifact.path.is_file()


def test_existing_different_cbz_survives_conversion_and_executor_conflict(roots) -> None:
    library, _ = roots
    scratch = library / '.kce-convert-collision'
    scratch.mkdir()
    book = library / 'Series v01.7z'
    _sevenzip(book, {'pages/01.txt': b'new book'})
    target = book.with_suffix('.cbz')
    target.write_bytes(b'an existing invalid CBZ is still user data')
    original = book.read_bytes()
    old_target = target.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'valid', result.reason
    assert result.artifact is not None
    plan = OperationPlan('conversion-test', 'collision-conversion', (
        Operation('book', 'publish', target, artifact=ArtifactSpec(source=result.artifact)),
    ), (RootPermission(library),))
    outcome = TransactionExecutor(library.parent / 'state', (library,)).apply(
        plan, authorization=RunAuthorization(apply=True),
    )
    assert outcome.committed == 0
    assert any(decision.status in {'conflict', 'stale_plan', 'review_required'} for decision in outcome.decisions)
    assert target.read_bytes() == old_target
    assert book.read_bytes() == original
    assert result.artifact.path.is_file()


@pytest.mark.parametrize('raw_name', ['../outside.txt', '/outside.txt', 'C:/outside.txt',
                                      '//server/share.txt', 'dir/../../outside.txt', 'dir\\..\\outside.txt'])
def test_actual_7z_refuses_unsafe_raw_names_without_extraction(roots, raw_name: str) -> None:
    library, scratch = roots
    book = library / 'unsafe.7z'
    _sevenzip(book, {'safe.txt': b'unsafe member fixture'}, raw_names=(raw_name,))
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []
    assert not (library.parent / 'outside.txt').exists()


@pytest.mark.parametrize('raw_names', [('same.txt', 'same.txt'), ('dir/a.txt', 'dir/./a.txt')])
def test_actual_7z_rejects_raw_and_canonical_duplicates(roots, raw_names) -> None:
    library, scratch = roots
    book = library / 'duplicates.7z'
    _sevenzip(book, {'first.txt': b'one', 'second.txt': b'two'}, raw_names=raw_names)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_actual_7z_refuses_symlink_member_before_native_decode(roots) -> None:
    library, scratch = roots
    py7zr = pytest.importorskip('py7zr', reason='actual 7z tests require the conversion extra')
    target = library / 'source.txt'
    target.write_bytes(b'symlink target must remain untouched')
    link = library / 'source-link'
    link.symlink_to(target.name)
    book = library / 'linked.7z'
    with py7zr.SevenZipFile(book, 'w', dereference=False) as archive:
        archive.write(link, 'pages/link')
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input'
    assert result.artifact is None
    assert target.read_bytes() == b'symlink target must remain untouched'
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('field,limit,expected', [
    ('conversion_member_max_bytes', 4, 'valid'), ('conversion_member_max_bytes', 3, 'invalid_input'),
    ('archive_expansion_max_bytes', 8, 'valid'), ('archive_expansion_max_bytes', 7, 'invalid_input'),
    ('archive_max_entries', 2, 'valid'), ('archive_max_entries', 1, 'invalid_input'),
    ('archive_max_ratio', 1, 'valid'), ('archive_max_ratio', .01, 'invalid_input'),
])
def test_actual_7z_policy_boundaries(roots, field: str, limit, expected: str) -> None:
    library, scratch = roots
    book = library / 'bounded.7z'
    _sevenzip(book, {'a.txt': b'abcd', 'b.txt': b'efgh'})
    original = book.read_bytes()
    policy = replace(ArchivePolicy(), **{field: limit})
    result = convert_book(book, root=library, scratch_dir=scratch, policy=policy)
    assert result.status == expected, result.reason
    if expected == 'valid':
        assert result.artifact is not None
        with zipfile.ZipFile(result.artifact.path) as archive:
            assert archive.read('a.txt') == b'abcd'
            assert archive.read('b.txt') == b'efgh'
    else:
        assert result.artifact is None
        assert list(scratch.iterdir()) == []
    assert book.read_bytes() == original


def test_actual_7z_scratch_file_budget_at_limit_and_limit_plus_one(roots) -> None:
    library, scratch = roots
    book = library / 'scratch-budget.7z'
    _sevenzip(book, {'pages/01.txt': b'abcde'})
    first = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert first.status == 'valid', first.reason
    assert first.artifact is not None
    size = first.artifact.size
    exactly = convert_book(book, root=library, scratch_dir=scratch,
                           policy=replace(ArchivePolicy(), conversion_scratch_max_bytes=size))
    too_small = convert_book(book, root=library, scratch_dir=scratch,
                             policy=replace(ArchivePolicy(), conversion_scratch_max_bytes=size - 1))
    assert exactly.status == 'valid', exactly.reason
    assert exactly.artifact is not None and exactly.artifact.size == size
    assert too_small.status == 'invalid_input'
    assert too_small.reason == 'conversion_scratch_limit'
    assert too_small.artifact is None
    assert sorted(path.stat().st_size for path in scratch.iterdir()) == [size, size]
    assert fingerprint(book, library) == first.sources[0]


@pytest.mark.parametrize('encrypted', [False, True])
def test_corrupt_or_encrypted_actual_7z_retains_source_and_owned_scratch(roots, encrypted: bool) -> None:
    library, scratch = roots
    book = library / 'bad.7z'
    _sevenzip(book, {'page.txt': b'original archive payload'}, password='fixture-password' if encrypted else None)
    if not encrypted:
        data = bytearray(book.read_bytes())
        data[32] ^= 1
        book.write_bytes(data)
    unrelated = scratch / 'unrelated.txt'
    unrelated.write_bytes(b'not owned by conversion')
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == ('unsupported' if encrypted else 'invalid_input')
    assert result.artifact is None
    assert book.read_bytes() == original
    assert unrelated.read_bytes() == b'not owned by conversion'
    assert list(scratch.iterdir()) == [unrelated]


def test_worker_refuses_existing_output_without_deleting_invalid_target(roots) -> None:
    library, scratch = roots
    book = library / 'exclusive.7z'
    _sevenzip(book, {'page.txt': b'archive data'})
    output = scratch / 'existing.cbz'
    output.write_bytes(b'not a ZIP but still not ours to delete')
    with pytest.raises(WorkerError) as error:
        run_worker('convert', {'book': str(book), 'root': str(library), 'output': str(output),
                                      'scratch_dir': str(scratch), 'archive_policy': ArchivePolicy().as_dict()})
    assert error.value.status == 'invalid_input'
    assert output.read_bytes() == b'not a ZIP but still not ours to delete'
    assert sorted(path.name for path in library.iterdir()) == ['exclusive.7z']


def test_cancelled_conversion_does_not_create_an_artifact(roots) -> None:
    library, scratch = roots
    book = library / 'cancel.7z'
    _sevenzip(book, {'page.txt': b'archive data'})
    original = book.read_bytes()
    cancel = Event()
    cancel.set()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy(), cancel=cancel)
    assert result.status == 'cancelled'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('mimetype,container_xml', [
    (b'application/epub+zip', None),
    (None, b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles/></container>'),
    (None, b'malformed metadata is not permission to rename EPUB to a comic'),
])
def test_header_correction_never_treats_epub_structure_as_comic(roots, mimetype, container_xml) -> None:
    library, _ = roots
    book = library / 'wrong.cbz'
    with zipfile.ZipFile(book, 'w') as archive:
        if mimetype is not None:
            archive.writestr('mimetype', mimetype)
        if container_xml is not None:
            archive.writestr('META-INF/container.xml', container_xml)
        archive.writestr('OPS/package.opf', b'<package/>')
    original = book.read_bytes()
    observation = observe_header(book, root=library, policy=ArchivePolicy())
    assert observation.container == 'epub'
    assert observation.target_suffix == '.epub'
    assert observation.source == fingerprint(book, library)
    assert book.read_bytes() == original
    assert not book.with_suffix('.epub').exists()


def test_header_observation_and_conversion_are_separate(roots) -> None:
    library, scratch = roots
    book = library / 'wrong.cbz'
    _sevenzip(book, {'page.txt': b'archive data'})
    observation = observe_header(book, root=library, policy=ArchivePolicy())
    assert observation.container == '7z'
    assert observation.target_suffix == '.7z'
    assert observation.source.path == book
    assert not book.with_suffix('.7z').exists()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'valid', result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.read('page.txt') == b'archive data'


def test_zip_header_without_epub_structure_has_comic_target_suffix(roots) -> None:
    library, _ = roots
    book = library / 'wrong.cbr'
    with zipfile.ZipFile(book, 'w') as archive:
        archive.writestr('page.txt', b'plain comic ZIP')
    original = book.read_bytes()
    result = observe_header(book, root=library, policy=ArchivePolicy())
    assert result.container == 'zip'
    assert result.target_suffix == '.cbz'
    assert result.source.sha256 == hashlib.sha256(original).hexdigest()
    assert book.read_bytes() == original


def test_actual_rar_nested_paths_unicode_and_empty_directories_survive(roots) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    original = book.read_bytes()
    result = _native_result(book, library, scratch)
    assert result.status == 'valid', result.reason
    assert result.container == 'rar' and result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert {info.filename: (info.file_size, info.CRC) for info in archive.infolist() if not info.is_dir()} == {
            'sub/dir2/file2.txt': (6, 0xc904a4c7),
            'sub/with space/long fn.txt': (8, 0x71b7247d),
            'sub/üȵĩöḋè/file.txt': (5, 0x2fec89c1),
            'sub/dir1/file1.txt': (6, 0xe229f704),
        }
        assert 'sub/empty/' in archive.namelist()
        assert archive.read('sub/dir1/file1.txt') == b'file1\n'
        assert archive.read('sub/dir2/file2.txt') == b'file2\n'
        assert archive.testzip() is None
    assert book.read_bytes() == original


def test_actual_rar_solid_native_decode_and_shell_metacharacter_name(roots) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-solid.rar', destination='- book ; $(not-executed).rar')
    original = book.read_bytes()
    result = _native_result(book, library, scratch)
    assert result.status == 'valid', result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert sorted(archive.namelist()) == ['stest1.txt', 'stest2.txt']
        for name in archive.namelist():
            data = archive.read(name)
            assert len(data) == 2048
            assert zlib.crc32(data) == 0xc5b7e6a2
        assert archive.read('stest1.txt') == archive.read('stest2.txt')
    assert book.read_bytes() == original
    assert sorted(path.name for path in library.iterdir()) == [book.name]


def test_actual_rar_archive_and_file_comments_survive(roots) -> None:
    library, scratch = roots
    book = _rar(library, 'rar3-comment-plain.rar')
    original = book.read_bytes()
    result = _native_result(book, library, scratch)
    assert result.status == 'valid', result.reason
    assert result.comment == b'RARcomment\n'
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.comment == b'RARcomment\n'
        assert archive.read('file1.txt') == b''
        assert archive.read('file2.txt') == b''
        assert archive.getinfo('file1.txt').comment == b'Comment1v2\n'
        assert archive.getinfo('file2.txt').comment == b'Comment2v2\n'
    assert book.read_bytes() == original


@pytest.mark.parametrize('name', ['rar5-dups.rar', 'rar5-hlink.rar', 'rar5-symlink-unix.rar', 'rar5-symlink-win.rar'])
def test_actual_rar_duplicate_or_link_headers_are_rejected_without_native_extract(roots, name: str) -> None:
    library, scratch = roots
    book = _rar(library, name)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input', result.reason
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('name', ['rar5-psw.rar', 'rar5-hpsw.rar'])
def test_actual_rar_encryption_is_explicitly_unsupported(roots, name: str) -> None:
    library, scratch = roots
    book = _rar(library, name)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'unsupported', result.reason
    assert result.reason == 'encrypted_archive'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_missing_native_unrar_is_not_replaced_by_another_backend(roots, monkeypatch) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    original = book.read_bytes()
    monkeypatch.setenv('PATH', str(library.parent / 'no-native-tools'))
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'missing_dependency'
    assert result.reason == 'unrar_native_tool_required'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_rar_header_correction_does_not_need_native_decoder(roots, monkeypatch) -> None:
    library, _ = roots
    book = _rar(library, 'rar5-subdirs.rar', destination='wrong.cbz')
    original = book.read_bytes()
    monkeypatch.setenv('PATH', str(library.parent / 'no-native-tools'))
    result = observe_header(book, root=library, policy=ArchivePolicy())
    assert result.container == 'rar'
    assert result.target_suffix == '.cbr'
    assert book.read_bytes() == original
    assert not book.with_suffix('.cbr').exists()


def test_missing_rar_multipart_volume_is_incomplete_input_not_partial_success(roots) -> None:
    library, scratch = roots
    first = _rar(library, 'rar5-vols.part1.rar')
    original = first.read_bytes()
    result = convert_book(first, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input', result.reason
    assert result.reason == 'incomplete_multipart_archive'
    assert result.artifact is None
    assert first.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_rar_multipart_symlink_part_cannot_escape_authorized_root(roots) -> None:
    library, scratch = roots
    first = _rar(library, 'rar5-vols.part1.rar')
    outside = library.parent / 'part-outside.rar'
    shutil.copyfile(NATIVE_FIXTURES / 'rar5-vols.part2.rar', outside)
    alias = library / 'rar5-vols.part2.rar'
    alias.symlink_to(outside)
    original = first.read_bytes()
    result = convert_book(first, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input', result.reason
    assert result.reason == 'symlink_traversal'
    assert result.artifact is None
    assert first.read_bytes() == original
    assert alias.is_symlink()
    assert outside.read_bytes() == (NATIVE_FIXTURES / 'rar5-vols.part2.rar').read_bytes()
    assert list(scratch.iterdir()) == []


def test_actual_complete_rar_multipart_retains_every_source_part(roots) -> None:
    library, scratch = roots
    parts = tuple(_rar(library, f'rar5-vols.part{index}.rar') for index in (1, 2, 3))
    originals = tuple(path.read_bytes() for path in parts)
    result = _native_result(parts[0], library, scratch)
    assert result.status == 'valid', result.reason
    assert result.source_parts == parts
    assert result.sources == tuple(fingerprint(path, library) for path in parts)
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert sorted(archive.namelist()) == ['vols/bigfile.txt', 'vols/smallfile.txt']
        assert len(archive.read('vols/bigfile.txt')) == 205000
        assert zlib.crc32(archive.read('vols/bigfile.txt')) == 0x509ad74c
        assert len(archive.read('vols/smallfile.txt')) == 2050
        assert zlib.crc32(archive.read('vols/smallfile.txt')) == 0xd08a1f86
    assert tuple(path.read_bytes() for path in parts) == originals
    assert not (library / 'rar5-vols.part1.cbz').exists()


def test_stalled_native_fault_injection_is_killed_and_reaped(roots, monkeypatch) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    original = book.read_bytes()
    tools = library.parent / 'fake-native-tools'
    tools.mkdir()
    marker = tools / 'native-pids.json'
    executable = tools / 'unrar'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import json, os, subprocess, sys, time\n'
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        f'with open({str(marker)!r}, "w") as stream: json.dump([os.getpid(), child.pid], stream)\n'
        'time.sleep(30)\n', encoding='utf-8',
    )
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    monkeypatch.setenv('PATH', str(tools))
    started = time.monotonic()
    result = convert_book(book, root=library, scratch_dir=scratch,
                          policy=replace(ArchivePolicy(), conversion_timeout_seconds=1))
    assert result.status == 'failed', result.reason
    assert time.monotonic() - started < 5
    assert marker.is_file(), 'the injected native process must actually have started'
    for pid in json.loads(marker.read_text(encoding='utf-8')):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert book.read_bytes() == original
    assert result.artifact is None


def test_actual_rar_declared_member_limit_fails_before_decoder_selection(roots) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch,
                          policy=replace(ArchivePolicy(), conversion_member_max_bytes=7))
    assert result.status == 'invalid_input', result.reason
    assert result.reason == 'archive_member_limit'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_actual_rar_truncated_header_does_not_become_empty_success(roots) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    truncated = book.read_bytes()[:20]
    book.write_bytes(truncated)
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input', result.reason
    assert result.artifact is None
    assert book.read_bytes() == truncated
    assert list(scratch.iterdir()) == []


def test_actual_7z_data_encryption_without_header_encryption_is_unsupported(roots) -> None:
    library, scratch = roots
    py7zr = pytest.importorskip('py7zr', reason='actual 7z tests require the conversion extra')
    book = library / 'encrypted-data.7z'
    with py7zr.SevenZipFile(book, 'w', password='generated-fixture-password', header_encryption=False) as archive:
        archive.writestr(b'data encryption must not trigger password guessing', 'page.txt')
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'unsupported', result.reason
    assert result.reason == 'encrypted_archive'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_worker_cannot_treat_final_library_destination_as_scratch_output(roots) -> None:
    library, scratch = roots
    book = library / 'source.7z'
    _sevenzip(book, {'page.txt': b'original source'})
    original = book.read_bytes()
    final_target = library / 'source.cbz'
    with pytest.raises(WorkerError) as error:
        run_worker('convert', {
            'book': str(book), 'root': str(library), 'output': str(final_target),
            'scratch_dir': str(scratch), 'archive_policy': ArchivePolicy().as_dict(),
        })
    assert error.value.status == 'invalid_input'
    assert not final_target.exists()
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('replacement', ['../outside.txt', '/outside.txt', 'C:/outside.txt',
                                        '//server/share.txt', 'dir/../../outside.txt'])
def test_actual_rar_unsafe_generated_headers_fail_without_output(roots, replacement: str) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    _rewrite_rar5_name(book, 'sub/dir1/file1.txt', replacement)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input', result.reason
    assert result.reason == 'unsafe_member_name'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []
    assert not (library.parent / 'outside.txt').exists()


@pytest.mark.parametrize('replacement', ['sub/dir2/file2.txt', './sub/dir2/file2.txt'])
def test_actual_rar_raw_and_canonical_duplicates_fail_without_output(roots, replacement: str) -> None:
    library, scratch = roots
    book = _rar(library, 'rar5-subdirs.rar')
    _rewrite_rar5_name(book, 'sub/dir1/file1.txt', replacement)
    original = book.read_bytes()
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'invalid_input', result.reason
    assert result.reason == 'duplicate_archive_member'
    assert result.artifact is None
    assert book.read_bytes() == original
    assert list(scratch.iterdir()) == []


def test_actual_7z_keeps_explicit_empty_directory(roots) -> None:
    library, scratch = roots
    py7zr = pytest.importorskip('py7zr', reason='actual 7z tests require the conversion extra')
    empty = library / 'empty-source'
    empty.mkdir()
    book = library / 'directories.7z'
    with py7zr.SevenZipFile(book, 'w') as archive:
        archive.write(empty, 'assets/empty')
        archive.writestr(b'<ComicInfo/>', 'ComicInfo.xml')
    result = convert_book(book, root=library, scratch_dir=scratch, policy=ArchivePolicy())
    assert result.status == 'valid', result.reason
    assert result.artifact is not None
    with zipfile.ZipFile(result.artifact.path) as archive:
        assert archive.getinfo('assets/empty/').is_dir()
        assert archive.read('ComicInfo.xml') == b'<ComicInfo/>'
    assert fingerprint(book, library) == result.sources[0]


@pytest.mark.parametrize('limit,expected', [(11, 'valid'), (10, 'invalid_input')])
def test_actual_rar_comment_budget_is_independent_of_native_probe_output(roots, limit, expected) -> None:
    library, scratch = roots
    book = _rar(library, 'rar3-comment-plain.rar')
    original = book.read_bytes()
    result = _native_result(book, library, scratch, replace(ArchivePolicy(), metadata_max_bytes=limit))
    assert result.status == expected, result.reason
    if expected == 'valid':
        assert result.artifact is not None
        with zipfile.ZipFile(result.artifact.path) as archive:
            assert archive.comment == b'RARcomment\n'
            assert archive.getinfo('file1.txt').comment == b'Comment1v2\n'
    else:
        assert result.artifact is None
        assert list(scratch.iterdir()) == []
    assert book.read_bytes() == original

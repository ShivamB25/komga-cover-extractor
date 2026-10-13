"""Every archive, XML document and raster in this module is generated in the test."""
import io
import zipfile
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Literal

import pytest
from PIL import Image

from komga_cover_extractor.domain.releases import FileObservation
from komga_cover_extractor.infrastructure.workers import WorkerError, run_worker
from komga_cover_extractor.infrastructure.xml_metadata import (
    read_book_metadata,
    resolve_epub_cover,
)
from komga_cover_extractor.infrastructure.zip_archive import (
    ArchivePolicy,
    ArchiveReader,
)
from komga_cover_extractor.parsing.filenames import ParsingPolicy, parse_release

_CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
_OPF = 'http://www.idpf.org/2007/opf'
_DC = 'http://purl.org/dc/elements/1.1/'


def _jpeg(color: str) -> bytes:
    output = io.BytesIO()
    with Image.new('RGB', (8, 8), color) as image:
        image.save(output, format='JPEG')
    return output.getvalue()


def _archive(tmp_path: Path, entries: list[tuple[str, bytes]], *, comment: bytes = b'',
             name: str = 'Series v01.epub') -> Path:
    book = tmp_path / name
    with zipfile.ZipFile(book, 'w') as archive:
        for path, data in entries:
            archive.writestr(path, data)
        archive.comment = comment
    return book


def _container(path: str = 'OPS/package/content.opf') -> bytes:
    return (f'<container xmlns="{_CONTAINER}" version="1.0"><rootfiles>'
            f'<rootfile full-path="{escape(path, quote=True)}" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>').encode()


def _package(manifest: str, *, metadata: str = '', version: str = '3.0') -> bytes:
    return (f'<package xmlns="{_OPF}" xmlns:dc="{_DC}" version="{version}">'
            f'<metadata>{metadata}</metadata><manifest>{manifest}</manifest></package>').encode()


def _epub(tmp_path: Path, *, href: str = '../images/Front%20Cover.jpg#front',
          mode: Literal['epub2', 'epub3'] = 'epub3', metadata: str = '',
          extra_items: str = '', extra_entries: list[tuple[str, bytes]] | None = None,
          comment: bytes = b'') -> Path:
    attributes = 'properties="nav cover-image scripted"' if mode == 'epub3' else ''
    if mode == 'epub2':
        metadata += '<meta name="cover" content="front"/>'
    item = (f'<item id="front" href="{escape(href, quote=True)}" '
            f'media-type="image/jpeg" {attributes}/>')
    return _archive(tmp_path, [
        ('mimetype', b'application/epub+zip'),
        ('META-INF/container.xml', _container()),
        ('OPS/package/content.opf', _package(item + extra_items, metadata=metadata,
                                            version='2.0' if mode == 'epub2' else '3.0')),
        ('elsewhere/Front Cover.jpg', _jpeg('blue')),
        ('OPS/images/Front Cover.jpg', _jpeg('red')),
        *(extra_entries or []),
    ], comment=comment)


@pytest.mark.parametrize('mode', ['epub2', 'epub3'])
def test_epub_reference_uses_exact_nested_path_and_not_same_basename_decoy(
    tmp_path: Path, mode: Literal['epub2', 'epub3'],
) -> None:
    book = _epub(tmp_path, mode=mode)
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert (reference.status, reference.member, reference.authoritative) == (
            'found', 'OPS/images/Front Cover.jpg', True,
        )
        assert reference.member is not None
        with Image.open(io.BytesIO(reader.read_member(reference.member))) as selected:
            red, green, blue = selected.getpixel((0, 0))
            assert red > 240 and green < 20 and blue < 20
        with Image.open(io.BytesIO(reader.read_member('elsewhere/Front Cover.jpg'))) as decoy:
            red, green, blue = decoy.getpixel((0, 0))
            assert blue > 240 and red < 20 and green < 20


def test_epub3_cover_image_takes_precedence_over_epub2_legacy_reference(tmp_path: Path) -> None:
    book = _epub(tmp_path, metadata='<meta name="cover" content="old"/>',
                 extra_items='<item id="old" href="../../elsewhere/Front%20Cover.jpg" media-type="image/jpeg"/>')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'found' and reference.member == 'OPS/images/Front Cover.jpg'


def test_manifest_href_is_percent_decoded_once_and_fragment_removed_before_decoding(tmp_path: Path) -> None:
    book = _epub(tmp_path, href='../images/Front%2520Cover.jpg#ignored',
                 extra_entries=[('OPS/images/Front%20Cover.jpg', _jpeg('green'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'found' and reference.member == 'OPS/images/Front%20Cover.jpg'
        assert reference.member is not None
        with Image.open(io.BytesIO(reader.read_member(reference.member))) as selected:
            red, green, blue = selected.getpixel((0, 0))
            assert green > red + 80 and green > blue + 80
    book = _epub(tmp_path, href='../images/front%23section.jpg#ignored',
                 extra_entries=[('OPS/images/front#section.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'found' and reference.member == 'OPS/images/front#section.jpg'


def test_relative_dot_segments_can_reach_a_resource_within_archive_root(tmp_path: Path) -> None:
    book = _epub(tmp_path, href='./../images/./discard/../Front%20Cover.jpg')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'found' and reference.member == 'OPS/images/Front Cover.jpg'


@pytest.mark.parametrize('href', [
    '../../../outside.jpg', '../%2e%2e/../../outside.jpg', 'https://example.invalid/cover.jpg',
    '//example.invalid/cover.jpg', '/OPS/images/Front%20Cover.jpg', 'C:/cover.jpg',
    r'..\images\Front%20Cover.jpg', '%2f%2fexample.invalid/cover.jpg',
    '%68%74%74%70%73%3a//example.invalid/cover.jpg', '../images/cover%ZZ.jpg',
    '../images/cover%00.jpg', '../images/cover%FF.jpg', '../images/Front%20Cover.jpg?download=1',
])
def test_unsafe_authoritative_reference_never_becomes_a_basename_fallback(tmp_path: Path, href: str) -> None:
    book = _epub(tmp_path, href=href)
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'invalid' and reference.authoritative and reference.member is None
        assert reader.read_member('OPS/images/Front Cover.jpg').startswith(b'\xff\xd8')


def test_missing_authoritative_resource_is_invalid_even_when_decoy_exists(tmp_path: Path) -> None:
    book = _epub(tmp_path, href='../missing/Front%20Cover.jpg')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert (reference.status, reference.reason, reference.authoritative) == (
            'invalid', 'missing_authoritative_cover', True,
        )
        assert reference.member is None


@pytest.mark.parametrize('items', [
    '<item id="second" href="../../elsewhere/Front%20Cover.jpg" properties="cover-image"/>',
    '<item id="front" href="../../elsewhere/Front%20Cover.jpg"/>',
])
def test_multiple_authoritative_or_duplicate_manifest_candidates_are_ambiguous(tmp_path: Path, items: str) -> None:
    book = _epub(tmp_path, extra_items=items)
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'ambiguous' and reference.authoritative and reference.member is None


def test_epub2_missing_manifest_id_and_multiple_cover_meta_are_explicit(tmp_path: Path) -> None:
    package = _package('<item id="image" href="cover.jpg"/>',
                       metadata='<meta name="cover" content="missing"/>', version='2.0')
    book = _archive(tmp_path, [('META-INF/container.xml', _container()),
                               ('OPS/package/content.opf', package), ('OPS/package/cover.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert (reference.status, reference.reason, reference.authoritative) == (
            'invalid', 'missing_cover_manifest_id', True,
        )
    book = _epub(tmp_path, mode='epub2', metadata='<meta name="cover" content="front"/>')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'ambiguous' and reference.authoritative


def test_cover_ids_are_matched_as_literal_values_without_xpath_interpolation(tmp_path: Path) -> None:
    identity = "front' or @id='decoy"
    package = _package(
        f'<item id="{escape(identity, quote=True)}" href="../images/Front%20Cover.jpg"/>'
        '<item id="decoy" href="../../elsewhere/Front%20Cover.jpg"/>',
        metadata=f'<meta name="cover" content="{escape(identity, quote=True)}"/>', version='2.0',
    )
    book = _archive(tmp_path, [('META-INF/container.xml', _container()),
                               ('OPS/package/content.opf', package),
                               ('OPS/images/Front Cover.jpg', _jpeg('red')),
                               ('elsewhere/Front Cover.jpg', _jpeg('blue'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'found' and reference.member == 'OPS/images/Front Cover.jpg'


def test_multiple_rootfiles_do_not_select_the_first_package(tmp_path: Path) -> None:
    container = (f'<container xmlns="{_CONTAINER}"><rootfiles><rootfile full-path="first.opf"/>'
                 '<rootfile full-path="second.opf"/></rootfiles></container>').encode()
    book = _archive(tmp_path, [('META-INF/container.xml', container),
                               ('first.opf', _package('<item properties="cover-image" href="red.jpg"/>')),
                               ('second.opf', _package('<item properties="cover-image" href="blue.jpg"/>')),
                               ('red.jpg', _jpeg('red')), ('blue.jpg', _jpeg('blue'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        cover = resolve_epub_cover(reader)
        assert cover.status == 'ambiguous' and cover.member is None
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.values == ()


@pytest.mark.parametrize('container', [
    b'<container>', b'<container xmlns="wrong"><rootfiles/></container>',
    _container('../../outside.opf'), _container('https://example.invalid/content.opf'),
    _container('OPS/package/missing.opf'),
])
def test_bad_container_data_has_no_authoritative_metadata_or_cover(tmp_path: Path, container: bytes) -> None:
    book = _archive(tmp_path, [('META-INF/container.xml', container),
                               ('OPS/package/content.opf', _package('', metadata='<dc:publisher>Decoy</dc:publisher>'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'invalid' and not reference.authoritative and reference.member is None
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.values == ()


def test_absent_container_and_authoritative_cover_are_distinct(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('OPS/package/content.opf', _package('<item id="page" href="cover.jpg"/>')),
                               ('OPS/package/cover.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'absent' and reference.reason == 'missing_epub_container'
    book = _archive(tmp_path, [('META-INF/container.xml', _container()),
                               ('OPS/package/content.opf', _package('<item id="page" href="cover.jpg"/>')),
                               ('OPS/package/cover.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'absent' and reference.reason == 'no_authoritative_cover'


def test_repeated_comicinfo_fields_and_comment_identifiers_reach_the_parser(tmp_path: Path) -> None:
    comicinfo = (b'<ComicInfo><Publisher>First Press</Publisher><Publisher>Kadokawa</Publisher>'
                 b'<Summary>Story</Summary><Year>2024</Year><ISBN>9780000000000</ISBN>'
                 b'<ISBN>9780000000001</ISBN></ComicInfo>')
    book = _archive(tmp_path, [('metadata/ComicInfo.xml', comicinfo)], name='Series v01.cbz',
                    comment=b'Description\nIDENTIFIERS: Anilist:123, ISBN:9780000000002')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid'
        assert metadata.values == (
            ('Publisher', 'First Press'), ('Publisher', 'Kadokawa'), ('Summary', 'Story'), ('Year', '2024'),
            ('ISBN', '9780000000000'), ('ISBN', '9780000000001'),
            ('identifier', 'Anilist:123'), ('identifier', 'ISBN:9780000000002'),
        )
    release = parse_release(FileObservation(book, metadata=metadata.values), ParsingPolicy())
    assert release.publisher == 'Kadokawa' and release.year == 2024
    assert release.identifiers == (('ISBN', '9780000000000'), ('ISBN', '9780000000001'),
                                   ('identifier', 'Anilist:123'), ('identifier', 'ISBN:9780000000002'))


def test_namespace_exact_epub_metadata_retains_repeated_identifiers_and_publishers(tmp_path: Path) -> None:
    text = ('<dc:description>Story</dc:description><dc:date>2023-04-05</dc:date>'
            '<dc:publisher>First Press</dc:publisher><dc:publisher>Kadokawa</dc:publisher>'
            '<dc:identifier>urn:uuid:first</dc:identifier><dc:identifier>9780000000000</dc:identifier>'
            '<fake:identifier xmlns:fake="https://wrong.invalid/dc">spoofed</fake:identifier>')
    book = _epub(tmp_path, metadata=text)
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid'
        assert metadata.values == (
            ('dc:description', 'Story'), ('dc:date', '2023-04-05'),
            ('dc:publisher', 'First Press'), ('dc:publisher', 'Kadokawa'),
            ('dc:identifier', 'urn:uuid:first'), ('dc:identifier', '9780000000000'),
        )
    release = parse_release(FileObservation(book, metadata=metadata.values), ParsingPolicy())
    assert release.year == 2023 and release.publisher == 'Kadokawa'
    assert release.identifiers == (('dc:identifier', 'urn:uuid:first'), ('dc:identifier', '9780000000000'))


@pytest.mark.parametrize('xml', [b'<ComicInfo>', b'<NotComicInfo><Publisher>Decoy</Publisher></NotComicInfo>'])
def test_malformed_or_wrong_comicinfo_never_supplies_matching_values(tmp_path: Path, xml: bytes) -> None:
    book = _archive(tmp_path, [('ComicInfo.xml', xml)], name='Series.cbz', comment=b'Identifiers:valid')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.values == ()


def test_duplicate_selected_comicinfo_documents_are_invalid(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('ComicInfo.xml', b'<ComicInfo><Publisher>First</Publisher></ComicInfo>'),
                               ('nested/ComicInfo.xml', b'<ComicInfo><Publisher>Second</Publisher></ComicInfo>')],
                    name='Series.cbz')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.reason == 'ambiguous_comicinfo_metadata'
        assert metadata.values == ()


@pytest.mark.parametrize('comment', [b'Identifiers missing', b'Identifiers:a,,b', b'Identifiers:a\x00', b'\xff'])
def test_malformed_zip_comment_is_typed_invalid_not_empty_matching_success(tmp_path: Path, comment: bytes) -> None:
    book = _archive(tmp_path, [('ComicInfo.xml', b'<ComicInfo><Publisher>Press</Publisher></ComicInfo>')],
                    name='Series.cbz', comment=comment)
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.values == ()


def test_metadata_absence_is_distinct_from_empty_and_malformed_xml(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('page.jpg', _jpeg('red'))], name='Series.cbz')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'absent' and metadata.reason == 'no_archive_metadata'
    book = _archive(tmp_path, [('ComicInfo.xml', b'<ComicInfo/>')], name='Series.cbz')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'absent' and metadata.reason == 'empty_comicinfo_metadata'


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_dtd_and_external_entities_are_rejected_without_reading_local_resource(tmp_path: Path, encoding: str) -> None:
    secret = tmp_path / 'not-metadata.txt'
    secret.write_text('private fixture sentinel')
    xml = (f'<?xml version="1.0" encoding="{encoding}"?>'
           f'<!DOCTYPE ComicInfo [<!ENTITY stolen SYSTEM "{secret.as_uri()}">]>'
           '<ComicInfo><Publisher>&stolen;</Publisher></ComicInfo>').encode(encoding)
    book = _archive(tmp_path, [('ComicInfo.xml', xml)], name='Series.cbz')
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.values == ()
    assert secret.read_text() == 'private fixture sentinel'


def test_epub_dtd_and_xml_base_cannot_change_cover_resolution(tmp_path: Path) -> None:
    package = (f'<!DOCTYPE package SYSTEM "https://example.invalid/metadata.dtd">'
               f'<package xmlns="{_OPF}"><manifest><item properties="cover-image" href="front.jpg"/>'
               '</manifest></package>').encode()
    book = _archive(tmp_path, [('META-INF/container.xml', _container()),
                               ('OPS/package/content.opf', package), ('OPS/package/front.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'invalid' and reference.member is None
    package = (f'<package xmlns="{_OPF}" xml:base="https://example.invalid/">'
               '<manifest><item properties="cover-image" href="front.jpg"/></manifest></package>').encode()
    book = _archive(tmp_path, [('META-INF/container.xml', _container()),
                               ('OPS/package/content.opf', package), ('OPS/package/front.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'invalid' and reference.authoritative and reference.member is None


def test_metadata_byte_budget_accepts_exact_limit_and_rejects_one_less(tmp_path: Path) -> None:
    xml = b'<ComicInfo><Publisher>Press</Publisher></ComicInfo>'
    book = _archive(tmp_path, [('ComicInfo.xml', xml)], name='Series.cbz')
    with ArchiveReader(book, replace(ArchivePolicy(), metadata_max_bytes=len(xml))) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid' and metadata.values == (('Publisher', 'Press'),)
    with ArchiveReader(book, replace(ArchivePolicy(), metadata_max_bytes=len(xml) - 1)) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.reason == 'metadata_byte_limit' and metadata.values == ()


@pytest.mark.parametrize('entries', [
    [('OPS/Text/bonus_1.xhtml', b'<html/>'), ('OPS/Text/signup.xhtml', b'<html/>')],
    [('OPS/Text/toc.xhtml', b'<html><body>J-Novel Club Bonus Color Illustrations</body></html>')],
    [('OPS/Text/copyright.xhtml', b'<html><body>Premium E-Book</body></html>')],
])
def test_characterized_premium_archive_evidence_reaches_release_parser(
    tmp_path: Path, entries: list[tuple[str, bytes]],
) -> None:
    book = _epub(tmp_path, extra_entries=entries)
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid' and ('premium', 'true') in metadata.values
    release = parse_release(FileObservation(book, metadata=metadata.values), ParsingPolicy())
    assert release.is_premium


def test_bonus_without_signup_and_unqualified_toc_do_not_invent_premium(tmp_path: Path) -> None:
    book = _epub(tmp_path, metadata='<dc:title>Series</dc:title>', extra_entries=[
        ('OPS/Text/bonus.xhtml', b'<html/>'),
        ('OPS/Text/toc.xhtml', b'<html><body>Bonus Stories from another publisher</body></html>'),
    ])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid' and metadata.values == (('dc:title', 'Series'),)
    release = parse_release(FileObservation(book, metadata=metadata.values), ParsingPolicy())
    assert release.is_premium is False


def test_premium_evidence_survives_missing_optional_opf_metadata(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('OPS/Text/bonus.xhtml', b'<html/>'), ('OPS/Text/signup.xhtml', b'<html/>')])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid' and metadata.values == (('premium', 'true'),)


def test_inspect_worker_keeps_lexical_first_nonimage_and_all_member_evidence(tmp_path: Path) -> None:
    book = _archive(tmp_path, [
        ('ComicInfo.xml', b'<ComicInfo><Publisher>Press</Publisher></ComicInfo>'),
        ('images/', b''), ('images/page.jpg', _jpeg('white')), ('z-last.jpg', _jpeg('red')),
    ], name='Series.cbz')
    result = run_worker('inspect', {'book': str(book), 'root': str(tmp_path),
                                           'archive_policy': ArchivePolicy().as_dict()})
    assert result['status'] == 'valid' and result['container'] == 'zip'
    assert result['metadata'] == [['Publisher', 'Press']]
    assert result['archive_pages'] == [
        {'name': 'ComicInfo.xml', 'is_grayscale': None, 'is_directory': False},
        {'name': 'images', 'is_grayscale': None, 'is_directory': True},
        {'name': 'images/page.jpg', 'is_grayscale': None, 'is_directory': False},
        {'name': 'z-last.jpg', 'is_grayscale': None, 'is_directory': False},
    ]
    assert list(tmp_path.iterdir()) == [book]


def test_inspect_worker_observes_only_lexical_first_all_member_grayscale(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('01.jpg', _jpeg('white')), ('cover.jpg', _jpeg('blue'))], name='Series.cbz')
    result = run_worker('inspect', {'book': str(book), 'root': str(tmp_path),
                                           'archive_policy': ArchivePolicy().as_dict()})
    assert result['archive_pages'] == [
        {'name': '01.jpg', 'is_grayscale': True, 'is_directory': False},
        {'name': 'cover.jpg', 'is_grayscale': None, 'is_directory': False},
    ]
    assert list(tmp_path.iterdir()) == [book]


def test_header_only_worker_observes_native_magic_without_requesting_decoder(tmp_path: Path) -> None:
    book = tmp_path / 'misnamed.cbz'
    book.write_bytes(b'Rar!\x1a\x07\x01\x00')
    result = run_worker('inspect', {'book': str(book), 'root': str(tmp_path), 'header_only': True,
                                           'archive_policy': ArchivePolicy().as_dict()})
    assert result['container'] == 'rar'
    fingerprint = result['source_fingerprint']
    assert isinstance(fingerprint, dict) and fingerprint['size'] == 8
    assert book.read_bytes() == b'Rar!\x1a\x07\x01\x00'


@pytest.mark.parametrize('package', [
    b'<package>',
    b'<package xmlns="https://wrong.invalid/opf"><metadata><publisher>Decoy</publisher></metadata></package>',
])
def test_malformed_or_wrong_namespace_opf_cannot_supply_authoritative_evidence(
    tmp_path: Path, package: bytes,
) -> None:
    book = _archive(tmp_path, [('META-INF/container.xml', _container()),
                               ('OPS/package/content.opf', package),
                               ('OPS/package/front.jpg', _jpeg('red'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        reference = resolve_epub_cover(reader)
        assert reference.status == 'invalid' and reference.authoritative is False
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.values == ()


def test_comment_metadata_limit_counts_encoded_utf8_bytes(tmp_path: Path) -> None:
    comment = 'Identifiers:é'.encode()
    book = _archive(tmp_path, [], comment=comment, name='Series.cbz')
    with ArchiveReader(book, ArchivePolicy(metadata_max_bytes=len(comment))) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'valid' and metadata.values == (('identifier', 'é'),)
    with ArchiveReader(book, ArchivePolicy(metadata_max_bytes=len(comment) - 1)) as reader:
        metadata = read_book_metadata(reader)
        assert metadata.status == 'invalid' and metadata.reason == 'metadata_byte_limit'
        assert metadata.values == ()


def test_non_authoritative_legacy_opf_metadata_is_named_without_inventing_cover_reference(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('OPS/content.opf', _package('', metadata='<dc:publisher>Press</dc:publisher>'))])
    with ArchiveReader(book, ArchivePolicy()) as reader:
        metadata = read_book_metadata(reader)
        assert (metadata.status, metadata.values, metadata.reason) == (
            'valid', (('dc:publisher', 'Press'),), 'non_authoritative_package_document',
        )
        reference = resolve_epub_cover(reader)
        assert reference.status == 'absent' and reference.authoritative is False


def test_inspect_worker_hidden_first_member_remains_unknown_not_the_next_image(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('.hidden.jpg', _jpeg('red')), ('01.jpg', _jpeg('white'))], name='Series.cbz')
    result = run_worker('inspect', {'book': str(book), 'root': str(tmp_path),
                                           'archive_policy': ArchivePolicy(cover_max_candidates=1).as_dict()})
    assert result['archive_pages'] == [
        {'name': '.hidden.jpg', 'is_grayscale': None, 'is_directory': False},
        {'name': '01.jpg', 'is_grayscale': None, 'is_directory': False},
    ]


def test_inspect_cover_candidate_budget_accepts_limit_and_rejects_one_more(tmp_path: Path) -> None:
    book = _archive(tmp_path, [('01.jpg', _jpeg('white')), ('02.jpg', _jpeg('red'))], name='Series.cbz')
    request = {'book': str(book), 'root': str(tmp_path),
               'archive_policy': ArchivePolicy(cover_max_candidates=2).as_dict()}
    accepted = run_worker('inspect', request)
    assert accepted['archive_pages'] == [
        {'name': '01.jpg', 'is_grayscale': True, 'is_directory': False},
        {'name': '02.jpg', 'is_grayscale': None, 'is_directory': False},
    ]
    request['archive_policy'] = ArchivePolicy(cover_max_candidates=1).as_dict()
    with pytest.raises(WorkerError) as failure:
        run_worker('inspect', request)
    assert failure.value.reason == 'cover_candidate_limit'
    assert list(tmp_path.iterdir()) == [book]

"""Bounded XML metadata and exact EPUB resource identities, with lazy XML support."""
from __future__ import annotations

import importlib
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from ..parsing.metadata import MetadataResult, extract_identifiers
from .filesystem import FilesystemError, checked_path, fingerprint
from .workers import WorkerError
from .zip_archive import (
    ArchiveError,
    ArchivePolicy,
    ArchiveSource,
    validate_member_name,
)

CONTAINER_MEMBER = 'META-INF/container.xml'
CONTAINER_NAMESPACE = 'urn:oasis:names:tc:opendocument:xmlns:container'
OPF_NAMESPACE = 'http://www.idpf.org/2007/opf'
DC_NAMESPACE = 'http://purl.org/dc/elements/1.1/'
DCTERMS_NAMESPACE = 'http://purl.org/dc/terms/'
XML_NAMESPACE = 'http://www.w3.org/XML/1998/namespace'
RASTER_SUFFIXES = frozenset({'.jpg', '.jpeg', '.png', '.tbn', '.webp'})
_OPF_FALLBACK_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r'content.opf', r'contents.opf', r'package.opf', r'standard.opf',
    r'volume.opf', r'metadata.opf', r'978.*.opf',
))
_BONUS_PATTERN = re.compile(r'((bonus)_?([0-9]+)?\.xhtml)', re.IGNORECASE)
_TOC_PREMIUM_PATTERN = re.compile(r'(Bonus\s+((Color\s+)?Illustrations?|(Short\s+)?Stories))', re.IGNORECASE)
_COPYRIGHT_PREMIUM_PATTERN = re.compile(r'(Premium(\s)+(E?-?Book|Epub))', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EpubCoverReference:
    status: Literal['found', 'absent', 'invalid', 'ambiguous']
    member: str | None = None
    reason: str = ''
    authoritative: bool = False

    def __post_init__(self) -> None:
        if (self.status == 'found') != (self.member is not None):
            raise ValueError('only a found EPUB cover can carry a member')


class _XmlInputError(ValueError):
    pass


class _SecureTreeBuilder(ET.TreeBuilder):
    def start(self, tag: str, attrs: Mapping[str, str]) -> ET.Element:
        return super().start(tag, dict(attrs))

    def doctype(self, name: str, public_id: str | None, system_id: str | None) -> None:
        raise _XmlInputError('xml_doctype_forbidden')


def _parse_xml(data: bytes) -> ET.Element:
    try:
        etree = importlib.import_module('lxml.etree')
    except ModuleNotFoundError:
        raise ArchiveError('xml_metadata_requires_covers_extra', 'missing_dependency') from None
    parser = etree.XMLParser(
        target=_SecureTreeBuilder(), resolve_entities=False, load_dtd=False,
        dtd_validation=False, attribute_defaults=False, no_network=True,
        recover=False, huge_tree=False, collect_ids=False,
    )
    try:
        parsed: object = etree.fromstring(data, parser=parser)
    except _XmlInputError:
        raise
    except (etree.XMLSyntaxError, ET.ParseError, ValueError, UnicodeError):
        raise _XmlInputError('malformed_xml') from None
    if not isinstance(parsed, ET.Element):
        raise _XmlInputError('invalid_xml_document')
    return parsed


def _read_xml(reader: ArchiveSource, name: str) -> ET.Element:
    try:
        data = reader.read_member(name, max_bytes=reader.policy.metadata_max_bytes)
    except ArchiveError as error:
        if error.reason == 'archive_member_limit':
            raise _XmlInputError('metadata_byte_limit') from None
        raise
    return _parse_xml(data)


@dataclass(frozen=True, slots=True)
class _PackageDocument:
    status: Literal['valid', 'absent', 'invalid', 'ambiguous']
    path: str | None = None
    root: ET.Element | None = None
    reason: str = ''


def _read_package(reader: ArchiveSource) -> _PackageDocument:
    names = {member.name for member in reader.members() if not member.is_dir}
    if CONTAINER_MEMBER not in names:
        return _PackageDocument('absent', reason='missing_epub_container')
    try:
        container = _read_xml(reader, CONTAINER_MEMBER)
    except _XmlInputError as error:
        return _PackageDocument('invalid', reason=str(error))
    if container.tag != f'{{{CONTAINER_NAMESPACE}}}container':
        return _PackageDocument('invalid', reason='invalid_container_namespace')
    rootfiles = container.findall(f'{{{CONTAINER_NAMESPACE}}}rootfiles/{{{CONTAINER_NAMESPACE}}}rootfile')
    if len(rootfiles) > 1:
        return _PackageDocument('ambiguous', reason='ambiguous_package_documents')
    if not rootfiles:
        return _PackageDocument('invalid', reason='missing_package_reference')
    path = rootfiles[0].get('full-path', '')
    try:
        reference = urlsplit(path)
        if reference.scheme or reference.netloc or reference.query or reference.fragment:
            return _PackageDocument('invalid', reason='unsafe_package_reference')
        path = validate_member_name(path)
    except (ArchiveError, ValueError):
        return _PackageDocument('invalid', reason='unsafe_package_reference')
    if path not in names:
        return _PackageDocument('invalid', reason='missing_package_document')
    try:
        package = _read_xml(reader, path)
    except _XmlInputError as error:
        return _PackageDocument('invalid', reason=str(error))
    if package.tag != f'{{{OPF_NAMESPACE}}}package':
        return _PackageDocument('invalid', reason='invalid_package_namespace')
    return _PackageDocument('valid', path, package)


def _resolve_href(package_path: str, href: str) -> str:
    if not href or '\x00' in href or '\\' in href or re.search(r'%(?![0-9A-Fa-f]{2})', href):
        raise _XmlInputError('unsafe_cover_reference')
    try:
        reference = urlsplit(href)
        if reference.scheme or reference.netloc or reference.query:
            raise _XmlInputError('external_cover_reference')
        decoded = unquote(reference.path, encoding='utf-8', errors='strict')
        if not decoded or decoded.startswith('/') or '\\' in decoded or '\x00' in decoded:
            raise _XmlInputError('unsafe_cover_reference')
        decoded_reference = urlsplit(decoded)
        if decoded_reference.scheme or decoded_reference.netloc or re.match(r'^[A-Za-z]:', decoded):
            raise _XmlInputError('external_cover_reference')
    except _XmlInputError:
        raise
    except (UnicodeError, ValueError):
        raise _XmlInputError('unsafe_cover_reference') from None
    parts = package_path.split('/')[:-1]
    for part in decoded.split('/'):
        if part in {'', '.'}:
            continue
        if part == '..':
            if not parts:
                raise _XmlInputError('escaped_cover_reference')
            parts.pop()
        else:
            parts.append(part)
    try:
        return validate_member_name('/'.join(parts))
    except ArchiveError:
        raise _XmlInputError('unsafe_cover_reference') from None


def resolve_epub_cover(reader: ArchiveSource) -> EpubCoverReference:
    package = _read_package(reader)
    if package.status != 'valid':
        status: Literal['absent', 'invalid', 'ambiguous']
        if package.status == 'absent':
            status = 'absent'
        elif package.status == 'ambiguous':
            status = 'ambiguous'
        else:
            status = 'invalid'
        return EpubCoverReference(status, reason=package.reason)
    assert package.root is not None and package.path is not None
    manifests = package.root.findall(f'{{{OPF_NAMESPACE}}}manifest')
    if len(manifests) > 1:
        return EpubCoverReference('ambiguous', reason='multiple_package_manifests', authoritative=True)
    if len(manifests) != 1:
        return EpubCoverReference('invalid', reason='invalid_package_manifest')
    items = manifests[0].findall(f'{{{OPF_NAMESPACE}}}item')
    cover_images = [item for item in items if 'cover-image' in item.get('properties', '').split()]
    authoritative = bool(cover_images)
    if len(cover_images) > 1:
        return EpubCoverReference('ambiguous', reason='multiple_epub3_covers', authoritative=True)
    if cover_images:
        selected = cover_images[0]
    else:
        cover_meta = package.root.findall(f'{{{OPF_NAMESPACE}}}metadata/{{{OPF_NAMESPACE}}}meta[@name="cover"]')
        if len(cover_meta) > 1:
            return EpubCoverReference('ambiguous', reason='multiple_epub2_covers', authoritative=True)
        if not cover_meta:
            return EpubCoverReference('absent', reason='no_authoritative_cover')
        authoritative = True
        cover_id = cover_meta[0].get('content', '')
        selected_items = [item for item in items if item.get('id') == cover_id]
        if not cover_id or not selected_items:
            return EpubCoverReference('invalid', reason='missing_cover_manifest_id', authoritative=True)
        if len(selected_items) > 1:
            return EpubCoverReference('ambiguous', reason='duplicate_cover_manifest_id', authoritative=True)
        selected = selected_items[0]
    identifiers = [item.get('id') for item in items if item.get('id')]
    if len(identifiers) != len(set(identifiers)):
        return EpubCoverReference('ambiguous', reason='duplicate_manifest_id', authoritative=authoritative)
    if any(node.get(f'{{{XML_NAMESPACE}}}base') for node in package.root.iter()):
        return EpubCoverReference('invalid', reason='unsupported_xml_base', authoritative=authoritative)
    try:
        member = _resolve_href(package.path, selected.get('href', ''))
    except _XmlInputError as error:
        return EpubCoverReference('invalid', reason=str(error), authoritative=authoritative)
    if not any(item.name == member and not item.is_dir for item in reader.members()):
        return EpubCoverReference('invalid', reason='missing_authoritative_cover', authoritative=authoritative)
    return EpubCoverReference('found', member, authoritative=authoritative)


def _comicinfo_metadata(reader: ArchiveSource, name: str) -> MetadataResult:
    try:
        root = _read_xml(reader, name)
    except _XmlInputError as error:
        return MetadataResult('invalid', reason=str(error))
    if root.tag != 'ComicInfo':
        return MetadataResult('invalid', reason='invalid_comicinfo_root')
    values = tuple((child.tag, ''.join(child.itertext()).strip())
                   for child in root if isinstance(child.tag, str) and ''.join(child.itertext()).strip())
    if not values:
        return MetadataResult('absent', reason='empty_comicinfo_metadata')
    return MetadataResult('valid', values)


def _package_metadata(root: ET.Element) -> MetadataResult:
    if root.tag != f'{{{OPF_NAMESPACE}}}package':
        return MetadataResult('invalid', reason='invalid_package_namespace')
    metadata = root.findall(f'{{{OPF_NAMESPACE}}}metadata')
    if len(metadata) > 1:
        return MetadataResult('invalid', reason='ambiguous_package_metadata')
    if not metadata:
        return MetadataResult('absent', reason='missing_package_metadata')
    values: list[tuple[str, str]] = []
    for child in metadata[0]:
        text = ''.join(child.itertext()).strip()
        if not text or not isinstance(child.tag, str):
            continue
        if child.tag.startswith(f'{{{DC_NAMESPACE}}}'):
            values.append(('dc:' + child.tag[len(DC_NAMESPACE) + 2:], text))
        elif child.tag.startswith(f'{{{DCTERMS_NAMESPACE}}}'):
            values.append(('dcterms:' + child.tag[len(DCTERMS_NAMESPACE) + 2:], text))
        elif child.tag == f'{{{OPF_NAMESPACE}}}meta' and child.get('property'):
            values.append((child.get('property', ''), text))
    return MetadataResult('valid', tuple(values)) if values else MetadataResult('absent', reason='empty_package_metadata')


def _premium_metadata(reader: ArchiveSource) -> MetadataResult:
    names = tuple(member.name for member in reader.members() if not member.is_dir)
    lowered = tuple(name.lower() for name in names)
    if any('/signup' in name for name in lowered) and any(_BONUS_PATTERN.search(name) for name in lowered):
        return MetadataResult('valid', (('premium', 'true'),))
    for name in names:
        basename = PurePosixPath(name).name
        if basename not in {'toc.xhtml', 'copyright.xhtml'}:
            continue
        try:
            contents = reader.read_member(name, max_bytes=reader.policy.metadata_max_bytes).decode('utf-8')
        except UnicodeError:
            return MetadataResult('invalid', reason='invalid_premium_text_encoding')
        except ArchiveError as error:
            if error.reason == 'archive_member_limit':
                return MetadataResult('invalid', reason='metadata_byte_limit')
            raise
        if basename == 'toc.xhtml':
            found = 'j-novel' in contents.lower() and bool(_TOC_PREMIUM_PATTERN.search(contents))
        else:
            found = 'premium' in contents.lower() and bool(_COPYRIGHT_PREMIUM_PATTERN.search(contents))
        if found:
            return MetadataResult('valid', (('premium', 'true'),))
    return MetadataResult('absent', reason='no_premium_evidence')


def _xml_metadata(reader: ArchiveSource) -> MetadataResult:
    names = tuple(member.name for member in reader.members() if not member.is_dir)
    if CONTAINER_MEMBER in names:
        package = _read_package(reader)
        if package.status in {'invalid', 'ambiguous'}:
            return MetadataResult('invalid', reason=package.reason)
        assert package.root is not None
        result = _package_metadata(package.root)
    else:
        comicinfo = [name for name in names if PurePosixPath(name).name.lower() == 'comicinfo.xml']
        if len(comicinfo) > 1:
            return MetadataResult('invalid', reason='ambiguous_comicinfo_metadata')
        if comicinfo:
            return _comicinfo_metadata(reader, comicinfo[0])
        opfs = [name for name in names if name.lower().endswith('.opf') and any(
            pattern.search(PurePosixPath(name).name.lower()) for pattern in _OPF_FALLBACK_PATTERNS)]
        if len(opfs) > 1:
            return MetadataResult('invalid', reason='ambiguous_package_documents')
        if not opfs:
            result = MetadataResult('absent', reason='no_archive_metadata')
            if reader.book.suffix.lower() != '.epub':
                return result
        else:
            try:
                result = _package_metadata(_read_xml(reader, opfs[0]))
            except _XmlInputError as error:
                return MetadataResult('invalid', reason=str(error))
            if result.status == 'valid':
                result = MetadataResult('valid', result.values, 'non_authoritative_package_document')
    if result.status == 'invalid':
        return result
    premium = _premium_metadata(reader)
    if premium.status == 'invalid':
        return premium
    if premium.status == 'valid':
        return MetadataResult('valid', result.values + premium.values, result.reason)
    return result


def read_book_metadata(reader: ArchiveSource) -> MetadataResult:
    """Preserve repeated fields and distinguish malformed data from absent evidence."""
    xml = _xml_metadata(reader)
    if xml.status == 'invalid':
        return xml
    if len(reader.comment) > reader.policy.metadata_max_bytes:
        return MetadataResult('invalid', reason='metadata_byte_limit')
    try:
        comment = reader.comment.decode('utf-8')
    except UnicodeError:
        return MetadataResult('invalid', reason='invalid_archive_comment_encoding')
    identifiers = extract_identifiers(comment, max_bytes=reader.policy.metadata_max_bytes)
    if identifiers.status == 'invalid':
        return MetadataResult('invalid', reason=identifiers.reason)
    values = xml.values + identifiers.identifiers
    return MetadataResult('valid', values, xml.reason) if values else MetadataResult('absent', reason=xml.reason)


def _assert_source_identity(book: Path, before: os.stat_result) -> None:
    try:
        after = book.stat()
    except OSError:
        raise WorkerError('source_changed', 'stale_plan') from None
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise WorkerError('source_changed', 'stale_plan')


def inspect_book_job(request: dict[str, object]) -> dict[str, object]:
    if set(request) - {'book', 'root', 'archive_policy', 'header_only', 'readiness_only'} or not {'book', 'root', 'archive_policy'} <= set(request):
        raise WorkerError('invalid_inspect_request')
    header_only = request.get('header_only', False)
    readiness_only = request.get('readiness_only', False)
    if type(header_only) is not bool or type(readiness_only) is not bool or header_only and readiness_only:
        raise WorkerError('invalid_inspect_request')
    book_value, root_value = request['book'], request['root']
    if not isinstance(book_value, str) or not book_value or not isinstance(root_value, str) or not root_value:
        raise WorkerError('invalid_inspect_request')
    policy = ArchivePolicy.from_dict(request['archive_policy'])
    book, root = Path(book_value), Path(root_value)
    try:
        book = checked_path(book, root)
    except FilesystemError as error:
        raise WorkerError(error.reason) from None
    from ..application.convert import detect_container, open_archive_reader
    before = book.stat()
    container = detect_container(book, policy=policy)
    if header_only:
        source = fingerprint(book, root)
        _assert_source_identity(book, before)
        return {'status': 'valid', 'container': container, 'source_fingerprint': source.as_dict()}
    with open_archive_reader(book, root=root, policy=policy) as reader:
        if readiness_only:
            sources = getattr(reader, "sources", None)
            if sources is None:
                sources = (fingerprint(book, root),)
            reader.assert_unchanged()
            _assert_source_identity(book, before)
            return {'status': 'valid', 'container': container,
                    'source_fingerprints': [source.as_dict() for source in sources]}
        metadata = read_book_metadata(reader)
        members = sorted(reader.members(), key=lambda member: member.raw_name)
        pages = [member for member in members if not member.is_dir
                 and PurePosixPath(member.name).suffix.lower() in RASTER_SUFFIXES
                 and not PurePosixPath(member.name).name.startswith(('.', '__'))]
        if len(pages) > policy.cover_max_candidates:
            raise WorkerError('cover_candidate_limit')
        observations: list[dict[str, object]] = [
            {'name': member.raw_name, 'is_grayscale': None, 'is_directory': member.is_dir}
            for member in members
        ]
        if (container != 'epub' and members and not members[0].is_dir
                and PurePosixPath(members[0].raw_name).suffix in RASTER_SUFFIXES):
            from .images import observe_image
            data = reader.read_member(members[0].name, max_bytes=policy.cover_member_max_bytes)
            observation = observe_image(data, max_pixels=policy.image_max_pixels)
            observations[0]['is_grayscale'] = observation.is_grayscale
        reader.assert_unchanged()
        sources = getattr(reader, "sources", None)
        if sources is None:
            sources = (fingerprint(book, root),)
        _assert_source_identity(book, before)
        diagnostics = [metadata.reason] if metadata.reason else []
        return {'status': 'valid', 'container': container, 'metadata_status': metadata.status,
                'metadata': [list(pair) for pair in metadata.values], 'metadata_reason': metadata.reason,
                'archive_pages': observations, 'diagnostics': diagnostics,
                'source_fingerprints': [source.as_dict() for source in sources]}

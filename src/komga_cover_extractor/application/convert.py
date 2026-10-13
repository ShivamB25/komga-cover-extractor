"""Container observations and bounded CBZ stages, never source cleanup or publication."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import stat
import uuid
import zipfile
from collections.abc import Buffer, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import BinaryIO, Literal, cast

from ..domain.operations import FileFingerprint
from ..infrastructure.filesystem import FilesystemError, checked_path, fingerprint
from ..infrastructure.rar_archive import RarReader
from ..infrastructure.seven_zip import (
    SevenZipReader,
    make_writer_factory,
    require_archive_worker,
)
from ..infrastructure.workers import WorkerError, run_worker
from ..infrastructure.zip_archive import (
    ArchiveError,
    ArchiveMember,
    ArchivePolicy,
    ArchiveReader,
    ArchiveSource,
    validate_member_name,
)

Container = Literal['zip', 'epub', 'rar', '7z']
ConversionStatus = Literal[
    'valid', 'invalid_input', 'unsupported', 'missing_dependency', 'stale_plan',
    'permission_denied', 'failed', 'cancelled',
]
ZIP_SIGNATURES = (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')
RAR_SIGNATURES = (b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')
SEVEN_ZIP_SIGNATURE = b'7z\xbc\xaf\x27\x1c'
TARGET_SUFFIXES: dict[Container, str] = {'zip': '.cbz', 'epub': '.epub', 'rar': '.cbr', '7z': '.7z'}
ZIP_COMMENT_MAX_BYTES = 65535
ZIP_NAME_MAX_BYTES = 65535


@dataclass(frozen=True, slots=True)
class HeaderObservation:
    container: Container
    target_suffix: str
    source: FileFingerprint


@dataclass(frozen=True, slots=True)
class ConversionResult:
    status: ConversionStatus
    reason: str
    artifact: FileFingerprint | None = None
    sources: tuple[FileFingerprint, ...] = ()
    manifest: tuple[tuple[str, str], ...] = ()
    comment: bytes = b''
    container: Container | None = None

    @property
    def artifact_path(self) -> Path | None:
        return self.artifact.path if self.artifact is not None else None

    @property
    def source_parts(self) -> tuple[Path, ...]:
        return tuple(source.path for source in self.sources)


def detect_container(book: Path, *, policy: ArchivePolicy | None = None) -> Container:
    """Detect bytes, then distinguish ZIP-based EPUB structure without trusting the suffix."""
    policy = policy or ArchivePolicy()
    path = checked_path(book, book.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveError('archive_not_regular_file')
        signature = stream.read(8)
    if signature.startswith(RAR_SIGNATURES):
        return 'rar'
    if signature.startswith(SEVEN_ZIP_SIGNATURE):
        return '7z'
    if not signature.startswith(ZIP_SIGNATURES):
        raise ArchiveError('unsupported_archive_container', 'unsupported')
    require_archive_worker()
    with ArchiveReader(path, policy) as reader:
        names = {member.name for member in reader.members() if not member.is_dir}
        if 'mimetype' in names:
            value = reader.read_member('mimetype', max_bytes=policy.metadata_max_bytes)
            if value == b'application/epub+zip':
                return 'epub'
        if 'META-INF/container.xml' in names:
            # Even malformed EPUB metadata must not authorize correction to a comic suffix.
            reader.read_member('META-INF/container.xml', max_bytes=policy.metadata_max_bytes)
            return 'epub'
        if path.suffix.lower() == '.epub':
            raise ArchiveError('epub_structure_missing')
        return 'zip'


def observe_header(book: Path, *, root: Path, policy: ArchivePolicy,
                   cancel: Event | None = None) -> HeaderObservation:
    source = fingerprint(book, root)
    result = run_worker('inspect', {
        'book': str(source.path), 'root': str(source.root), 'archive_policy': policy.as_dict(),
        'header_only': True,
    }, timeout=policy.conversion_timeout_seconds, cancel=cancel)
    if result.get('source_fingerprint') != source.as_dict():
        raise ArchiveError('source_changed', 'stale_plan')
    container = _container(result.get('container'))
    if fingerprint(source.path, source.root) != source:
        raise ArchiveError('source_changed', 'stale_plan')
    return HeaderObservation(container, TARGET_SUFFIXES[container], source)


@contextmanager
def open_archive_reader(book: Path, *, root: Path, policy: ArchivePolicy,
                        scratch_dir: Path | None = None) -> Iterator[ArchiveSource]:
    book = checked_path(book, root)
    container = detect_container(book, policy=policy)
    if container in {'zip', 'epub'}:
        with ArchiveReader(book, policy) as reader:
            yield reader
    elif container == '7z':
        with SevenZipReader(book, policy, root=root) as sevenzip:
            yield sevenzip
    else:
        with RarReader(book, policy, root=root, scratch_dir=scratch_dir) as rar:
            yield rar


class _LimitedOutput(io.BufferedIOBase):
    def __init__(self, stream: BinaryIO, limit: int):
        self.stream = stream
        self.limit = limit

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.stream.seek(offset, whence)

    def write(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast("B")
        if self.tell() + len(view) > self.limit:
            raise ArchiveError('conversion_scratch_limit')
        written = self.stream.write(view)
        if written != len(view):
            raise ArchiveError('conversion_short_write', 'failed')
        return written

    def flush(self) -> None:
        if not self.stream.closed:
            self.stream.flush()

    def close(self) -> None:
        try:
            super().close()
        finally:
            self.stream.close()


def _zip_info(member: ArchiveMember, *, comment: bytes = b'') -> zipfile.ZipInfo:
    target_name = member.name + '/' if member.is_dir else member.name
    if len(target_name.encode('utf-8')) > ZIP_NAME_MAX_BYTES:
        raise ArchiveError('unsupported_zip_member_name', 'unsupported')
    info = zipfile.ZipInfo(target_name)
    info.create_system = 3
    info.external_attr = (stat.S_IFDIR | 0o755 if member.is_dir else stat.S_IFREG | 0o644) << 16
    if member.is_dir:
        info.external_attr |= 0x10
    info.file_size = member.size
    info.compress_type = zipfile.ZIP_STORED
    if len(comment) > ZIP_COMMENT_MAX_BYTES:
        raise ArchiveError('archive_comment_limit')
    info.comment = comment
    return info


def _validate_cbz(output: Path, manifest: tuple[tuple[str, str], ...], *,
                  policy: ArchivePolicy, comment: bytes) -> None:
    actual: list[tuple[str, str]] = []
    validation_policy = replace(policy, cover_read_max_bytes=policy.archive_expansion_max_bytes)
    with ArchiveReader(output, validation_policy) as reader:
        if reader.comment != comment:
            raise ArchiveError('conversion_comment_mismatch', 'failed')
        for member in reader.members():
            digest = hashlib.sha256()
            if not member.is_dir:
                with reader.open_member(member.name) as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
            actual.append((member.name, digest.hexdigest()))
        reader.assert_unchanged()
    if tuple(sorted(actual)) != manifest:
        raise ArchiveError('conversion_manifest_mismatch', 'failed')


def conversion_job(request: dict[str, object]) -> dict[str, object]:
    """Known worker entry point. Its only writes are exclusive files in owned scratch."""
    require_archive_worker()
    if set(request) != {'book', 'root', 'output', 'scratch_dir', 'archive_policy'}:
        raise ArchiveError('invalid_conversion_request')
    values: dict[str, Path] = {}
    for key in ('book', 'root', 'output', 'scratch_dir'):
        raw = request[key]
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            raise ArchiveError('invalid_conversion_request')
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts:
            raise ArchiveError('invalid_conversion_request')
        values[key] = path
    policy = ArchivePolicy.from_dict(request['archive_policy'])
    book, root, output, scratch = (values[key] for key in ('book', 'root', 'output', 'scratch_dir'))
    try:
        book = checked_path(book, root)
        scratch = checked_path(scratch, scratch)
        scratch_info = scratch.lstat()
        if not stat.S_ISDIR(scratch_info.st_mode) or scratch_info.st_uid != os.getuid():
            raise ArchiveError('conversion_scratch_not_owned', 'permission_denied')
        if output.parent != scratch or output.suffix.lower() != '.cbz' or output == book:
            raise ArchiveError('conversion_output_outside_scratch')
        checked_path(output, scratch, allow_missing=True)
        container = detect_container(book, policy=policy)
        if container not in {'rar', '7z'}:
            raise ArchiveError('container_does_not_require_conversion', 'unsupported')
        source = fingerprint(book, root)
        read_policy = replace(policy, cover_read_max_bytes=policy.archive_expansion_max_bytes)
        native = (SevenZipReader(book, read_policy, root=root) if container == '7z'
                  else RarReader(book, read_policy, root=root, scratch_dir=scratch))
        created = False
        descriptor: int | None = None
        try:
            with native as reader:
                members = reader.members()
                comment = reader.comment
                if len(comment) > min(ZIP_COMMENT_MAX_BYTES, policy.metadata_max_bytes):
                    raise ArchiveError('archive_comment_limit')
                descriptor = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                     | os.O_CLOEXEC, 0o600)
                created = True
                raw_output = cast(BinaryIO, os.fdopen(descriptor, 'w+b'))
                descriptor = None
                manifest_map: dict[str, str] = {}
                with _LimitedOutput(raw_output, policy.conversion_scratch_max_bytes) as limited:
                    with zipfile.ZipFile(cast(BinaryIO, limited), 'w', allowZip64=True) as archive:
                        archive.comment = comment
                        for member in members:
                            if member.is_dir:
                                archive.writestr(_zip_info(member), b'')
                                manifest_map[member.name] = hashlib.sha256(b'').hexdigest()
                        if isinstance(reader, SevenZipReader):
                            with ExitStack() as outputs:
                                def open_output(member: ArchiveMember) -> BinaryIO:
                                    stream = archive.open(_zip_info(member), 'w',
                                                          force_zip64=member.size >= zipfile.ZIP64_LIMIT)
                                    outputs.callback(stream.close)
                                    return cast(BinaryIO, stream)

                                factory = make_writer_factory(
                                    members, policy=policy, open_output=open_output,
                                    completed=lambda member, digest: manifest_map.__setitem__(member.name, digest),
                                    total_limit=policy.archive_expansion_max_bytes,
                                )
                                reader.extract_to(factory, tuple(member for member in members if not member.is_dir))
                        else:
                            for member in members:
                                if member.is_dir:
                                    continue
                                file_comment = reader.member_comment(member.name)
                                digest = hashlib.sha256()
                                with reader.open_member(member.name) as incoming:
                                    with archive.open(_zip_info(member, comment=file_comment), 'w',
                                                      force_zip64=member.size >= zipfile.ZIP64_LIMIT) as outgoing:
                                        while chunk := incoming.read(65536):
                                            if outgoing.write(chunk) != len(chunk):
                                                raise ArchiveError('conversion_short_write', 'failed')
                                            digest.update(chunk)
                                manifest_map[member.name] = digest.hexdigest()
                    limited.flush()
                    os.fsync(raw_output.fileno())
                if set(manifest_map) != {member.name for member in members}:
                    raise ArchiveError('conversion_manifest_mismatch', 'failed')
                manifest = tuple(sorted(manifest_map.items()))
                _validate_cbz(output, manifest, policy=policy, comment=comment)
                reader.assert_unchanged()
                if source != reader.sources[0]:
                    raise ArchiveError('source_changed', 'stale_plan')
                for part in reader.sources:
                    if part != fingerprint(part.path, root):
                        raise ArchiveError('source_changed', 'stale_plan')
                artifact_root = root if scratch.is_relative_to(root) else scratch
                artifact = fingerprint(output, artifact_root)
                return {
                    'status': 'valid', 'artifact_path': str(output), 'artifact': artifact.as_dict(),
                    'manifest': [[name, digest] for name, digest in manifest],
                    'comment_base64': base64.b64encode(comment).decode('ascii'),
                    'source_parts': [str(part.path) for part in reader.sources],
                    'source_fingerprints': [part.as_dict() for part in reader.sources],
                    'container': container,
                }
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
    except ArchiveError:
        raise
    except FileExistsError:
        raise ArchiveError('conversion_scratch_output_exists', 'invalid_input') from None
    except FilesystemError as error:
        raise ArchiveError(error.reason) from None
    except PermissionError:
        raise ArchiveError('conversion_permission_denied', 'permission_denied') from None
    except OSError:
        raise ArchiveError('conversion_storage_failed', 'failed') from None


def _container(value: object) -> Container:
    if not isinstance(value, str) or value not in {'zip', 'epub', 'rar', '7z'}:
        raise ArchiveError('invalid_worker_container', 'failed')
    return cast(Container, value)


def _fingerprint_value(value: object, *, root: Path) -> FileFingerprint:
    if not isinstance(value, dict) or set(value) != {
        'root', 'relative_path', 'device', 'inode', 'size', 'mtime_ns', 'sha256',
    }:
        raise ArchiveError('invalid_worker_fingerprint', 'failed')
    if value['root'] != str(root):
        raise ArchiveError('invalid_worker_fingerprint', 'failed')
    relative, digest = value['relative_path'], value['sha256']
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise ArchiveError('invalid_worker_fingerprint', 'failed')
    numbers = [value[key] for key in ('device', 'inode', 'size', 'mtime_ns')]
    if not all(type(number) is int for number in numbers):
        raise ArchiveError('invalid_worker_fingerprint', 'failed')
    try:
        integers = cast(list[int], numbers)
        result = FileFingerprint(root, relative, integers[0], integers[1], integers[2], integers[3], digest)
    except ValueError:
        raise ArchiveError('invalid_worker_fingerprint', 'failed') from None
    checked_path(result.path, root)
    if fingerprint(result.path, root) != result:
        raise ArchiveError('source_changed', 'stale_plan')
    return result


def convert_book(book: Path, *, root: Path, scratch_dir: Path, policy: ArchivePolicy,
                 cancel: Event | None = None) -> ConversionResult:
    """Return a verified generated file; only the executor may publish or retire sources."""
    sources: tuple[FileFingerprint, ...] = ()
    if cancel is not None and cancel.is_set():
        return ConversionResult('cancelled', 'shutdown_requested')
    try:
        root = Path(os.path.abspath(root))
        source = fingerprint(book, root)
        sources = (source,)
        scratch = checked_path(scratch_dir, scratch_dir)
        output = scratch / f'.kce-converted-{uuid.uuid4().hex}.cbz'
        result = run_worker('convert', {
            'book': str(source.path), 'root': str(root), 'output': str(output),
            'scratch_dir': str(scratch), 'archive_policy': policy.as_dict(),
        }, timeout=policy.conversion_timeout_seconds, cancel=cancel)
        if result.get('artifact_path') != str(output):
            raise ArchiveError('invalid_worker_artifact', 'failed')
        artifact_root = root if scratch.is_relative_to(root) else scratch
        artifact = _fingerprint_value(result.get('artifact'), root=artifact_root)
        if artifact.path != output:
            raise ArchiveError('invalid_worker_artifact', 'failed')
        source_values = result.get('source_fingerprints')
        if not isinstance(source_values, list) or not source_values:
            raise ArchiveError('invalid_worker_sources', 'failed')
        sources = tuple(_fingerprint_value(value, root=root) for value in source_values)
        if sources[0] != source or len({part.path for part in sources}) != len(sources):
            raise ArchiveError('source_changed', 'stale_plan')
        if result.get('source_parts') != [str(part.path) for part in sources]:
            raise ArchiveError('invalid_worker_sources', 'failed')
        raw_manifest = result.get('manifest')
        if not isinstance(raw_manifest, list):
            raise ArchiveError('invalid_worker_manifest', 'failed')
        manifest = []
        names: set[str] = set()
        for pair in raw_manifest:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(item, str) for item in pair):
                raise ArchiveError('invalid_worker_manifest', 'failed')
            name, digest = cast(list[str], pair)
            if validate_member_name(name) != name or name in names or len(digest) != 64 or any(
                character not in '0123456789abcdef' for character in digest
            ):
                raise ArchiveError('invalid_worker_manifest', 'failed')
            names.add(name)
            manifest.append((name, digest))
        comment_value = result.get('comment_base64')
        if not isinstance(comment_value, str):
            raise ArchiveError('invalid_worker_comment', 'failed')
        try:
            comment = base64.b64decode(comment_value, validate=True)
        except ValueError:
            raise ArchiveError('invalid_worker_comment', 'failed') from None
        if len(comment) > min(ZIP_COMMENT_MAX_BYTES, policy.metadata_max_bytes):
            raise ArchiveError('invalid_worker_comment', 'failed')
        return ConversionResult('valid', 'conversion_stage_verified', artifact, sources,
                                tuple(sorted(manifest)), comment, _container(result.get('container')))
    except WorkerError as error:
        status = error.status
        if error.reason in {'source_changed', 'multipart_source_changed'}:
            status = 'stale_plan'
        if status not in {'invalid_input', 'unsupported', 'missing_dependency', 'stale_plan',
                          'permission_denied', 'failed', 'cancelled'}:
            status = 'failed'
        return ConversionResult(cast(ConversionStatus, status), error.reason, sources=sources)
    except FilesystemError as error:
        return ConversionResult('invalid_input', error.reason, sources=sources)
    except PermissionError:
        return ConversionResult('permission_denied', 'conversion_permission_denied', sources=sources)
    except OSError:
        return ConversionResult('failed', 'conversion_storage_failed', sources=sources)

"""Scoped ZIP observations. Untrusted callers run this reader in an archive worker."""
from __future__ import annotations

import io
import math
import os
import re
import stat
import struct
import zipfile
import zlib
from collections.abc import Buffer, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Protocol, Self, cast

from .filesystem import FilesystemError, checked_path
from .workers import WorkerError

READ_CHUNK_BYTES = 64 * 1024
_SUPPORTED_COMPRESSION = frozenset({
    zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA, zipfile.ZIP_ZSTANDARD,
})


class ArchiveError(WorkerError):
    """A static, secret-free failure at the archive input boundary."""


@dataclass(frozen=True, slots=True)
class ArchivePolicy:
    archive_max_entries: int = 100_000
    cover_max_candidates: int = 10_000
    metadata_max_bytes: int = 4_194_304
    cover_member_max_bytes: int = 134_217_728
    conversion_member_max_bytes: int = 536_870_912
    cover_read_max_bytes: int = 1_073_741_824
    archive_expansion_max_bytes: int = 8_589_934_592
    conversion_scratch_max_bytes: int = 8_589_934_592
    archive_max_ratio: float = 1_000.0
    image_max_pixels: int = 100_000_000
    conversion_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == 'archive_max_ratio':
                if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                    raise ArchiveError('invalid_archive_policy')
                object.__setattr__(self, field.name, float(value))
            elif type(value) is not int or value <= 0:
                raise ArchiveError('invalid_archive_policy')

    def as_dict(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, value: object) -> ArchivePolicy:
        if not isinstance(value, Mapping):
            raise ArchiveError('invalid_archive_policy')
        expected = {field.name for field in fields(cls)}
        if set(value) != expected:
            raise ArchiveError('invalid_archive_policy')
        integers: dict[str, int] = {}
        for name in expected - {'archive_max_ratio'}:
            number = value[name]
            if type(number) is not int or number <= 0:
                raise ArchiveError('invalid_archive_policy')
            integers[name] = number
        ratio = value['archive_max_ratio']
        if type(ratio) not in {int, float} or not math.isfinite(ratio) or ratio <= 0:
            raise ArchiveError('invalid_archive_policy')
        return cls(archive_max_ratio=float(ratio), **integers)


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    name: str
    size: int
    compressed_size: int
    is_dir: bool
    raw_name: str


class ArchiveSource(Protocol):
    @property
    def book(self) -> Path: ...
    @property
    def policy(self) -> ArchivePolicy: ...
    @property
    def comment(self) -> bytes: ...

    def members(self) -> tuple[ArchiveMember, ...]: ...
    def open_member(self, name: str) -> BinaryIO: ...
    def read_member(self, name: str, max_bytes: int | None = None) -> bytes: ...
    def assert_unchanged(self) -> None: ...
    def close(self) -> None: ...


def validate_member_name(name: str) -> str:
    """Validate raw POSIX archive names before canonicalizing harmless dot segments."""
    if not isinstance(name, str) or not name or '\x00' in name or '\\' in name:
        raise ArchiveError('unsafe_member_name')
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ArchiveError('unsafe_member_name')
    parts = name.split('/')
    if '..' in parts:
        raise ArchiveError('unsafe_member_name')
    canonical = '/'.join(part for part in parts if part not in {'', '.'})
    if not canonical:
        raise ArchiveError('unsafe_member_name')
    return canonical


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _check_entry_type(info: zipfile.ZipInfo) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveError('special_archive_member')
    if (kind == stat.S_IFDIR and not info.is_dir()) or (kind == stat.S_IFREG and info.is_dir()):
        raise ArchiveError('invalid_member_type')
    if info.external_attr & (0x400 | 0x40 | 0x08):
        raise ArchiveError('special_archive_member')
    offset = 0
    while offset < len(info.extra):
        if len(info.extra) - offset < 4:
            raise ArchiveError('invalid_member_extra')
        code, length = struct.unpack_from('<HH', info.extra, offset)
        offset += 4
        if length > len(info.extra) - offset:
            raise ArchiveError('invalid_member_extra')
        payload = info.extra[offset:offset + length]
        offset += length
        if code == 0x000D and length > 12:
            raise ArchiveError('special_archive_member')
        if code == 0x756E:
            if length < 14:
                raise ArchiveError('invalid_member_extra')
            extra_kind = stat.S_IFMT(struct.unpack_from('<H', payload, 4)[0])
            if extra_kind not in {0, stat.S_IFREG, stat.S_IFDIR} or length > 14:
                raise ArchiveError('special_archive_member')


class ArchiveReader:
    def __init__(self, book: Path, policy: ArchivePolicy):
        self.book = book
        self.policy = policy
        self.comment = b''
        self._file: BinaryIO | None = None
        self._zip: zipfile.ZipFile | None = None
        self._observed: tuple[int, int, int, int, int] | None = None
        self._members: tuple[ArchiveMember, ...] = ()
        self._entries: dict[str, zipfile.ZipInfo] = {}
        self._streams: set[_MemberStream] = set()
        self._read_bytes = 0

    def __enter__(self) -> Self:
        if self._file is not None:
            raise ArchiveError('archive_already_open')
        try:
            self.book = checked_path(self.book, self.book.parent)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            descriptor = os.open(self.book, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ArchiveError('archive_not_regular_file')
                self._file = os.fdopen(descriptor, 'rb')
            except BaseException:
                os.close(descriptor)
                raise
            self._observed = _identity(info)
            self._zip = zipfile.ZipFile(self._file, 'r')
            self._preflight()
            self.assert_unchanged()
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, (ArchiveError, KeyboardInterrupt, SystemExit, MemoryError)):
                raise
            if isinstance(error, PermissionError):
                raise ArchiveError('archive_permission_denied', 'permission_denied') from None
            if isinstance(error, FilesystemError):
                raise ArchiveError(error.reason) from None
            if isinstance(error, FileNotFoundError):
                raise ArchiveError('missing_archive') from None
            if isinstance(error, (zipfile.BadZipFile, EOFError, OSError, ValueError)):
                raise ArchiveError('invalid_zip_archive') from None
            raise

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        try:
            if exc_type is None and self._file is not None:
                self.assert_unchanged()
        finally:
            self.close()

    def _preflight(self) -> None:
        archive = self._require_archive()
        entries = archive.infolist()
        if len(entries) > self.policy.archive_max_entries:
            raise ArchiveError('archive_entry_limit')
        raw_names: set[str] = set()
        members: list[ArchiveMember] = []
        expansion = 0
        for info in entries:
            raw_name = info.orig_filename
            canonical = validate_member_name(raw_name)
            if raw_name in raw_names or canonical in self._entries:
                raise ArchiveError('duplicate_archive_member')
            raw_names.add(raw_name)
            _check_entry_type(info)
            if info.flag_bits & (0x01 | 0x40):
                raise ArchiveError('encrypted_archive_member', 'unsupported')
            if info.compress_type not in _SUPPORTED_COMPRESSION:
                raise ArchiveError('unsupported_zip_compression', 'unsupported')
            if info.file_size < 0 or info.compress_size < 0:
                raise ArchiveError('invalid_member_size')
            if info.is_dir() and info.file_size:
                raise ArchiveError('invalid_directory_member')
            if info.file_size > self.policy.conversion_member_max_bytes:
                raise ArchiveError('archive_member_limit')
            ratio = info.file_size / info.compress_size if info.compress_size else 0.0
            if (info.file_size and not info.compress_size) or ratio > self.policy.archive_max_ratio:
                raise ArchiveError('archive_ratio_limit')
            expansion += info.file_size
            if expansion > self.policy.archive_expansion_max_bytes:
                raise ArchiveError('archive_expansion_limit')
            self._entries[canonical] = info
            members.append(ArchiveMember(canonical, info.file_size, info.compress_size,
                                         info.is_dir(), raw_name))
        files = {member.name for member in members if not member.is_dir}
        for member in members:
            components = member.name.split('/')
            if any('/'.join(components[:index]) in files for index in range(1, len(components))):
                raise ArchiveError('file_directory_conflict')
        self._members = tuple(members)
        self.comment = archive.comment

    def _require_archive(self) -> zipfile.ZipFile:
        if self._zip is None:
            raise ArchiveError('archive_closed')
        return self._zip

    def members(self) -> tuple[ArchiveMember, ...]:
        self._require_archive()
        return self._members

    @property
    def read_bytes(self) -> int:
        return self._read_bytes

    def assert_unchanged(self) -> None:
        if self._file is None or self._observed is None:
            raise ArchiveError('archive_closed')
        try:
            current = self.book.lstat()
            opened = os.fstat(self._file.fileno())
        except OSError:
            raise ArchiveError('source_changed', 'stale_plan') from None
        if stat.S_ISLNK(current.st_mode) or _identity(current) != self._observed or _identity(opened) != self._observed:
            raise ArchiveError('source_changed', 'stale_plan')

    def open_member(self, name: str) -> BinaryIO:
        archive = self._require_archive()
        canonical = validate_member_name(name)
        info = self._entries.get(canonical)
        if info is None or info.is_dir():
            raise ArchiveError('missing_archive_member')
        self.assert_unchanged()
        try:
            stream = _MemberStream(self, archive.open(info, 'r'), info,
                                   self.policy.conversion_member_max_bytes)
        except (zipfile.BadZipFile, EOFError, RuntimeError, OSError):
            self.close()
            raise ArchiveError('invalid_zip_member') from None
        except NotImplementedError:
            self.close()
            raise ArchiveError('unsupported_zip_compression', 'unsupported') from None
        self._streams.add(stream)
        return cast(BinaryIO, stream)

    def read_member(self, name: str, max_bytes: int | None = None) -> bytes:
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise ValueError('member byte limit must be positive')
        canonical = validate_member_name(name)
        info = self._entries.get(canonical)
        if info is None or info.is_dir():
            raise ArchiveError('missing_archive_member')
        limit = min(self.policy.conversion_member_max_bytes,
                    max_bytes if max_bytes is not None else self.policy.conversion_member_max_bytes)
        if info.file_size > limit:
            raise ArchiveError('archive_member_limit')
        with self.open_member(canonical) as stream:
            bounded = cast(_MemberStream, stream)
            bounded.limit = limit
            return bounded.read()

    def _remaining_bytes(self) -> int:
        return min(self.policy.cover_read_max_bytes, self.policy.archive_expansion_max_bytes) - self._read_bytes

    def close(self) -> None:
        for stream in tuple(self._streams):
            stream.close()
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._file is not None:
            self._file.close()
            self._file = None


class _MemberStream(io.RawIOBase):
    def __init__(self, reader: ArchiveReader, source: IO[bytes],
                 info: zipfile.ZipInfo, limit: int):
        super().__init__()
        self.reader = reader
        self.source = source
        self.info = info
        self.limit = limit
        self.count = 0
        self.checksum = 0
        self._finished = False

    def readable(self) -> bool:
        return not self.closed

    def seekable(self) -> bool:
        return False

    def read(self, size: int | None = -1) -> bytes:
        if size is None:
            size = -1
        if self.closed:
            raise ValueError('read of closed archive member')
        if size == 0 or self._finished:
            return b''
        if size < -1:
            raise ValueError('invalid read size')
        output = bytearray()
        try:
            self.reader.assert_unchanged()
            while size == -1 or len(output) < size:
                remaining = min(self.limit - self.count, self.reader._remaining_bytes())
                requested = min(READ_CHUNK_BYTES, remaining + 1)
                if size != -1:
                    requested = min(requested, size - len(output))
                chunk = self.source.read(requested)
                self.count += len(chunk)
                self.reader._read_bytes += len(chunk)
                if len(chunk) > remaining:
                    raise ArchiveError('archive_read_limit')
                if self.count > self.info.file_size:
                    raise ArchiveError('archive_member_size')
                self.checksum = zlib.crc32(chunk, self.checksum)
                if not chunk or self.count == self.info.file_size:
                    if self.count != self.info.file_size or self.checksum != self.info.CRC:
                        raise ArchiveError('archive_member_checksum')
                    self._finished = True
                output.extend(chunk)
                if self._finished:
                    break
            self.reader.assert_unchanged()
            return bytes(output)
        except (ArchiveError, zipfile.BadZipFile, EOFError, OSError, RuntimeError, zlib.error) as error:
            self.reader.close()
            if isinstance(error, ArchiveError):
                raise
            raise ArchiveError('archive_member_checksum') from None

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast('B')
        data = self.read(len(view))
        view[:len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self.source.close()
        finally:
            self.reader._streams.discard(self)
            super().close()

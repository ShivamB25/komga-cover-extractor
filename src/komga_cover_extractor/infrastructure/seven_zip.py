"""The locked py7zr 1.1.3 adapter. Native decoding belongs to the archive worker."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
import tempfile
from collections.abc import Buffer, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, BinaryIO, cast

from .filesystem import checked_path, fingerprint
from .workers import WORKER_CPU_SECONDS, WORKER_MEMORY_BYTES
from .zip_archive import (
    ArchiveError,
    ArchiveMember,
    ArchivePolicy,
    validate_member_name,
)

if TYPE_CHECKING:
    from py7zr import SevenZipFile
    from py7zr.io import Py7zIO, WriterFactory


@dataclass(slots=True)
class ReadBudget:
    limit: int
    used: int = 0

    def consume(self, count: int) -> None:
        self.used += count
        if self.used > self.limit:
            raise ArchiveError('archive_read_limit')


def source_identity(path: Path, root: Path) -> tuple[int, int, int, int, int]:
    info = checked_path(path, root).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ArchiveError('source_changed', 'stale_plan')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def require_archive_worker() -> None:
    if sys.platform != 'linux':
        raise ArchiveError('worker_resource_limits_require_linux', 'unsupported')
    import resource
    memory = resource.getrlimit(resource.RLIMIT_AS)[1]
    cpu = resource.getrlimit(resource.RLIMIT_CPU)[1]
    if (os.getpgrp() != os.getpid() or memory < 0 or memory > WORKER_MEMORY_BYTES
            or cpu < 0 or cpu > WORKER_CPU_SECONDS):
        raise ArchiveError('native_archive_requires_supervised_worker', 'unsupported')


def check_members(members: tuple[ArchiveMember, ...], policy: ArchivePolicy,
                  *, packed_size: int) -> None:
    if len(members) > policy.archive_max_entries:
        raise ArchiveError('archive_entry_limit')
    names: set[str] = set()
    raw_names: set[str] = set()
    total = 0
    for member in members:
        if member.name in names or member.raw_name in raw_names:
            raise ArchiveError('duplicate_archive_member')
        names.add(member.name)
        raw_names.add(member.raw_name)
        if member.size < 0 or member.compressed_size < 0:
            raise ArchiveError('invalid_archive_size')
        if member.is_dir:
            if member.size:
                raise ArchiveError('invalid_directory_size')
            continue
        if member.size > policy.conversion_member_max_bytes:
            raise ArchiveError('archive_member_limit')
        total += member.size
        if total > policy.archive_expansion_max_bytes:
            raise ArchiveError('archive_expansion_limit')
        # Solid 7z only supplies the packed size on the first file of a block.
        if member.compressed_size and member.size > member.compressed_size * policy.archive_max_ratio:
            raise ArchiveError('archive_ratio_limit')
    if total > max(1, packed_size) * policy.archive_max_ratio:
        raise ArchiveError('archive_ratio_limit')
    by_name = {member.name: member for member in members}
    for member in members:
        parents = member.name.split('/')[:-1]
        parent = ''
        for part in parents:
            parent = f'{parent}/{part}' if parent else part
            if parent in by_name and not by_name[parent].is_dir:
                raise ArchiveError('archive_path_collision')


class MemberStream(io.BufferedIOBase):
    """A nonseekable stream whose reader-wide quota survives opening another member."""

    def __init__(self, stream: BinaryIO, member: ArchiveMember, budget: ReadBudget,
                 *, limit: int, finish: Callable[[], None] | None = None):
        self._stream = stream
        self._member = member
        self._budget = budget
        self._limit = min(limit, member.size)
        self._read = 0
        self._finish = finish
        self._finished = False

    def readable(self) -> bool:
        return not self.closed

    def read(self, size: int | None = -1) -> bytes:
        if self.closed:
            raise ValueError('read of closed archive member')
        if size == 0 or self._finished:
            return b''
        if size is None or size < 0:
            output = bytearray()
            while chunk := self.read(65536):
                output.extend(chunk)
            return bytes(output)
        allowance = min(self._limit - self._read, self._budget.limit - self._budget.used)
        count = min(size, max(0, allowance) + 1, 65536)
        try:
            chunk = self._stream.read(count)
        except Exception as error:
            if isinstance(error, ArchiveError):
                raise
            raise ArchiveError('corrupt_archive_member') from None
        self._budget.consume(len(chunk))
        if self._read + len(chunk) > self._limit:
            raise ArchiveError('archive_member_limit')
        self._read += len(chunk)
        if not chunk or self._read == self._member.size:
            if self._read != self._member.size:
                raise ArchiveError('archive_member_size_mismatch')
            self._complete()
        return chunk

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast('B')
        chunk = self.read(len(view))
        view[:len(chunk)] = chunk
        return len(chunk)

    def _complete(self) -> None:
        if not self._finished:
            self._finished = True
            if self._finish is not None:
                self._finish()

    def close(self) -> None:
        if not self.closed:
            try:
                self._stream.close()
            finally:
                super().close()


@dataclass(slots=True)
class ExtractionBudget:
    member_limit: int
    total_limit: int
    used: int = 0

    def consume(self, current: int, count: int, declared: int) -> None:
        if current + count > min(self.member_limit, declared):
            raise ArchiveError('archive_member_limit')
        if self.used + count > self.total_limit:
            raise ArchiveError('archive_expansion_limit')
        self.used += count


def make_writer_factory(
    members: tuple[ArchiveMember, ...], *, policy: ArchivePolicy,
    open_output: Callable[[ArchiveMember], BinaryIO],
    completed: Callable[[ArchiveMember, str], None], total_limit: int,
    extraction_budget: ExtractionBudget | None = None,
) -> WriterFactory:
    """Import the optional ABCs only when this capability is selected."""
    from py7zr.io import Py7zIO, WriterFactory

    allowed = {member.name: member for member in members if not member.is_dir}
    budget = extraction_budget or ExtractionBudget(policy.conversion_member_max_bytes, total_limit)

    class Output(Py7zIO):
        def __init__(self, member: ArchiveMember):
            self.member = member
            self.stream = open_output(member)
            self.current = 0
            self.digest = hashlib.sha256()
            self.closed = False

        def write(self, chunk: bytes | bytearray) -> int:
            if self.closed:
                raise ArchiveError('closed_archive_output')
            budget.consume(self.current, len(chunk), self.member.size)
            written = self.stream.write(chunk)
            if written != len(chunk):
                raise ArchiveError('archive_short_write', 'failed')
            self.current += written
            self.digest.update(chunk)
            return written

        def read(self, size: int | None = None) -> bytes:
            raise io.UnsupportedOperation('forward-only archive output')

        def seek(self, offset: int, whence: int = 0) -> int:
            # py7zr seeks to zero before its CRC check and again in MemIO.close.
            if offset != 0 or whence != 0:
                raise io.UnsupportedOperation('forward-only archive output')
            return 0

        def flush(self) -> None:
            if not self.closed:
                self.stream.flush()

        def size(self) -> int:
            return self.current

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                self.stream.close()
                if self.current != self.member.size:
                    raise ArchiveError('archive_member_size_mismatch')
                completed(self.member, self.digest.hexdigest())

    class Factory(WriterFactory):
        def __init__(self) -> None:
            self.products: dict[str, Output] = {}

        def create(self, filename: str) -> Py7zIO:
            name = validate_member_name(filename)
            if name not in allowed or name in self.products:
                raise ArchiveError('unexpected_archive_output')
            product = Output(allowed[name])
            self.products[name] = product
            return product

    return Factory()


class SevenZipReader:
    def __init__(self, book: Path, policy: ArchivePolicy, *, root: Path):
        self.book = book
        self.root = root
        self.policy = policy
        self.comment = b''
        self._archive: SevenZipFile | None = None
        self._source_stream: BinaryIO | None = None
        self._opened_identity: tuple[int, int, int, int, int] | None = None
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._streams: list[MemberStream] = []
        self._budget = ReadBudget(min(policy.cover_read_max_bytes, policy.archive_expansion_max_bytes))
        self._decoded = 0
        self._selected_budget = ExtractionBudget(
            policy.conversion_member_max_bytes,
            min(policy.cover_read_max_bytes, policy.archive_expansion_max_bytes, policy.conversion_scratch_max_bytes),
        )
        self._members: tuple[ArchiveMember, ...] = ()
        self._by_name: dict[str, ArchiveMember] = {}
        self.sources = (fingerprint(book, root),)

    def __enter__(self) -> SevenZipReader:
        require_archive_worker()
        try:
            from py7zr import SevenZipFile, __version__
            from py7zr.exceptions import PasswordRequired
        except ImportError:
            raise ArchiveError('install_conversion_extra_for_7z', 'missing_dependency') from None
        if __version__ != '1.1.3':
            raise ArchiveError('unsupported_py7zr_version', 'missing_dependency')
        try:
            book = checked_path(self.book, self.root)
            descriptor = os.open(book, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                opened = os.fstat(descriptor)
                source = self.sources[0]
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
                ) != (source.device, source.inode, source.size, source.mtime_ns):
                    raise ArchiveError('source_changed', 'stale_plan')
                self._opened_identity = (opened.st_dev, opened.st_ino, opened.st_size,
                                         opened.st_mtime_ns, opened.st_ctime_ns)
                self._source_stream = cast(BinaryIO, os.fdopen(descriptor, 'rb'))
            except BaseException:
                os.close(descriptor)
                raise
            # A passed descriptor disables py7zr's parallel extraction threads.
            self._archive = SevenZipFile(self._source_stream, 'r', mp=False,
                                         max_extract_size=self.policy.archive_expansion_max_bytes)
            if self._archive.needs_password():
                raise ArchiveError('encrypted_archive', 'unsupported')
            members: list[ArchiveMember] = []
            for item in self._archive.files:
                if len(members) >= self.policy.archive_max_entries:
                    raise ArchiveError('archive_entry_limit')
                if item.is_symlink or item.is_junction or item.is_socket:
                    raise ArchiveError('special_archive_member')
                properties = item.file_properties()
                attributes = properties.get('attributes')
                if isinstance(attributes, int) and attributes & 0x400:
                    raise ArchiveError('special_archive_member')
                mode = item.st_fmt
                if mode is not None and mode not in {stat.S_IFDIR, stat.S_IFREG}:
                    raise ArchiveError('special_archive_member')
                is_dir = item.is_directory or mode == stat.S_IFDIR
                if not (is_dir or item.is_file):
                    raise ArchiveError('special_archive_member')
                raw_name = item.filename
                name = validate_member_name(raw_name)
                size = item.uncompressed
                packed = item.compressed
                if not isinstance(size, int) or (packed is not None and not isinstance(packed, int)):
                    raise ArchiveError('invalid_archive_size')
                members.append(ArchiveMember(name, size, packed or 0, is_dir, raw_name))
            self._members = tuple(members)
            check_members(self._members, self.policy, packed_size=self.sources[0].size)
            self._by_name = {member.name: member for member in self._members}
            self.assert_unchanged()
            return self
        except PasswordRequired:
            self.close()
            raise ArchiveError('encrypted_archive', 'unsupported') from None
        except ArchiveError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ArchiveError('invalid_7z_archive') from None

    def members(self) -> tuple[ArchiveMember, ...]:
        if self._archive is None:
            raise ArchiveError('archive_closed')
        return self._members

    def extract_to(self, factory: WriterFactory, targets: tuple[ArchiveMember, ...]) -> None:
        assert self._archive is not None
        try:
            self._archive.reset()
            remaining = self.policy.archive_expansion_max_bytes - self._decoded
            self._archive.max_extract_size = remaining
            try:
                self._archive.extract(targets=[member.raw_name for member in targets], factory=factory)
            finally:
                self._decoded += self._archive.worker._total_extracted
            self.assert_unchanged()
        except ArchiveError:
            self.close()
            raise
        except Exception as error:
            self.close()
            from py7zr.exceptions import (
                DecompressionBombError,
                UnsupportedCompressionMethodError,
            )
            if isinstance(error, DecompressionBombError):
                raise ArchiveError('archive_expansion_limit') from None
            if isinstance(error, UnsupportedCompressionMethodError):
                raise ArchiveError('unsupported_7z_compression', 'unsupported') from None
            raise ArchiveError('corrupt_7z_archive') from None

    def open_member(self, name: str) -> BinaryIO:
        if self._archive is None:
            raise ArchiveError('archive_closed')
        member = self._by_name.get(validate_member_name(name))
        if member is None or member.is_dir:
            raise ArchiveError('missing_archive_member')
        if self._scratch is None:
            self._scratch = tempfile.TemporaryDirectory(prefix='kce-native-read-')
        output = Path(self._scratch.name) / f'{len(self._streams):08d}.member'
        completed: dict[str, str] = {}
        with ExitStack() as outputs:
            def open_output(_: ArchiveMember) -> BinaryIO:
                stream = output.open('xb')
                outputs.callback(stream.close)
                return cast(BinaryIO, stream)

            factory = make_writer_factory(
                (member,), policy=self.policy, open_output=open_output,
                completed=lambda entry, digest: completed.__setitem__(entry.name, digest),
                total_limit=self._selected_budget.total_limit, extraction_budget=self._selected_budget,
            )
            self.extract_to(factory, (member,))
        if member.name not in completed:
            raise ArchiveError('missing_archive_output')
        stream = MemberStream(cast(BinaryIO, output.open('rb')), member, self._budget,
                              limit=self.policy.conversion_member_max_bytes)
        self._streams.append(stream)
        return cast(BinaryIO, stream)

    def read_member(self, name: str, max_bytes: int | None = None) -> bytes:
        member = self._by_name.get(validate_member_name(name))
        if member is None:
            raise ArchiveError('missing_archive_member')
        if max_bytes is not None and member.size > max_bytes:
            raise ArchiveError('archive_member_limit')
        with self.open_member(name) as stream:
            return stream.read()

    def assert_unchanged(self) -> None:
        if self._source_stream is None or self._opened_identity is None:
            raise ArchiveError('archive_closed')
        try:
            opened = os.fstat(self._source_stream.fileno())
            descriptor_identity = (opened.st_dev, opened.st_ino, opened.st_size,
                                   opened.st_mtime_ns, opened.st_ctime_ns)
            unchanged = (source_identity(self.book, self.root) == self._opened_identity
                         and descriptor_identity == self._opened_identity)
        except OSError:
            unchanged = False
        if not unchanged:
            raise ArchiveError('source_changed', 'stale_plan')

    def close(self) -> None:
        for stream in self._streams:
            stream.close()
        self._streams.clear()
        if self._archive is not None:
            self._archive.close()
            self._archive = None
        if self._source_stream is not None:
            self._source_stream.close()
            self._source_stream = None
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

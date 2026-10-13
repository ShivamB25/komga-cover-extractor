"""The rarfile 4.2 adapter, pinned to the unrar backend inside an owned worker."""
from __future__ import annotations

import importlib
import os
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, BinaryIO, cast

from ..domain.operations import FileFingerprint
from .filesystem import FilesystemError, checked_path, fingerprint
from .seven_zip import (
    MemberStream,
    ReadBudget,
    check_members,
    require_archive_worker,
    source_identity,
)
from .zip_archive import (
    ArchiveError,
    ArchiveMember,
    ArchivePolicy,
    validate_member_name,
)


class RarReader:
    def __init__(self, book: Path, policy: ArchivePolicy, *, root: Path,
                 scratch_dir: Path | None = None):
        self.book = book
        self.root = root
        self.policy = policy
        self.comment = b''
        self._scratch_dir = scratch_dir
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._rar: ModuleType | None = None
        self._archive: Any = None
        self._previous: dict[str, object] = {}
        self._parser_methods: list[tuple[Any, object]] = []
        self._members: tuple[ArchiveMember, ...] = ()
        self._by_name: dict[str, ArchiveMember] = {}
        self._infos: dict[str, object] = {}
        self._streams: list[MemberStream] = []
        self._budget = ReadBudget(min(policy.cover_read_max_bytes, policy.archive_expansion_max_bytes))
        self.sources: tuple[FileFingerprint, ...] = (fingerprint(book, root),)
        self._source_identities: dict[Path, tuple[int, int, int, int, int]] = {}
        self._deadline = time.monotonic() + policy.conversion_timeout_seconds
        self._unrar: str | None = None
        self._verified_backend = False

    def _install_backend(self) -> None:
        assert self._rar is not None
        rar = self._rar
        self._previous = {key: getattr(rar, key) for key in (
            'UNRAR_TOOL', 'USE_EXTRACT_HACK', 'HACK_TMP_DIR', 'CURRENT_SETUP',
            'custom_popen', 'rar3_decompress',
        )}
        self._scratch = tempfile.TemporaryDirectory(prefix='kce-rar-metadata-', dir=self._scratch_dir)
        setattr(rar, 'HACK_TMP_DIR', self._scratch.name)
        setattr(rar, 'USE_EXTRACT_HACK', 0)
        setattr(rar, 'CURRENT_SETUP', None)
        self._unrar = shutil.which('unrar')
        if self._unrar is not None:
            setattr(rar, 'UNRAR_TOOL', self._unrar)
        owner = self
        original_decompress = rar.rar3_decompress

        class OwnedProcess(subprocess.Popen[bytes]):
            output_limit = owner.policy.metadata_max_bytes
            limit_reason = 'metadata_read_limit'

            def communicate(self, input: bytes | None = None, timeout: float | None = None
                            ) -> tuple[bytes, bytes]:
                if input is not None:
                    raise ArchiveError('unsupported_native_input', 'unsupported')
                assert self.stdout is not None
                result = bytearray()
                deadline = owner._deadline
                if timeout is not None:
                    deadline = min(deadline, time.monotonic() + timeout)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(self.stdout, selectors.EVENT_READ)
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ArchiveError('native_decode_deadline', 'failed')
                            for key, _ in selector.select(min(.1, remaining)):
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                if len(result) + len(chunk) > self.output_limit:
                                    raise ArchiveError(self.limit_reason)
                                result.extend(chunk)
                    code = self.wait(timeout=max(.01, deadline - time.monotonic()))
                    if code:
                        raise ArchiveError('native_decode_failed')
                    return bytes(result), b''
                finally:
                    if self.poll() is None:
                        self.kill()
                    self.wait()
                    self.stdout.close()

        def owned_popen(command: list[str]) -> subprocess.Popen[bytes]:
            if owner._unrar is None:
                raise ArchiveError('unrar_native_tool_required', 'missing_dependency')
            if not command or command[0] != owner._unrar:
                raise ArchiveError('unverified_rar_backend', 'unsupported')
            try:
                probe = command[1:] in (['-?'], ['-inul', '-?'])
                process = OwnedProcess(command, bufsize=0, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if probe else None,
                                       stdin=subprocess.DEVNULL)
                if probe:
                    process.output_limit = 65536
                    process.limit_reason = 'native_probe_output_limit'
                return process
            except OSError:
                raise ArchiveError('unrar_native_tool_required', 'missing_dependency') from None

        def bounded_comment(version: int, method: int, data: bytes, declen: int = 0,
                            flags: int = 0, crc: int = 0, pwd: object = None,
                            salt: object = None) -> bytes:
            if pwd or flags & rar.RAR_FILE_PASSWORD:
                raise ArchiveError('encrypted_archive', 'unsupported')
            if declen > owner.policy.metadata_max_bytes:
                raise ArchiveError('metadata_read_limit')
            if len(data) > owner.policy.conversion_member_max_bytes:
                raise ArchiveError('archive_member_limit')
            capsule_size = (len(data) + len(rar.RAR_ID) + 13 + rar.S_BLK_HDR.size
                            + rar.S_FILE_HDR.size + 4 + (len(salt) if isinstance(salt, bytes) else 0))
            if method != rar.RAR_M0 and capsule_size > owner.policy.conversion_scratch_max_bytes:
                raise ArchiveError('conversion_scratch_limit')
            if method != rar.RAR_M0:
                owner._select_unrar()
            value: object = original_decompress(version, method, data, declen, flags, crc, None, salt)
            if not isinstance(value, bytes) or len(value) > owner.policy.metadata_max_bytes:
                raise ArchiveError('metadata_read_limit')
            if declen and len(value) != declen:
                raise ArchiveError('archive_comment_size_mismatch')
            if flags & rar.RAR_LONG_BLOCK and zlib.crc32(value) != crc:
                raise ArchiveError('archive_comment_checksum')
            owner._budget.consume(len(value))
            return value

        setattr(rar, 'custom_popen', owned_popen)
        setattr(rar, 'rar3_decompress', bounded_comment)

        def bounded_parser(original: Callable[[Any, Any, Any], None]) -> Callable[[Any, Any, Any], None]:
            def process(parser: Any, descriptor: Any, item: Any) -> None:
                if item.type == rar.RAR_BLOCK_SUB and item.filename == 'CMT':
                    if item.needs_password() or item.flags & rar.RAR_FILE_PASSWORD:
                        raise ArchiveError('encrypted_archive', 'unsupported')
                    if item.file_size > owner.policy.metadata_max_bytes:
                        raise ArchiveError('metadata_read_limit')
                    if item.compress_size > owner.policy.conversion_member_max_bytes:
                        raise ArchiveError('archive_member_limit')
                    if item.file_size > owner._budget.limit - owner._budget.used:
                        raise ArchiveError('archive_read_limit')
                    # RAR5 comments are read internally, without rar3_decompress.
                    if item.extract_version == 50:
                        owner._budget.consume(item.file_size)
                original(parser, descriptor, item)
            return process

        for parser in (rar.RAR3Parser, rar.RAR5Parser):
            original = parser.process_entry
            self._parser_methods.append((parser, original))
            parser.process_entry = bounded_parser(original)

    def _select_unrar(self) -> None:
        assert self._rar is not None
        if self._verified_backend:
            return
        if self._unrar is None:
            raise ArchiveError('unrar_native_tool_required', 'missing_dependency')
        # A bounded version probe rejects libarchive's distinct unrar-free semantics.
        process = self._rar.custom_popen([self._unrar, '-?'])
        banner, _ = process.communicate()
        if not banner.lstrip().startswith(b'UNRAR ') or len(banner) > 65536:
            raise ArchiveError('rarlab_unrar_required', 'missing_dependency')
        try:
            self._rar.tool_setup(unrar=True, unar=False, bsdtar=False, sevenzip=False,
                                 sevenzip2=False, force=True)
            self._verified_backend = True
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError('unrar_native_tool_required', 'missing_dependency') from None

    def _check_info(self, item: Any) -> ArchiveMember:
        assert self._rar is not None
        rar = self._rar
        if item.needs_password():
            raise ArchiveError('encrypted_archive', 'unsupported')
        if item.is_symlink() or item.file_redir is not None:
            raise ArchiveError('special_archive_member')
        if not (item.is_file() or item.is_dir()):
            raise ArchiveError('special_archive_member')
        if item.host_os == rar.RAR_OS_UNIX:
            mode = stat.S_IFMT(item.mode)
            if mode not in {stat.S_IFREG, stat.S_IFDIR}:
                raise ArchiveError('special_archive_member')
        elif item.mode & 0x400:
            raise ArchiveError('special_archive_member')
        name_value: object = item.filename
        raw_value: object = item.orig_filename
        if not isinstance(name_value, str):
            raise ArchiveError('invalid_archive_name')
        if isinstance(raw_value, bytes):
            # rarfile normalizes the format's Windows separators. Validate before its rstrip.
            raw_name = raw_value.decode('utf-8', 'surrogateescape').replace('\\', '/')
            validate_member_name(raw_name)
        else:
            raw_name = name_value
        name = validate_member_name(name_value)
        size: object = item.file_size
        packed: object = item.compress_size
        if not isinstance(size, int) or not isinstance(packed, int):
            raise ArchiveError('invalid_archive_size')
        if size < 0 or packed < 0:
            raise ArchiveError('invalid_archive_size')
        if item.is_dir() and size:
            raise ArchiveError('invalid_directory_size')
        if size > self.policy.conversion_member_max_bytes:
            raise ArchiveError('archive_member_limit')
        return ArchiveMember(name, size, packed, bool(item.is_dir()), raw_name)

    def _preflight_parts(self) -> None:
        assert self._rar is not None
        rar = self._rar
        current = self.sources[0].path
        sources: list[FileFingerprint] = []
        names: dict[str, str] = {}
        raw_names: set[str] = set()
        split_name: str | None = None
        header_count = 0
        declared_total = 0
        while True:
            if len(sources) >= self.policy.archive_max_entries:
                raise ArchiveError('archive_entry_limit')
            try:
                current = checked_path(current, self.root)
                observation = fingerprint(current, self.root)
            except FilesystemError as error:
                if error.reason == 'missing_path':
                    raise ArchiveError('incomplete_multipart_archive') from None
                raise ArchiveError(error.reason) from None
            if observation in sources:
                raise ArchiveError('invalid_multipart_archive')
            sources.append(observation)
            needs_next = False
            new_numbering = False

            def inspect_header(item: Any) -> None:
                nonlocal needs_next, new_numbering, split_name, header_count, declared_total
                header_count += 1
                if header_count > self.policy.archive_max_entries * 4:
                    raise ArchiveError('archive_entry_limit')
                if item.type == rar.RAR_BLOCK_MAIN:
                    new_numbering = bool(item.flags & rar.RAR_MAIN_NEWNUMBERING)
                    if len(sources) == 1 and new_numbering and not item.flags & rar.RAR_MAIN_FIRSTVOLUME:
                        raise ArchiveError('multipart_first_volume_required')
                    if item.flags & rar.RAR_MAIN_PASSWORD:
                        raise ArchiveError('encrypted_archive', 'unsupported')
                elif item.type == rar.RAR_BLOCK_ENDARC:
                    needs_next = bool(item.flags & rar.RAR_ENDARC_NEXT_VOLUME)
                elif item.type == rar.RAR_BLOCK_FILE:
                    member = self._check_info(item)
                    if item.flags & rar.RAR_FILE_SPLIT_BEFORE:
                        if split_name != member.name:
                            raise ArchiveError('invalid_multipart_archive')
                    else:
                        if member.name in names or member.raw_name in raw_names:
                            raise ArchiveError('duplicate_archive_member')
                        names[member.name] = member.raw_name
                        raw_names.add(member.raw_name)
                        declared_total += member.size
                        if declared_total > self.policy.archive_expansion_max_bytes:
                            raise ArchiveError('archive_expansion_limit')
                        if len(names) > self.policy.archive_max_entries:
                            raise ArchiveError('archive_entry_limit')
                    split_name = member.name if item.flags & rar.RAR_FILE_SPLIT_AFTER else None
                    if split_name is not None:
                        needs_next = True

            with rar.RarFile(str(current), errors='strict', part_only=True,
                             crc_check=True, info_callback=inspect_header) as archive:
                if archive.needs_password():
                    raise ArchiveError('encrypted_archive', 'unsupported')
            if fingerprint(current, self.root) != observation:
                raise ArchiveError('source_changed', 'stale_plan')
            self._source_identities[current] = source_identity(current, self.root)
            if not needs_next:
                if split_name is not None:
                    raise ArchiveError('incomplete_multipart_archive')
                break
            next_value: object = (rar._next_newvol(str(current)) if new_numbering
                                  else rar._next_oldvol(str(current)))
            if not isinstance(next_value, str):
                raise ArchiveError('invalid_multipart_archive')
            current = Path(next_value)
        self.sources = tuple(sources)

    def __enter__(self) -> RarReader:
        require_archive_worker()
        try:
            self._rar = importlib.import_module('rarfile')
        except ImportError:
            raise ArchiveError('install_conversion_extra_for_rar', 'missing_dependency') from None
        if self._rar.__version__ != '4.2':
            raise ArchiveError('unsupported_rarfile_version', 'missing_dependency')
        try:
            self._install_backend()
            self._preflight_parts()
            self.assert_unchanged()
            self._archive = self._rar.RarFile(str(self.sources[0].path), errors='strict', crc_check=True)
            if self._archive.needs_password():
                raise ArchiveError('encrypted_archive', 'unsupported')
            actual_parts = tuple(Path(part) for part in self._archive.volumelist())
            if actual_parts != tuple(source.path for source in self.sources):
                raise ArchiveError('multipart_source_changed')
            members = []
            for info in self._archive.infolist():
                member = self._check_info(info)
                members.append(member)
                self._infos[member.name] = info
            self._members = tuple(members)
            check_members(self._members, self.policy, packed_size=sum(source.size for source in self.sources))
            self._by_name = {member.name: member for member in self._members}
            comment: object = self._archive.comment
            if comment is not None and not isinstance(comment, str):
                raise ArchiveError('invalid_archive_comment')
            self.comment = comment.encode('utf-8') if isinstance(comment, str) else b''
            if len(self.comment) > min(65535, self.policy.metadata_max_bytes):
                raise ArchiveError('archive_comment_limit')
            self._select_unrar()
            self.assert_unchanged()
            return self
        except ArchiveError:
            self.close()
            raise
        except Exception as error:
            self.close()
            if isinstance(error, self._rar.NeedFirstVolume):
                raise ArchiveError('multipart_first_volume_required') from None
            if isinstance(error, self._rar.PasswordRequired):
                raise ArchiveError('encrypted_archive', 'unsupported') from None
            if isinstance(error, self._rar.RarCannotExec):
                raise ArchiveError('unrar_native_tool_required', 'missing_dependency') from None
            raise ArchiveError('invalid_rar_archive') from None

    def members(self) -> tuple[ArchiveMember, ...]:
        if self._archive is None:
            raise ArchiveError('archive_closed')
        return self._members

    def open_member(self, name: str) -> BinaryIO:
        if self._archive is None:
            raise ArchiveError('archive_closed')
        member = self._by_name.get(validate_member_name(name))
        if member is None or member.is_dir:
            raise ArchiveError('missing_archive_member')
        self.assert_unchanged()
        try:
            raw = self._archive.open(self._infos[member.name], 'r')
        except Exception:
            raise ArchiveError('native_decode_failed') from None

        def finish() -> None:
            # rarfile stops at the declared length. Probe its native pipe for excess output.
            process = getattr(raw, '_proc', None)
            if process is not None and process.stdout is not None:
                if process.stdout.read(1):
                    raise ArchiveError('archive_member_size_mismatch')
            raw.close()
            if getattr(raw, '_returncode', 0):
                raise ArchiveError('native_decode_failed')
            self.assert_unchanged()

        stream = MemberStream(cast(BinaryIO, raw), member, self._budget,
                              limit=self.policy.conversion_member_max_bytes, finish=finish)
        self._streams.append(stream)
        return cast(BinaryIO, stream)

    def member_comment(self, name: str) -> bytes:
        info = self._infos.get(name)
        value: object = getattr(info, 'comment', None)
        if value is None:
            return b''
        if not isinstance(value, str):
            raise ArchiveError('invalid_archive_comment')
        encoded = value.encode('utf-8')
        if len(encoded) > min(65535, self.policy.metadata_max_bytes):
            raise ArchiveError('archive_comment_limit')
        return encoded

    def read_member(self, name: str, max_bytes: int | None = None) -> bytes:
        member = self._by_name.get(validate_member_name(name))
        if member is None:
            raise ArchiveError('missing_archive_member')
        if max_bytes is not None and member.size > max_bytes:
            raise ArchiveError('archive_member_limit')
        try:
            with self.open_member(name) as stream:
                return stream.read()
        except ArchiveError:
            self.close()
            raise

    def assert_unchanged(self) -> None:
        for source in self.sources:
            try:
                unchanged = source_identity(source.path, source.root) == self._source_identities[source.path]
            except OSError:
                unchanged = False
            if not unchanged:
                raise ArchiveError('source_changed', 'stale_plan')

    def close(self) -> None:
        try:
            for stream in self._streams:
                stream.close()
            self._streams.clear()
            if self._archive is not None:
                self._archive.close()
                self._archive = None
        finally:
            if self._rar is not None:
                for key, value in self._previous.items():
                    setattr(self._rar, key, value)
            for parser, original in self._parser_methods:
                parser.process_entry = original
            self._parser_methods.clear()
            if self._scratch is not None:
                self._scratch.cleanup()
                self._scratch = None

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

"""Binary archive format (internally called VSAR — VideoStore ARchive).

Layout, written in this order so the archive can be built with a single
streaming pass over the input files (checksums are only known once each file's
bytes have been read, so the manifest — which needs those checksums — is
written last, ZIP-central-directory style):

    [ARCHIVE HEADER]   magic, version, flags, file_count
    [FILE DATA...]     raw bytes of every file, back to back, in manifest order
    [MANIFEST]         one entry per file (metadata + checksum + offset)
    [MANIFEST_CHECKSUM] BLAKE3 over the manifest bytes
    [FOOTER]           manifest_offset, manifest_len, archive_checksum, magic

A binary format (not JSON) is used for the payload itself because this blob is
what gets compressed/encrypted/FEC-coded — JSON's syntactic overhead and
non-compactness would waste channel capacity. See docs/protocol.md.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Optional

from videostore.utils.hashing import blake3_bytes
from videostore.utils.pathsafe import sanitize_archive_path

MAGIC = b"VSAR"
VERSION = 1

_HEADER_FMT = ">4sHII"  # magic, version, flags, file_count
_HEADER_LEN = struct.calcsize(_HEADER_FMT)

_ENTRY_FIXED_FMT = ">IHQqIQ32s"  # file_id, path_len, size, mtime, mode, offset, checksum
_ENTRY_FIXED_LEN = struct.calcsize(_ENTRY_FIXED_FMT)

_FOOTER_FMT = ">QQ32s4s"  # manifest_offset, manifest_len, archive_checksum, magic
_FOOTER_LEN = struct.calcsize(_FOOTER_FMT)

NO_MTIME = -1
NO_MODE = 0xFFFFFFFF


@dataclass
class FileEntry:
    file_id: int
    path: str  # sanitized, posix-style, relative
    size: int
    checksum: bytes  # blake3 digest of file content, 32 bytes
    offset: int  # byte offset into the data section
    mtime: Optional[float] = None
    mode: Optional[int] = None

    def pack(self) -> bytes:
        path_bytes = self.path.encode("utf-8")
        mtime_i = NO_MTIME if self.mtime is None else int(self.mtime)
        mode_i = NO_MODE if self.mode is None else int(self.mode) & 0xFFFFFFFF
        fixed = struct.pack(
            _ENTRY_FIXED_FMT,
            self.file_id,
            len(path_bytes),
            self.size,
            mtime_i,
            mode_i,
            self.offset,
            self.checksum,
        )
        return fixed + path_bytes

    @classmethod
    def unpack_from(cls, buf: bytes, pos: int) -> tuple["FileEntry", int]:
        fixed = struct.unpack_from(_ENTRY_FIXED_FMT, buf, pos)
        pos += _ENTRY_FIXED_LEN
        file_id, path_len, size, mtime_i, mode_i, offset, checksum = fixed
        path_bytes = buf[pos : pos + path_len]
        pos += path_len
        path = sanitize_archive_path(path_bytes.decode("utf-8", errors="strict"))
        entry = cls(
            file_id=file_id,
            path=path,
            size=size,
            checksum=checksum,
            offset=offset,
            mtime=None if mtime_i == NO_MTIME else float(mtime_i),
            mode=None if mode_i == NO_MODE else mode_i,
        )
        return entry, pos


@dataclass
class ArchiveSummary:
    entries: list[FileEntry] = field(default_factory=list)
    total_size: int = 0
    archive_checksum: bytes = b""

    @property
    def file_count(self) -> int:
        return len(self.entries)


def write_header(fh: BinaryIO, file_count: int, flags: int = 0) -> None:
    fh.write(struct.pack(_HEADER_FMT, MAGIC, VERSION, flags, file_count))


def read_header(fh: BinaryIO) -> tuple[int, int, int]:
    data = fh.read(_HEADER_LEN)
    if len(data) != _HEADER_LEN:
        raise ValueError("archive truncated: incomplete header")
    magic, version, flags, file_count = struct.unpack(_HEADER_FMT, data)
    if magic != MAGIC:
        raise ValueError(f"bad archive magic: {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported archive version: {version}")
    return flags, file_count, _HEADER_LEN


def pack_manifest(entries: list[FileEntry]) -> bytes:
    return b"".join(e.pack() for e in entries)


def unpack_manifest(buf: bytes, count: int) -> list[FileEntry]:
    entries = []
    pos = 0
    for _ in range(count):
        entry, pos = FileEntry.unpack_from(buf, pos)
        entries.append(entry)
    return entries


def write_footer(fh: BinaryIO, manifest_offset: int, manifest_len: int, archive_checksum: bytes) -> None:
    fh.write(struct.pack(_FOOTER_FMT, manifest_offset, manifest_len, archive_checksum, MAGIC))


def read_footer(fh: BinaryIO) -> tuple[int, int, bytes]:
    fh.seek(-_FOOTER_LEN, os.SEEK_END)
    data = fh.read(_FOOTER_LEN)
    if len(data) != _FOOTER_LEN:
        raise ValueError("archive truncated: incomplete footer")
    manifest_offset, manifest_len, archive_checksum, magic = struct.unpack(_FOOTER_FMT, data)
    if magic != MAGIC:
        raise ValueError("bad archive footer magic (archive likely corrupted/truncated)")
    return manifest_offset, manifest_len, archive_checksum

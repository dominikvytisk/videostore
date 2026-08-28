from __future__ import annotations

import os
from typing import BinaryIO, Callable, Iterable, Optional

import blake3

from videostore.utils.hashing import blake3_bytes
from videostore.utils.pathsafe import to_archive_path

from .manifest import (
    ArchiveSummary,
    FileEntry,
    pack_manifest,
    read_footer,
    read_header,
    unpack_manifest,
    write_footer,
    write_header,
)

CHUNK_SIZE = 1 << 20  # 1 MiB streaming chunk; bounds memory regardless of file size

ProgressCB = Optional[Callable[[int], None]]


def _walk_inputs(inputs: list[str]) -> Iterable[tuple[str, str]]:
    """Yield (host_path, archive_path) pairs for every regular file under `inputs`."""
    for raw in inputs:
        path = os.path.abspath(raw)
        if os.path.isdir(path):
            base = os.path.dirname(path.rstrip(os.sep))
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    host_path = os.path.join(root, name)
                    yield host_path, to_archive_path(host_path, base)
        elif os.path.isfile(path):
            yield path, os.path.basename(path)
        else:
            raise FileNotFoundError(f"input not found: {raw}")


def build_archive(
    inputs: list[str],
    output_path: str,
    *,
    include_mtime: bool = True,
    include_mode: bool = True,
    progress: ProgressCB = None,
) -> ArchiveSummary:
    """Stream `inputs` (files and/or directories) into a VSAR archive at
    `output_path`. Memory use is bounded by CHUNK_SIZE regardless of input size."""
    entries: list[FileEntry] = []
    items = list(_walk_inputs(inputs))
    if not items:
        raise ValueError("no input files found")

    with open(output_path, "wb") as out:
        write_header(out, file_count=len(items))
        offset = 0
        archive_hasher = blake3.blake3()
        # Hash the header bytes too by re-reading what we just wrote (cheap, header is tiny).
        out.flush()
        with open(output_path, "rb") as reread:
            archive_hasher.update(reread.read())

        for file_id, (host_path, archive_path) in enumerate(items):
            st = os.stat(host_path)
            file_hasher = blake3.blake3()
            size = 0
            with open(host_path, "rb") as src:
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
                    file_hasher.update(chunk)
                    archive_hasher.update(chunk)
                    size += len(chunk)
                    if progress:
                        progress(len(chunk))
            entries.append(
                FileEntry(
                    file_id=file_id,
                    path=archive_path,
                    size=size,
                    checksum=file_hasher.digest(),
                    offset=offset,
                    mtime=st.st_mtime if include_mtime else None,
                    mode=st.st_mode if include_mode else None,
                )
            )
            offset += size

        manifest_offset = out.tell()
        manifest_bytes = pack_manifest(entries)
        manifest_checksum = blake3_bytes(manifest_bytes)
        out.write(manifest_bytes)
        out.write(manifest_checksum)
        archive_hasher.update(manifest_bytes)
        archive_hasher.update(manifest_checksum)
        manifest_len = len(manifest_bytes) + len(manifest_checksum)

        archive_checksum = archive_hasher.digest()
        write_footer(out, manifest_offset, manifest_len, archive_checksum)

    return ArchiveSummary(entries=entries, total_size=offset, archive_checksum=archive_checksum)


def compute_archive_checksum(archive_path: str) -> bytes:
    """Recomputes the archive_checksum the same way build_archive did: BLAKE3
    over everything EXCEPT the trailing footer (the footer contains the
    checksum itself, so it can't be part of what it hashes). Used by the
    decoder to verify recovered bytes against the checksum carried in the
    video's GlobalHeader — hashing the whole file including the footer here
    would never match what was computed at encode time."""
    with open(archive_path, "rb") as fh:
        manifest_offset, manifest_len, _stored = read_footer(fh)
        end = manifest_offset + manifest_len
        fh.seek(0)
        hasher = blake3.blake3()
        remaining = end
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
        return hasher.digest()


def list_archive(archive_path: str) -> ArchiveSummary:
    with open(archive_path, "rb") as fh:
        manifest_offset, manifest_len, archive_checksum = read_footer(fh)
        fh.seek(manifest_offset)
        blob = fh.read(manifest_len)
        manifest_bytes, stored_checksum = blob[:-32], blob[-32:]
        if blake3_bytes(manifest_bytes) != stored_checksum:
            raise ValueError("manifest checksum mismatch (archive corrupted)")
        _flags, file_count, _hdr_len = read_header(_reopen_at_start(fh))
        entries = unpack_manifest(manifest_bytes, file_count)
    total = sum(e.size for e in entries)
    return ArchiveSummary(entries=entries, total_size=total, archive_checksum=archive_checksum)


def _reopen_at_start(fh: BinaryIO) -> BinaryIO:
    fh.seek(0)
    return fh


def extract_archive(
    archive_path: str,
    output_dir: str,
    *,
    progress: ProgressCB = None,
) -> tuple[list[FileEntry], list[tuple[FileEntry, str]]]:
    """Extract every recoverable file. Returns (recovered, failed) where `failed`
    is a list of (entry, reason) for files whose bytes did not pass their
    individual checksum — this supports partial recovery from a damaged archive
    (see docs/protocol.md, "partial recovery")."""
    from videostore.utils.pathsafe import safe_extract_path

    os.makedirs(output_dir, exist_ok=True)
    recovered: list[FileEntry] = []
    failed: list[tuple[FileEntry, str]] = []

    with open(archive_path, "rb") as fh:
        file_size = os.fstat(fh.fileno()).st_size
        try:
            manifest_offset, manifest_len, archive_checksum = read_footer(fh)
            fh.seek(manifest_offset)
            blob = fh.read(manifest_len)
            manifest_bytes, stored_checksum = blob[:-32], blob[-32:]
            manifest_ok = blake3_bytes(manifest_bytes) == stored_checksum
        except ValueError:
            raise ValueError(
                "archive footer/manifest unreadable — data loss too severe for "
                "file-level recovery; only FEC could have saved this"
            )

        if not manifest_ok:
            raise ValueError("manifest checksum mismatch (archive corrupted)")

        _flags, file_count, _hdr_len = read_header(_reopen_at_start(fh))
        entries = unpack_manifest(manifest_bytes, file_count)

        for entry in entries:
            # entry.offset is relative to the start of the data section, i.e.
            # right after the fixed-size archive header — not the file start.
            abs_start = _hdr_len + entry.offset
            abs_end = abs_start + entry.size
            if abs_end > manifest_offset or abs_end > file_size:
                failed.append((entry, "declared range extends past available data"))
                continue
            fh.seek(abs_start)
            hasher = blake3.blake3()
            dest = safe_extract_path(output_dir, entry.path)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            remaining = entry.size
            with open(dest, "wb") as out:
                while remaining > 0:
                    chunk = fh.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    remaining -= len(chunk)
                    if progress:
                        progress(len(chunk))
            if remaining > 0 or hasher.digest() != entry.checksum:
                failed.append((entry, "checksum mismatch or truncated data"))
                try:
                    os.remove(dest)
                except OSError:
                    pass
                continue
            if entry.mode is not None:
                try:
                    os.chmod(dest, entry.mode & 0o777)
                except OSError:
                    pass
            if entry.mtime is not None:
                try:
                    os.utime(dest, (entry.mtime, entry.mtime))
                except OSError:
                    pass
            recovered.append(entry)

    return recovered, failed

"""Hashing helpers. BLAKE3 for content-integrity checksums (fast, 256-bit), CRC32 for
cheap structural checks on small headers where a full cryptographic hash is overkill."""
from __future__ import annotations

import zlib
from typing import BinaryIO

import blake3


def blake3_bytes(data: bytes) -> bytes:
    return blake3.blake3(data).digest()


def blake3_hex(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def blake3_stream(fh: BinaryIO, chunk_size: int = 1 << 20) -> bytes:
    """Hash a file-like object without loading it fully into memory."""
    h = blake3.blake3()
    while True:
        chunk = fh.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
    return h.digest()


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF

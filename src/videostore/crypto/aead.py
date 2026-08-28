"""Chunked authenticated encryption (a STREAM-style construction, cf. Rogaway/
Shrimpton and the `age` tool's STREAM). Encrypting the payload as one big AEAD
call would need the whole (possibly multi-GB) plaintext in RAM; chunking keeps
memory bounded to CHUNK_SIZE while the per-chunk AAD (index + is_last flag)
still detects truncation and chunk reordering, which a naive "just AES-CTR
without chunk binding" scheme would miss.
"""
from __future__ import annotations

import enum
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

CHUNK_SIZE = 1 << 20  # 1 MiB plaintext per chunk
NONCE_PREFIX_LEN = 8  # random per-file, stored in header
TAG_LEN = 16
_CHUNK_HDR_FMT = ">IB"  # ciphertext_len (incl tag), is_last flag
_CHUNK_HDR_LEN = struct.calcsize(_CHUNK_HDR_FMT)


class Algorithm(enum.IntEnum):
    NONE = 0
    AES256_GCM = 1
    CHACHA20_POLY1305 = 2


def _cipher(algorithm: Algorithm, key: bytes):
    if algorithm == Algorithm.AES256_GCM:
        return AESGCM(key)
    if algorithm == Algorithm.CHACHA20_POLY1305:
        return ChaCha20Poly1305(key)
    raise ValueError(f"unsupported AEAD algorithm: {algorithm}")


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def new_nonce_prefix() -> bytes:
    return os.urandom(NONCE_PREFIX_LEN)


def encrypt_file(
    src_path: str,
    dst_path: str,
    algorithm: Algorithm,
    key: bytes,
    nonce_prefix: bytes,
) -> int:
    if algorithm == Algorithm.NONE:
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
        return os.path.getsize(dst_path)

    aead = _cipher(algorithm, key)
    size = os.path.getsize(src_path)
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        index = 0
        pos = 0
        while True:
            chunk = src.read(CHUNK_SIZE)
            pos += len(chunk)
            is_last = 1 if pos >= size else 0
            aad = struct.pack(">IB", index, is_last)
            ct = aead.encrypt(_nonce(nonce_prefix, index), chunk, aad)
            dst.write(struct.pack(_CHUNK_HDR_FMT, len(ct), is_last))
            dst.write(ct)
            index += 1
            if is_last:
                break
    return os.path.getsize(dst_path)


class DecryptionError(ValueError):
    pass


@dataclass
class DecryptResult:
    size: int
    failed_chunks: int
    total_chunks: int


def decrypt_file(
    src_path: str,
    dst_path: str,
    algorithm: Algorithm,
    key: bytes,
    nonce_prefix: bytes,
    best_effort: bool = False,
) -> DecryptResult:
    """If best_effort is False (default): raises DecryptionError on the first
    authentication failure or truncation — the caller gets a strong
    guarantee that a successful return means every byte is exactly what was
    originally encrypted.

    If best_effort is True: a chunk that fails authentication is zero-filled
    and counted instead of aborting the whole file — this is what supports
    partial recovery (spec section 30) when FEC could not fully repair the
    payload. Every chunk that *is* returned as-is still passed its own AEAD
    tag check; corrupted regions are marked, never silently accepted.
    """
    if algorithm == Algorithm.NONE:
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
        return DecryptResult(size=os.path.getsize(dst_path), failed_chunks=0, total_chunks=0)

    aead = _cipher(algorithm, key)
    failed_chunks = 0
    total_chunks = 0
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        index = 0
        while True:
            hdr = src.read(_CHUNK_HDR_LEN)
            if not hdr:
                if best_effort:
                    break
                raise DecryptionError("truncated ciphertext: expected more chunks (missing final chunk)")
            if len(hdr) != _CHUNK_HDR_LEN:
                if best_effort:
                    break
                raise DecryptionError("truncated chunk header")
            ct_len, is_last = struct.unpack(_CHUNK_HDR_FMT, hdr)
            ct = src.read(ct_len)
            if len(ct) != ct_len:
                if best_effort:
                    dst.write(b"\x00" * max(0, ct_len - TAG_LEN))
                    failed_chunks += 1
                    total_chunks += 1
                    break
                raise DecryptionError("truncated ciphertext chunk")
            aad = struct.pack(">IB", index, is_last)
            total_chunks += 1
            try:
                pt = aead.decrypt(_nonce(nonce_prefix, index), ct, aad)
                dst.write(pt)
            except InvalidTag as exc:
                if not best_effort:
                    raise DecryptionError(
                        f"authentication failed on chunk {index} — wrong password, "
                        f"corrupted ciphertext, or FEC did not fully recover the payload"
                    ) from exc
                dst.write(b"\x00" * max(0, ct_len - TAG_LEN))
                failed_chunks += 1
            index += 1
            if is_last:
                break
    return DecryptResult(size=os.path.getsize(dst_path), failed_chunks=failed_chunks, total_chunks=total_chunks)

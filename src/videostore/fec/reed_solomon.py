"""Reed-Solomon FEC over GF(256), block-coded with configurable redundancy.

Chosen over LDPC/BCH/fountain codes for v1 because (a) it's a mature, simple,
well-tested library (`reedsolo`), (b) RS's classic strength — correcting burst
errors when combined with interleaving (fec/interleave.py) — matches exactly the
error pattern video transcoding produces (damage concentrated in blocks/frames,
not uniformly scattered bits), and (c) it supports erasure decoding, which lets
the modulation layer's confidence estimate feed back into FEC (see
docs/protocol.md "confidence-assisted erasure decoding" — an approximation of
full soft-decision decoding that doesn't require implementing belief
propagation for a rate-adaptive LDPC code).

Architecture is swappable: encode_blocks/decode_blocks is the only interface
the rest of the pipeline depends on, so LDPC/fountain codes can be dropped in
behind the same functions later without touching framing/modulation code.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

from reedsolo import RSCodec, ReedSolomonError

DEFAULT_NSIZE = 255


@dataclass(frozen=True)
class RSConfig:
    nsize: int = DEFAULT_NSIZE
    nsym: int = 32  # parity bytes per block

    @property
    def message_len(self) -> int:
        return self.nsize - self.nsym

    @property
    def redundancy(self) -> float:
        return self.nsym / self.nsize


def fec_output_size(plaintext_size: int, config: RSConfig) -> int:
    """Exact size of encode_file/encode_blocks' output for a given input size —
    used by the decoder to recompute the expected FEC-domain layout without
    having the original plaintext (see decoder/pipeline.py)."""
    n_blocks = math.ceil(plaintext_size / config.message_len) if plaintext_size else 0
    return n_blocks * config.nsize


def rs_config_for_redundancy(redundancy_fraction: float, nsize: int = DEFAULT_NSIZE) -> RSConfig:
    if not (0.0 < redundancy_fraction < 1.0):
        raise ValueError("redundancy_fraction must be in (0, 1)")
    nsym = max(2, min(nsize - 1, round(nsize * redundancy_fraction)))
    return RSConfig(nsize=nsize, nsym=nsym)


def _codec(config: RSConfig) -> RSCodec:
    return RSCodec(nsym=config.nsym, nsize=config.nsize)


def encode_blocks(data: bytes, config: RSConfig) -> bytes:
    """Pad `data` to a multiple of message_len, RS-encode each block
    independently, and concatenate. Caller must record the true (unpadded)
    length elsewhere (it's stored in the frame header)."""
    codec = _codec(config)
    msg_len = config.message_len
    pad = (-len(data)) % msg_len
    padded = data + b"\x00" * pad
    out = bytearray()
    for i in range(0, len(padded), msg_len):
        block = padded[i : i + msg_len]
        out += bytes(codec.encode(block))
    return bytes(out)


def encode_file(src_path: str, dst_path: str, config: RSConfig, read_chunk_blocks: int = 4096) -> int:
    """Streaming version of encode_blocks: never holds more than
    read_chunk_blocks worth of message data in memory at once, regardless of
    src file size."""
    codec = _codec(config)
    msg_len = config.message_len
    chunk_bytes = msg_len * read_chunk_blocks
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        while True:
            chunk = src.read(chunk_bytes)
            if not chunk:
                break
            pad = (-len(chunk)) % msg_len
            if pad:
                chunk = chunk + b"\x00" * pad
            for i in range(0, len(chunk), msg_len):
                dst.write(bytes(codec.encode(chunk[i : i + msg_len])))
    return os.path.getsize(dst_path)


@dataclass
class DecodeStats:
    blocks_total: int = 0
    blocks_ok: int = 0
    blocks_uncorrectable: int = 0
    bytes_corrected: int = 0

    @property
    def block_success_rate(self) -> float:
        return self.blocks_ok / self.blocks_total if self.blocks_total else 1.0


def decode_blocks(
    data: bytes,
    config: RSConfig,
    erasure_mask: Optional[bytes] = None,
) -> tuple[bytes, DecodeStats]:
    """Inverse of encode_blocks. `erasure_mask`, if given, must be the same
    length as `data` with a nonzero byte at every position the modulation layer
    flagged as low-confidence — those positions are passed to RS as erasures,
    which it can correct at up to 2x the rate of blind error correction.
    Uncorrectable blocks are zero-filled and counted in the returned stats so
    callers can support partial recovery instead of failing the whole payload."""
    codec = _codec(config)
    nsize = config.nsize
    msg_len = config.message_len
    if len(data) % nsize != 0:
        raise ValueError(f"decode_blocks: data length {len(data)} is not a multiple of nsize {nsize}")

    stats = DecodeStats()
    out = bytearray()
    for i in range(0, len(data), nsize):
        block = data[i : i + nsize]
        erase_pos = None
        if erasure_mask is not None:
            block_mask = erasure_mask[i : i + nsize]
            erase_pos = [j for j, b in enumerate(block_mask) if b]
            if len(erase_pos) > config.nsym:
                erase_pos = erase_pos[: config.nsym]
        stats.blocks_total += 1
        try:
            decoded_msg, _decoded_with_ecc, errata_pos = codec.decode(block, erase_pos=erase_pos)
            out += bytes(decoded_msg)[:msg_len]
            stats.blocks_ok += 1
            stats.bytes_corrected += len(errata_pos)
        except ReedSolomonError:
            out += b"\x00" * msg_len
            stats.blocks_uncorrectable += 1

    return bytes(out), stats


def decode_file(
    src_path: str,
    dst_path: str,
    config: RSConfig,
    erasure_mask_path: Optional[str] = None,
    read_chunk_blocks: int = 4096,
) -> DecodeStats:
    """Streaming version of decode_blocks."""
    codec = _codec(config)
    nsize = config.nsize
    msg_len = config.message_len
    stats = DecodeStats()
    chunk_bytes = nsize * read_chunk_blocks
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        mask_fh = open(erasure_mask_path, "rb") if erasure_mask_path else None
        try:
            while True:
                block_chunk = src.read(chunk_bytes)
                if not block_chunk:
                    break
                mask_chunk = mask_fh.read(len(block_chunk)) if mask_fh else None
                for i in range(0, len(block_chunk), nsize):
                    block = block_chunk[i : i + nsize]
                    if len(block) != nsize:
                        break  # trailing partial block: caller mis-sized input
                    erase_pos = None
                    if mask_chunk is not None:
                        block_mask = mask_chunk[i : i + nsize]
                        erase_pos = [j for j, b in enumerate(block_mask) if b]
                        if len(erase_pos) > config.nsym:
                            erase_pos = erase_pos[: config.nsym]
                    stats.blocks_total += 1
                    try:
                        decoded_msg, _ecc, errata_pos = codec.decode(block, erase_pos=erase_pos)
                        dst.write(bytes(decoded_msg)[:msg_len])
                        stats.blocks_ok += 1
                        stats.bytes_corrected += len(errata_pos)
                    except ReedSolomonError:
                        dst.write(b"\x00" * msg_len)
                        stats.blocks_uncorrectable += 1
        finally:
            if mask_fh:
                mask_fh.close()
    return stats

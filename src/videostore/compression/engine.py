"""Streaming compression. Runs BEFORE encryption (compressing ciphertext is
useless — it's indistinguishable from random data — see docs/protocol.md).

`auto` mode samples the input instead of blindly compressing: FEC-coded,
modulated channel capacity is expensive, so we should not spend a byte of
video runtime on a compression header that nets zero savings on data that's
already compressed (jpg/zip/mp4/...).
"""
from __future__ import annotations

import enum
import os
from typing import BinaryIO

import zstandard as zstd

CHUNK_SIZE = 1 << 20
SAMPLE_SIZE = 1 << 20  # 1 MiB sample for the `auto` decision
AUTO_MIN_RATIO = 0.97  # if sample doesn't shrink below 97% of original, skip it


class Algorithm(enum.IntEnum):
    NONE = 0
    ZSTD = 1


def decide_auto(sample_path_or_data, level: int = 12) -> Algorithm:
    """Peek at up to SAMPLE_SIZE bytes and decide whether zstd is worth it."""
    if isinstance(sample_path_or_data, (bytes, bytearray)):
        sample = bytes(sample_path_or_data[:SAMPLE_SIZE])
    else:
        with open(sample_path_or_data, "rb") as fh:
            sample = fh.read(SAMPLE_SIZE)
    if not sample:
        return Algorithm.NONE
    compressed = zstd.ZstdCompressor(level=level).compress(sample)
    ratio = len(compressed) / len(sample)
    return Algorithm.ZSTD if ratio < AUTO_MIN_RATIO else Algorithm.NONE


def compress_file(src_path: str, dst_path: str, algorithm: Algorithm, level: int = 19) -> int:
    """Stream-compress src_path -> dst_path. Returns output size in bytes."""
    if algorithm == Algorithm.NONE:
        return _copy(src_path, dst_path)
    cctx = zstd.ZstdCompressor(level=level, threads=-1)
    total = 0
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        with cctx.stream_writer(dst) as writer:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                writer.write(chunk)
                total += len(chunk)
    return os.path.getsize(dst_path)


def decompress_file(src_path: str, dst_path: str, algorithm: Algorithm) -> int:
    if algorithm == Algorithm.NONE:
        return _copy(src_path, dst_path)
    dctx = zstd.ZstdDecompressor()
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        with dctx.stream_reader(src) as reader:
            while True:
                chunk = reader.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
    return os.path.getsize(dst_path)


def _copy(src_path: str, dst_path: str) -> int:
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
    return os.path.getsize(dst_path)

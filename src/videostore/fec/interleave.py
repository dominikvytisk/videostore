"""Block interleaver.

Video transcoding damage is bursty and spatially/temporally correlated (a
whole macroblock, a whole frame region, or a dropped frame gets corrupted
together) rather than uniformly scattered. Reed-Solomon corrects up to
`nsym/2` byte errors *per block* — a burst that lands entirely inside one
block can exceed that easily, while the same number of corrupted bytes spread
across many blocks is trivially correctable. Interleaving trades nothing for
this: it just changes the order bytes are written to the channel, so it turns
correlated burst damage into damage that looks uncorrelated to each RS block.
"""
from __future__ import annotations

import os

import numpy as np


def padded_block_count(n_blocks: int, depth: int) -> int:
    return n_blocks + ((-n_blocks) % depth)


def _flat_memmap_rw(path: str, size: int) -> np.memmap:
    with open(path, "wb") as f:
        f.truncate(size)
    return np.memmap(path, dtype=np.uint8, mode="r+", shape=(size,))


def interleave_file(src_path: str, dst_path: str, block_size: int, depth: int) -> int:
    """File-based version of interleave() using memmap so memory use is
    O(depth * block_size) instead of O(file size) — the FEC-coded payload for
    a multi-GB input can be gigabytes by itself. Operates on FLAT byte offsets
    throughout (not 2D memmap shapes) to avoid a row-width mismatch between
    block_size and depth silently producing a wrong (but shape-valid) result.
    """
    size = os.path.getsize(src_path)
    if size % block_size != 0:
        raise ValueError("interleave_file: src is not a whole number of blocks")
    n_blocks = size // block_size
    n_padded = padded_block_count(n_blocks, depth)
    out_size = n_padded * block_size
    group_bytes = depth * block_size

    src = np.memmap(src_path, dtype=np.uint8, mode="r", shape=(size,))
    dst = _flat_memmap_rw(dst_path, out_size)

    for g in range(n_padded // depth):
        block0 = g * depth
        actual = max(0, min(depth, n_blocks - block0))
        group = np.zeros((depth, block_size), dtype=np.uint8)  # group[d, b]
        if actual > 0:
            flat0 = block0 * block_size
            group[:actual] = src[flat0 : flat0 + actual * block_size].reshape(actual, block_size)
        # out[k] = group[k % depth, k // depth]  <=>  out = group.T.reshape(-1)
        out_start = g * group_bytes
        dst[out_start : out_start + group_bytes] = group.T.reshape(-1)
    dst.flush()
    del src, dst
    return out_size


def deinterleave_file(src_path: str, dst_path: str, block_size: int, depth: int) -> int:
    """Inverse of interleave_file. Exact algebraic inverse of the per-group
    mapping out[k] = group[k % depth, k // depth]:
        group[d, b] = out[b * depth + d]
    i.e. reshape the group's flat bytes as (block_size, depth) and transpose.
    """
    size = os.path.getsize(src_path)
    if size % block_size != 0:
        raise ValueError("deinterleave_file: src is not a whole number of blocks")
    n_blocks = size // block_size
    if n_blocks % depth != 0:
        raise ValueError("deinterleave_file: block count is not a multiple of depth")
    group_bytes = depth * block_size

    src = np.memmap(src_path, dtype=np.uint8, mode="r", shape=(size,))
    dst = _flat_memmap_rw(dst_path, size)

    for g in range(n_blocks // depth):
        start = g * group_bytes
        flat_group = src[start : start + group_bytes]
        original_group = flat_group.reshape(block_size, depth).T  # (depth, block_size)
        dst[start : start + group_bytes] = original_group.reshape(-1)
    dst.flush()
    del src, dst
    return size


def interleave(data: bytes, block_size: int, depth: int) -> bytes:
    """`data` must be a whole number of `block_size`-sized blocks. Groups
    blocks into rows of `depth` blocks (padding the final group with zero
    blocks if needed) and transposes each group byte-column-wise."""
    if len(data) % block_size != 0:
        raise ValueError("interleave: data is not a whole number of blocks")
    n_blocks = len(data) // block_size
    pad_blocks = (-n_blocks) % depth
    if pad_blocks:
        data = data + b"\x00" * (pad_blocks * block_size)
        n_blocks += pad_blocks

    arr = np.frombuffer(data, dtype=np.uint8).reshape(n_blocks // depth, depth, block_size)
    # within each group of `depth` blocks: (depth, block_size) -> transpose -> (block_size, depth)
    interleaved = arr.transpose(0, 2, 1).reshape(-1)
    return interleaved.tobytes()


def deinterleave(data: bytes, block_size: int, depth: int) -> bytes:
    if len(data) % block_size != 0:
        raise ValueError("deinterleave: data is not a whole number of blocks")
    n_blocks = len(data) // block_size
    if n_blocks % depth != 0:
        raise ValueError("deinterleave: block count is not a multiple of depth")

    arr = np.frombuffer(data, dtype=np.uint8).reshape(n_blocks // depth, block_size, depth)
    original = arr.transpose(0, 2, 1).reshape(-1)
    return original.tobytes()

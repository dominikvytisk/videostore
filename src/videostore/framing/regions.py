"""Every frame reserves a fixed top-left pixel rectangle for the frame tag
(synchronization/frame_tag.py) — a tiny, maximally-robust field that lets the
decoder locate itself (which logical frame is this?) before it knows anything
else about the stream. The rest of the usable area carries header or payload
bits depending on the frame's position in the stream (see layout.py).

TAG_REGION_SIZE is chosen to be divisible by every block size used elsewhere
(8/16/32/64) so it always covers a whole number of blocks regardless of which
modulation profile is active, and small enough to be a negligible capacity
tax (64 bits of tag payload per frame, embedded at a very conservative
margin) that even the header (which decoder does NOT need for the tag itself)
can't fit into.
"""
from __future__ import annotations

import numpy as np

from videostore.constants import TAG_REGION_SIZE

__all__ = ["TAG_REGION_SIZE", "tag_block_indices", "payload_capacity_bits", "scatter_logical_bits", "gather_logical_bits"]


def tag_block_indices(width: int, height: int, block_size: int, tag_size: int = TAG_REGION_SIZE) -> np.ndarray:
    """Linear (row-major) block indices, within the block grid produced by a
    ModulationScheme with this block_size, that fall inside the [0,tag_size)^2
    reserved rectangle."""
    from .base_dims import block_grid_shape  # local import, tiny helper module

    rows, cols = block_grid_shape(width, height, block_size)
    tag_rows = min(rows, tag_size // block_size)
    tag_cols = min(cols, tag_size // block_size)
    idx = np.arange(rows * cols).reshape(rows, cols)
    return idx[:tag_rows, :tag_cols].reshape(-1)


def _excluded_group_indices(width: int, height: int, block_size: int, capacity_blocks: int, spread_factor: int = 1) -> set:
    """Which logical bit positions (== capacity_blocks() of a scheme) must be
    skipped because the tag region will overwrite part of the signal there.
    For spread_factor==1 a logical bit IS a raw block, so this is just the
    raw tag-block indices. For spread_factor>1, `capacity_blocks` groups
    `spread_factor` CONSECUTIVE raw blocks per logical bit (see
    modulation/masked_luminance.py) -- a group is excluded if ANY of its raw
    blocks falls in the tag region, since the tag would otherwise clobber
    part of that group's aggregate signal."""
    raw_excluded = tag_block_indices(width, height, block_size).tolist()
    if spread_factor <= 1:
        return set(raw_excluded)
    excluded = set()
    for i in raw_excluded:
        g = i // spread_factor
        if g < capacity_blocks:
            excluded.add(g)
    return excluded


def payload_capacity_bits(width: int, height: int, block_size: int, capacity_blocks: int, spread_factor: int = 1) -> int:
    excluded = _excluded_group_indices(width, height, block_size, capacity_blocks, spread_factor)
    return capacity_blocks - len(excluded)


def scatter_logical_bits(
    logical_bits: np.ndarray, width: int, height: int, block_size: int, capacity_blocks: int, spread_factor: int = 1
) -> np.ndarray:
    """Place `logical_bits` (length == payload_capacity_bits(...)) into a full
    capacity_blocks-length array in row-major block order, skipping tag-region
    block indices (left as 0 — those pixels get overwritten by the tag encoder
    afterward anyway)."""
    excluded = _excluded_group_indices(width, height, block_size, capacity_blocks, spread_factor)
    full = np.zeros(capacity_blocks, dtype=np.uint8)
    li = 0
    for i in range(capacity_blocks):
        if i in excluded:
            continue
        full[i] = logical_bits[li]
        li += 1
    if li != len(logical_bits):
        raise ValueError(f"scatter_logical_bits: expected {li} logical bits, got {len(logical_bits)}")
    return full


def gather_logical_bits(
    full_bits: np.ndarray, full_confidence: np.ndarray, width: int, height: int, block_size: int, spread_factor: int = 1
) -> tuple[np.ndarray, np.ndarray]:
    excluded = _excluded_group_indices(width, height, block_size, len(full_bits), spread_factor)
    mask = np.ones(len(full_bits), dtype=bool)
    if excluded:
        mask[list(excluded)] = False
    return full_bits[mask], full_confidence[mask]

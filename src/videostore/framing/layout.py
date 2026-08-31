"""Ties frame tagging (synchronization/), the reserved tag region (regions.py)
and a chosen payload ModulationScheme together into whole-frame bit layouts
for encode and decode.

Header frames and payload frames never share a frame index — a frame is
either one or the other — so within a frame's usable-minus-tag area there is
only ever one modulation scheme active (HEADER_MODULATION for header frames,
whatever the header declares for payload frames). See docs/protocol.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from videostore.container.format import GlobalHeader, HEADER_LEN
from videostore.modulation import LuminanceBlockModulation, PerceptualMaskedModulation
from videostore.modulation.base import ModulationScheme

from .regions import payload_capacity_bits

# Fixed by protocol version, NOT stored in the header — the header itself must
# be readable using only protocol constants + the frame tag (which is
# resolution-self-describing; see synchronization/frame_tag.py).
HEADER_MODULATION_SYNTHETIC = LuminanceBlockModulation(block_size=16, margin=48.0)
# Cover-video mode's header frames overwrite the *entire* frame (not just the
# tag's small corner), so a flat 48.0 margin here would be a much bigger
# visibility problem than the tag. Perceptually masked, same rationale as
# TAG_MODULATION_STEALTH (see synchronization/frame_tag.py) — the decoder
# learns which of the two to use from which TAG_MODULATION variant matched
# during sync-scan, since both are always paired 1:1 by mode.
HEADER_MODULATION_STEALTH = PerceptualMaskedModulation(block_size=16, margin=48.0, margin_floor=18.0)
HEADER_MODULATION = HEADER_MODULATION_SYNTHETIC  # default/backward-compatible name

HEADER_BITS = HEADER_LEN * 8


def header_capacity_per_frame(width: int, height: int) -> int:
    cap = HEADER_MODULATION.capacity_blocks(width, height)
    return payload_capacity_bits(width, height, HEADER_MODULATION.block_size, cap)


def payload_capacity_per_frame(width: int, height: int, modulation: ModulationScheme) -> int:
    cap = modulation.capacity_blocks(width, height)
    return payload_capacity_bits(width, height, modulation.block_size, cap, spread_factor=modulation.spread_factor)


def frames_needed_for_payload(payload_bits: int, width: int, height: int, modulation: ModulationScheme) -> int:
    per_frame = payload_capacity_per_frame(width, height, modulation)
    if per_frame <= 0:
        raise ValueError("payload modulation has zero usable capacity at this resolution")
    return math.ceil(payload_bits / per_frame)


def tile_header_bits(header_bytes: bytes, total_bits_needed: int) -> np.ndarray:
    """Repeat the packed header's bits until `total_bits_needed` bits are filled."""
    from videostore.utils.bitstream import bytes_to_bits

    header_bits = bytes_to_bits(header_bytes)
    assert len(header_bits) == HEADER_BITS
    reps = math.ceil(total_bits_needed / HEADER_BITS)
    tiled = np.tile(header_bits, reps)[:total_bits_needed]
    return tiled


@dataclass
class HeaderRecoveryResult:
    header_bytes: bytes
    tiles_found: int
    mean_confidence: float
    crc_ok: bool


def recover_header_bits(all_bits: np.ndarray, all_confidence: np.ndarray) -> HeaderRecoveryResult:
    """`all_bits`/`all_confidence` are the concatenation of every header
    frame's logical bits/confidence, in ascending frame_index order. Performs
    a confidence-weighted majority vote of every HEADER_BITS-aligned tile."""
    from videostore.utils.bitstream import bits_to_bytes
    from videostore.container.format import GlobalHeader as _GH

    n_tiles = len(all_bits) // HEADER_BITS
    if n_tiles < 1:
        raise ValueError("not enough header data recovered to attempt reconstruction")
    usable = n_tiles * HEADER_BITS
    bits = all_bits[:usable].reshape(n_tiles, HEADER_BITS).astype(np.float64)
    conf = all_confidence[:usable].reshape(n_tiles, HEADER_BITS).astype(np.float64)

    # Confidence-weighted soft vote: map bit in {0,1} -> {-1,+1}, weight by
    # confidence, sign of the weighted sum is the recovered bit.
    signed = np.where(bits > 0, 1.0, -1.0)
    weighted = (signed * np.maximum(conf, 1e-6)).sum(axis=0)
    recovered_bits = (weighted > 0).astype(np.uint8)
    mean_conf = float(conf.mean())

    header_bytes = bits_to_bytes(recovered_bits)[: HEADER_LEN]
    try:
        _GH.unpack(header_bytes)
        crc_ok = True
    except ValueError:
        crc_ok = False
    return HeaderRecoveryResult(header_bytes=header_bytes, tiles_found=n_tiles, mean_confidence=mean_conf, crc_ok=crc_ok)


class FrameLayout:
    """Convenience wrapper bundling the above for a specific header+modulation."""

    def __init__(self, header: GlobalHeader, payload_modulation: ModulationScheme):
        self.header = header
        self.payload_modulation = payload_modulation
        self.width = header.frame_width
        self.height = header.frame_height

    @property
    def header_capacity(self) -> int:
        return header_capacity_per_frame(self.width, self.height)

    @property
    def payload_capacity(self) -> int:
        return payload_capacity_per_frame(self.width, self.height, self.payload_modulation)

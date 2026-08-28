"""Per-frame synchronization tag.

Every single frame (header or payload) carries a tiny 64-bit tag in a fixed
top-left 256x256 pixel rectangle: `frame_index` (this frame's logical
position in the original stream), a `session_tag` (a few bits derived from
the session ID, so a decoder scanning an arbitrary downloaded video can tell
"this is/isn't a VideoStore stream, and isn't a stream from a different
encode" before trusting anything else in it), and a CRC.

This design (continuous per-frame self-location, vs. spec section 17's
sparse "every K frames: checkpoint") gives every single frame independent
synchronization instead of only every Kth one. It costs 64 bits/frame at a
very conservative margin (tiny compared to a payload frame's capacity) and
in exchange the decoder can correctly reassemble a video that has had frames
dropped, duplicated, or reordered by an fps conversion — it doesn't need to
find the *nearest* checkpoint and hope nothing shifted since.

The tag's own modulation parameters (block_size=32, margin=64) are a fixed
protocol constant, not something stored in the header — the decoder has to
be able to read frame_index before it has even fully recovered the header.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from videostore.modulation import LuminanceBlockModulation
from videostore.utils.bitstream import bits_to_bytes, bytes_to_bits
from videostore.utils.hashing import crc32

from ..constants import TAG_REGION_SIZE

TAG_MODULATION = LuminanceBlockModulation(block_size=16, margin=56.0)
# frame_index, session_tag, ENCODER'S logical frame_width/frame_height, crc16.
# width/height are duplicated here (also in the GlobalHeader) so the decoder
# can tell "did YouTube actually serve me the resolution I encoded at?" from
# frame 0 alone, at a fixed pixel offset, before it has decoded the header —
# see layout.py / decoder/pipeline.py for how this resolves the bootstrap
# problem (block-grid math needs a resolution; the header needs a resolution
# to be readable in the first place).
_TAG_FMT = ">IHHHH"  # frame_index, session_tag, frame_width, frame_height, crc16
_TAG_BYTES = struct.calcsize(_TAG_FMT)
TAG_BITS = _TAG_BYTES * 8


def session_tag_from_id(session_id: bytes) -> int:
    return struct.unpack(">H", session_id[:2])[0]


def _pack(frame_index: int, session_tag: int, frame_width: int, frame_height: int) -> bytes:
    body = struct.pack(">IHHH", frame_index, session_tag, frame_width, frame_height)
    crc16 = crc32(body) & 0xFFFF
    return body + struct.pack(">H", crc16)


@dataclass
class FrameTag:
    frame_index: int
    session_tag: int
    frame_width: int
    frame_height: int
    valid: bool
    confidence: float


def embed_tag(plane: np.ndarray, frame_index: int, session_tag: int, frame_width: int, frame_height: int) -> np.ndarray:
    tag_bytes = _pack(frame_index, session_tag, frame_width, frame_height)
    bits = bytes_to_bits(tag_bytes)
    region = plane[:TAG_REGION_SIZE, :TAG_REGION_SIZE]
    n = TAG_MODULATION.capacity_blocks(region.shape[1], region.shape[0])
    if n < len(bits):
        raise ValueError(f"tag region too small: capacity {n} bits < {len(bits)} needed")
    padded_bits = np.zeros(n, dtype=np.uint8)
    padded_bits[: len(bits)] = bits
    modified = TAG_MODULATION.embed(region, padded_bits)
    out = plane.copy()
    out[:TAG_REGION_SIZE, :TAG_REGION_SIZE] = modified
    return out


def extract_tag(plane: np.ndarray) -> FrameTag:
    region = plane[:TAG_REGION_SIZE, :TAG_REGION_SIZE]
    bits, conf = TAG_MODULATION.extract(region)
    tag_bits = bits[:TAG_BITS]
    tag_bytes = bits_to_bytes(tag_bits)
    frame_index, session_tag, frame_width, frame_height, crc16 = struct.unpack(_TAG_FMT, tag_bytes)
    body = struct.pack(">IHHH", frame_index, session_tag, frame_width, frame_height)
    valid = (crc32(body) & 0xFFFF) == crc16
    mean_conf = float(conf[: TAG_BITS].mean())
    return FrameTag(
        frame_index=frame_index,
        session_tag=session_tag,
        frame_width=frame_width,
        frame_height=frame_height,
        valid=valid,
        confidence=mean_conf,
    )

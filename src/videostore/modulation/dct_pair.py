"""DCT mid-band coefficient-pair modulation (Koch-Zhao style relative
watermarking, adapted for high-density data embedding rather than a single
watermark bit).

Why this over absolute coefficient values: H.264/H.265/AV1 all quantize DCT
(or DCT-like, e.g. AV1's ADST) coefficients on the encoder side and the
*relative magnitude* of two coefficients that sit at the same "distance" from
DC survives quantization far better than either coefficient's absolute value,
because both get divided by similar quantizer step sizes and rounded the same
direction most of the time. Choosing a coefficient pair with equal (u+v) —
default (2,3) and (3,2) — means both lie on the same zig-zag/frequency band,
so a codec's per-band quantization affects them symmetrically. This is the
same principle behind classic Koch-Zhao / patchwork DCT watermarking; we're
using it as a data channel rather than a single ownership bit.

High-frequency coefficients are avoided entirely (per spec section 4B) — they
are the first thing quantized to zero at any nontrivial CRF.
"""
from __future__ import annotations

import numpy as np

from videostore.video.dct import blocks_from_plane, dct2_batch, idct2_batch, plane_from_blocks

from .base import ModulationScheme, register, usable_dims


@register
class DCTPairModulation(ModulationScheme):
    name = "dct-pair"
    scheme_id = 0

    def __init__(
        self,
        block_size: int = 8,
        margin: float = 12.0,
        coeff_a: tuple[int, int] = (2, 3),
        coeff_b: tuple[int, int] = (3, 2),
        spread_factor: int = 1,
    ):
        super().__init__(block_size=block_size, margin=margin, spread_factor=spread_factor)
        self.coeff_a = coeff_a
        self.coeff_b = coeff_b
        if block_size < 4:
            raise ValueError("block_size must be >= 4")
        au, av = coeff_a
        bu, bv = coeff_b
        if not (0 <= au < block_size and 0 <= av < block_size and 0 <= bu < block_size and 0 <= bv < block_size):
            raise ValueError("coeff_a/coeff_b out of range for block_size")

    def capacity_blocks(self, width: int, height: int) -> int:
        uw, uh = usable_dims(width, height, self.block_size)
        return (uw // self.block_size) * (uh // self.block_size)

    def embed(self, plane: np.ndarray, bits: np.ndarray) -> np.ndarray:
        h, w = plane.shape
        uw, uh = usable_dims(w, h, self.block_size)
        n = self.capacity_blocks(w, h)
        if len(bits) != n:
            raise ValueError(f"expected {n} bits, got {len(bits)}")

        out = plane.astype(np.float64).copy()
        blocks = blocks_from_plane(out[:uh, :uw], self.block_size)
        coeffs = dct2_batch(blocks)

        au, av = self.coeff_a
        bu, bv = self.coeff_b
        a_val = coeffs[:, au, av].copy()
        b_val = coeffs[:, bu, bv].copy()
        diff = a_val - b_val
        desired = np.where(bits == 1, 1.0, -1.0)
        shortfall = self.margin - desired * diff
        delta = np.where(shortfall > 0, shortfall / 2.0, 0.0)
        coeffs[:, au, av] = a_val + desired * delta
        coeffs[:, bu, bv] = b_val - desired * delta

        blocks_mod = idct2_batch(coeffs)
        out[:uh, :uw] = plane_from_blocks(blocks_mod, uh, uw, self.block_size)
        return out

    def extract(self, plane: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h, w = plane.shape
        uw, uh = usable_dims(w, h, self.block_size)
        blocks = blocks_from_plane(plane.astype(np.float64)[:uh, :uw], self.block_size)
        coeffs = dct2_batch(blocks)
        au, av = self.coeff_a
        bu, bv = self.coeff_b
        diff = coeffs[:, au, av] - coeffs[:, bu, bv]
        bits = (diff > 0).astype(np.uint8)
        confidence = np.clip(np.abs(diff) / self.margin, 0.0, 1.0)
        return bits, confidence

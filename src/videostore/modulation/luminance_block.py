"""Block-average luminance modulation: compares the mean luma of the top half
vs. bottom half of a block. Simpler and spatially coarser than DCT-pair
modulation (spec section 4A "block-average modulation"), kept as the baseline
alternative the benchmark suite compares DCT-pair against — averaging over a
whole half-block is inherently robust to chroma subsampling and to isolated
pixel-level noise, at the cost of much lower capacity per pixel and more
visible blocking at aggressive margins. Whether this or DCT-pair wins after a
real transcode is an empirical question the benchmark answers, not an
assumption (spec section 53).
"""
from __future__ import annotations

import numpy as np

from videostore.video.dct import blocks_from_plane, plane_from_blocks

from .base import ModulationScheme, register, usable_dims


@register
class LuminanceBlockModulation(ModulationScheme):
    name = "luminance-block"
    scheme_id = 1

    def __init__(self, block_size: int = 16, margin: float = 20.0):
        super().__init__(block_size=block_size, margin=margin)
        if block_size % 2 != 0:
            raise ValueError("block_size must be even (split into top/bottom halves)")

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
        half = self.block_size // 2
        top = blocks[:, :half, :]
        bottom = blocks[:, half:, :]
        top_mean = top.mean(axis=(1, 2))
        bottom_mean = bottom.mean(axis=(1, 2))
        diff = top_mean - bottom_mean
        desired = np.where(bits == 1, 1.0, -1.0)
        shortfall = self.margin - desired * diff
        delta = np.where(shortfall > 0, shortfall / 2.0, 0.0)

        new_top = np.clip(top + (desired * delta)[:, None, None], 0, 255)
        new_bottom = np.clip(bottom - (desired * delta)[:, None, None], 0, 255)
        out_blocks = np.concatenate([new_top, new_bottom], axis=1)
        out[:uh, :uw] = plane_from_blocks(out_blocks, uh, uw, self.block_size)
        return out

    def extract(self, plane: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h, w = plane.shape
        uw, uh = usable_dims(w, h, self.block_size)
        blocks = blocks_from_plane(plane.astype(np.float64)[:uh, :uw], self.block_size)
        half = self.block_size // 2
        top_mean = blocks[:, :half, :].mean(axis=(1, 2))
        bottom_mean = blocks[:, half:, :].mean(axis=(1, 2))
        diff = top_mean - bottom_mean
        bits = (diff > 0).astype(np.uint8)
        confidence = np.clip(np.abs(diff) / self.margin, 0.0, 1.0)
        return bits, confidence

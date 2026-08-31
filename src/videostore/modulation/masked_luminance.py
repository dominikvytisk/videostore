"""Perceptually-masked luminance-block modulation (cover-video/"stego" mode,
scheme_id=2). Same top-half-vs-bottom-half mean-difference mechanic as
`LuminanceBlockModulation`, except the push magnitude ("local margin") is not
a flat constant -- it scales with each block's own local contrast, so a flat
region (sky, shadow, out-of-focus background) gets a small, harder-to-notice
push while a highly textured region gets pushed as hard as the flat scheme
would (`margin` is a ceiling, not a fixed push). This is the capacity-parity
design from docs/architecture.md's cover-video section: every block stays
usable (same `capacity_blocks()` as `LuminanceBlockModulation`, so framing/
layout math is unaffected), only the per-block push size adapts.

Local contrast is measured as the *average of the top-half and bottom-half
standard deviations*, not the whole block's std -- pushing top/bottom by a
uniform delta is a per-pixel shift within each half, which doesn't change
that half's own variance at all. So this measurement is invariant to our own
embedding, on both sides: the encoder computes it from the pre-embed cover
frame, and the decoder recomputes the identical statistic from the (possibly
transcoded) received frame, without it being contaminated by our own push in
either direction. That symmetry is what keeps the two sides' independently
computed masks in agreement (see docs/architecture.md's "mask desync"
writeup for the failure mode this is designed to avoid) -- but it doesn't
fully solve it: real compression can still shift a block's true local
contrast between encode and decode, especially near the margin_floor/ceiling
boundaries. `margin_floor` and quantizing the margin into a small number of
discrete tiers are the two mitigations for that residual risk (small,
transcode-induced statistic shifts rarely cross a tier boundary; a floor high
enough above the FEC layer's erasure-confidence threshold means a legitimately
weak block degrades to "erasure" -- which Reed-Solomon corrects far more
reliably than a silent wrong bit -- rather than to a confidently-wrong bit).

The bit *value* itself is still just sign(top_mean - bottom_mean), exactly as
in the flat scheme -- local_margin is only used to size the push at encode
time and to normalize confidence at decode time, never to decide the bit.

## Spread-spectrum mode (`spread_factor` > 1)

Optional, off by default (`spread_factor=1` reduces everything below to
exactly the single-block behavior described above). When enabled, each
LOGICAL bit is spent across `spread_factor` consecutive raw blocks instead
of one: each of those blocks gets pushed only `local_margin/spread_factor`
(so the *sum* of `spread_factor` independent pushes reconstructs to roughly
the original margin's worth of aggregate signal), and `extract()` sums the
group's diffs before deciding the bit. This trades capacity (fewer logical
bits per frame -- more frames/duration needed for the same payload) for a
much smaller per-block visible delta, which is the standard spread-spectrum
lever for reducing perceived flicker: the same "signal energy" spread over
more, smaller, lower-amplitude pushes reads as texture/grain rather than a
few large, sharply-defined ones. `capacity_blocks()` reports the number of
GROUPS, not raw blocks -- this is what the framing layer treats as "one
logical bit," and `framing/regions.py`'s tag-exclusion math is
spread-aware (a whole group is excluded if any of its raw blocks falls in
the tag's reserved region, since the tag will clobber part of the group's
signal otherwise).
"""
from __future__ import annotations

import numpy as np

from videostore.video.dct import blocks_from_plane, plane_from_blocks

from .base import ModulationScheme, register, usable_dims


@register
class PerceptualMaskedModulation(ModulationScheme):
    name = "masked-luminance"
    scheme_id = 2

    def __init__(
        self,
        block_size: int = 16,
        margin: float = 48.0,
        mask_gain: float = 1.5,
        margin_floor: float = 16.0,
        tiers: int = 4,
        spread_factor: int = 1,
    ):
        super().__init__(block_size=block_size, margin=margin, spread_factor=spread_factor)
        if block_size % 2 != 0:
            raise ValueError("block_size must be even (split into top/bottom halves)")
        if not (0 < margin_floor <= margin):
            raise ValueError("margin_floor must be in (0, margin]")
        if spread_factor < 1:
            raise ValueError("spread_factor must be >= 1")
        self.mask_gain = mask_gain
        self.margin_floor = margin_floor
        self.tiers = tiers

    def _raw_block_count(self, width: int, height: int) -> int:
        uw, uh = usable_dims(width, height, self.block_size)
        return (uw // self.block_size) * (uh // self.block_size)

    def capacity_blocks(self, width: int, height: int) -> int:
        return self._raw_block_count(width, height) // self.spread_factor

    def _split_and_margin(self, cropped: np.ndarray):
        """Returns (top_blocks, bottom_blocks, top_mean, bottom_mean,
        local_margin) for a (uh, uw) plane already cropped to a multiple of
        block_size."""
        blocks = blocks_from_plane(cropped, self.block_size)
        half = self.block_size // 2
        top = blocks[:, :half, :]
        bottom = blocks[:, half:, :]
        top_mean = top.mean(axis=(1, 2))
        bottom_mean = bottom.mean(axis=(1, 2))
        local_std = 0.5 * (top.std(axis=(1, 2)) + bottom.std(axis=(1, 2)))
        local_margin = np.clip(self.mask_gain * local_std, self.margin_floor, self.margin)
        if self.tiers > 1:
            step = (self.margin - self.margin_floor) / (self.tiers - 1)
            local_margin = self.margin_floor + np.round((local_margin - self.margin_floor) / step) * step
        return top, bottom, top_mean, bottom_mean, local_margin

    def embed(self, plane: np.ndarray, bits: np.ndarray) -> np.ndarray:
        h, w = plane.shape
        uw, uh = usable_dims(w, h, self.block_size)
        n = self.capacity_blocks(w, h)
        if len(bits) != n:
            raise ValueError(f"expected {n} bits, got {len(bits)}")

        out = plane.astype(np.float64).copy()
        cropped = out[:uh, :uw]
        top, bottom, top_mean, bottom_mean, local_margin = self._split_and_margin(cropped)
        used_raw = n * self.spread_factor  # trailing raw blocks beyond this are left untouched

        diff = top_mean[:used_raw] - bottom_mean[:used_raw]
        desired_group = np.where(bits == 1, 1.0, -1.0)
        desired = np.repeat(desired_group, self.spread_factor)  # length used_raw
        eff_margin = local_margin[:used_raw] / self.spread_factor
        shortfall = eff_margin - desired * diff
        delta = np.where(shortfall > 0, shortfall / 2.0, 0.0)

        out_top = top.copy()
        out_bottom = bottom.copy()
        out_top[:used_raw] = np.clip(top[:used_raw] + (desired * delta)[:, None, None], 0, 255)
        out_bottom[:used_raw] = np.clip(bottom[:used_raw] - (desired * delta)[:, None, None], 0, 255)
        out_blocks = np.concatenate([out_top, out_bottom], axis=1)
        out[:uh, :uw] = plane_from_blocks(out_blocks, uh, uw, self.block_size)
        return out

    def extract(self, plane: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h, w = plane.shape
        uw, uh = usable_dims(w, h, self.block_size)
        cropped = plane.astype(np.float64)[:uh, :uw]
        _, _, top_mean, bottom_mean, local_margin = self._split_and_margin(cropped)

        raw_n = top_mean.shape[0]
        n = raw_n // self.spread_factor
        used_raw = n * self.spread_factor

        diff = (top_mean - bottom_mean)[:used_raw]
        eff_margin = local_margin[:used_raw] / self.spread_factor
        if self.spread_factor == 1:
            agg_diff = diff
            agg_margin = eff_margin
        else:
            agg_diff = diff.reshape(n, self.spread_factor).sum(axis=1)
            agg_margin = eff_margin.reshape(n, self.spread_factor).sum(axis=1)

        bits = (agg_diff > 0).astype(np.uint8)
        confidence = np.clip(np.abs(agg_diff) / np.maximum(agg_margin, 1e-9), 0.0, 1.0)
        return bits, confidence

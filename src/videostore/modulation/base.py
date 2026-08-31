"""Common interface every modulation scheme implements (spec section 40).

A scheme embeds/extracts one bit per "block" (its own definition of block —
an 8x8 DCT block, a 16x16 luminance-comparison block, etc.) for a whole batch
of blocks at once, because per-block Python-level loops are too slow for
1080p+ frame rates. Confidence is a float in [0, 1]; 0.5 means "no idea",
further from 0.5 means more confident. Confidence feeds FEC's erasure
decoding (fec/reed_solomon.py) instead of implementing full soft-decision
belief propagation — see that module's docstring for why.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


def usable_dims(width: int, height: int, block_size: int) -> tuple[int, int]:
    """Largest (width, height) at or below the given dims that are exact
    multiples of block_size. Standard resolutions (e.g. 1080) are not multiples
    of every candidate block_size (1080 % 32 != 0), so modulation schemes crop
    to this usable region and leave a thin unused border rather than requiring
    the caller to pick "block-size-friendly" resolutions."""
    return (width - width % block_size, height - height % block_size)


class ModulationScheme(ABC):
    name: str = "base"
    scheme_id: int = -1

    def __init__(self, block_size: int, margin: float, spread_factor: int = 1):
        self.block_size = block_size
        self.margin = margin
        # Spread-spectrum-style redundancy: a scheme MAY spend `spread_factor`
        # blocks per logical bit instead of 1, at reduced per-block amplitude
        # (see modulation/masked_luminance.py). Stored generically here so
        # `get_modulation` (self-describing from GlobalHeader.mod_spread_factor)
        # doesn't need to special-case which schemes implement it — a scheme
        # that ignores it (spread_factor stays 1 in practice for those) is
        # unaffected.
        self.spread_factor = spread_factor

    @abstractmethod
    def capacity_blocks(self, width: int, height: int) -> int:
        """Number of blocks (== bits, at 1 bit/symbol) available in one plane."""

    @abstractmethod
    def embed(self, plane: np.ndarray, bits: np.ndarray) -> np.ndarray:
        """plane: (H, W) float64 luma plane. bits: (capacity_blocks,) uint8.
        Returns a modified (H, W) float64 plane, values NOT yet clipped/rounded
        to uint8 (caller does that once, after all embedding for the frame)."""

    @abstractmethod
    def extract(self, plane: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Returns (bits (capacity_blocks,) uint8, confidence (capacity_blocks,) float64 in [0,1])."""


MODULATIONS: dict[int, type[ModulationScheme]] = {}


def register(cls: type[ModulationScheme]) -> type[ModulationScheme]:
    MODULATIONS[cls.scheme_id] = cls
    return cls


def get_modulation(scheme_id: int, block_size: int, margin: float, spread_factor: int = 1) -> ModulationScheme:
    if scheme_id not in MODULATIONS:
        raise ValueError(f"unknown modulation scheme_id={scheme_id}")
    return MODULATIONS[scheme_id](block_size=block_size, margin=margin, spread_factor=spread_factor)

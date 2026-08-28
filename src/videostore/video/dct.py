"""Vectorized batched 2D DCT-II/III (8x8 by default) over numpy arrays.

Implemented as matrix multiplication against a precomputed orthonormal basis
rather than calling into a per-block Python loop or scipy — a whole frame's
worth of blocks (tens of thousands at 1080p) needs to transform in a few
milliseconds, and `Cmat @ blocks @ Cmat.T` batches over the leading axis via
numpy's matmul broadcasting, which is what makes that fast enough to be
practical for per-frame embedding at encode time.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=8)
def dct_matrix(n: int) -> np.ndarray:
    """Orthonormal n x n DCT-II basis matrix C such that C @ x @ C.T is the 2D
    DCT-II of block x, and C.T @ X @ C is the inverse (2D DCT-III)."""
    k = np.arange(n).reshape(-1, 1)
    i = np.arange(n).reshape(1, -1)
    c = np.cos(np.pi / n * (i + 0.5) * k)
    alpha = np.full((n, 1), np.sqrt(2.0 / n))
    alpha[0, 0] = np.sqrt(1.0 / n)
    return (alpha * c).astype(np.float64)


def blocks_from_plane(plane: np.ndarray, block_size: int) -> np.ndarray:
    """(H, W) -> (n_blocks_h * n_blocks_w, block_size, block_size). H and W
    must be multiples of block_size (callers pad the plane beforehand)."""
    h, w = plane.shape
    bh, bw = h // block_size, w // block_size
    blocks = plane.reshape(bh, block_size, bw, block_size).transpose(0, 2, 1, 3)
    return blocks.reshape(bh * bw, block_size, block_size)


def plane_from_blocks(blocks: np.ndarray, h: int, w: int, block_size: int) -> np.ndarray:
    bh, bw = h // block_size, w // block_size
    arr = blocks.reshape(bh, bw, block_size, block_size).transpose(0, 2, 1, 3)
    return arr.reshape(h, w)


def dct2_batch(blocks: np.ndarray) -> np.ndarray:
    """blocks: (N, n, n) float -> DCT-II coefficients, same shape."""
    n = blocks.shape[-1]
    c = dct_matrix(n)
    return c @ blocks @ c.T


def idct2_batch(coeffs: np.ndarray) -> np.ndarray:
    n = coeffs.shape[-1]
    c = dct_matrix(n)
    return c.T @ coeffs @ c

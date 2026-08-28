"""Synthetic test corpus for the benchmark suite (spec section 32): a spread
of compressibility/entropy profiles so `--compression auto` and FEC/modulation
robustness get exercised against realistic data shapes, not just one kind."""
from __future__ import annotations

import os
import struct
import zlib

import numpy as np


def generate_test_files(out_dir: str, size_bytes: int = 200_000) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.default_rng(1234)
    paths: dict[str, str] = {}

    # 1. Uniform random (incompressible) — models already-encrypted/compressed data.
    p = os.path.join(out_dir, "random.bin")
    with open(p, "wb") as f:
        f.write(rng.bytes(size_bytes))
    paths["random"] = p

    # 2. Highly compressible text.
    p = os.path.join(out_dir, "text.txt")
    line = "the quick brown fox jumps over the lazy dog. " * 4 + "\n"
    with open(p, "w") as f:
        n = size_bytes // len(line)
        f.write(line * n)
    paths["text"] = p

    # 3. Synthetic "image-like" data: smooth gradients + noise (moderately compressible).
    p = os.path.join(out_dir, "image.raw")
    side = int((size_bytes) ** 0.5)
    yy, xx = np.mgrid[0:side, 0:side]
    grad = ((xx * 3 + yy * 7) % 256).astype(np.uint8)
    noise = rng.integers(0, 20, (side, side)).astype(np.uint8)
    img = grad + noise
    with open(p, "wb") as f:
        f.write(img.tobytes())
    paths["image"] = p

    # 4. Already-compressed archive (zlib) — should NOT benefit from further compression.
    p = os.path.join(out_dir, "already_compressed.zbin")
    raw = (line * (size_bytes // len(line))).encode()
    compressed = zlib.compress(raw, level=9)
    with open(p, "wb") as f:
        f.write(compressed)
    paths["already_compressed"] = p

    # 5. Structured "binary executable-like" data: repeating headers + varied bodies.
    p = os.path.join(out_dir, "structured.bin")
    with open(p, "wb") as f:
        n_records = size_bytes // 64
        for i in range(n_records):
            f.write(struct.pack(">I", i) + rng.bytes(60))
    paths["structured"] = p

    return paths


def generate_mixed_dataset(out_dir: str, size_bytes: int = 200_000) -> str:
    """A subdirectory containing one of each test file type, for exercising
    the archive/manifest layer with multiple files at once."""
    mixed_dir = os.path.join(out_dir, "mixed")
    generate_test_files(mixed_dir, size_bytes // 5)
    return mixed_dir

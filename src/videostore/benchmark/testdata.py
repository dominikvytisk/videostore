"""Synthetic test corpus for the benchmark suite (spec section 32): a spread
of compressibility/entropy profiles so `--compression auto` and FEC/modulation
robustness get exercised against realistic data shapes, not just one kind."""
from __future__ import annotations

import os
import struct
import subprocess
import zlib

import numpy as np

from videostore.video.io import FFMPEG


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


# Synthetic (ffmpeg lavfi) cover-video corpus for cover-video/stego-mode
# benchmarking, spanning a spread of local-texture profiles: a genuinely flat
# clip (the worst case for the masked scheme -- everything sits at
# margin_floor), a highly detailed one (best case, near-parity capacity), and
# one with real motion (deblocking/motion-compensation stresses local-texture
# statistics the most, the closest proxy this corpus has to the "mask desync"
# risk in docs/architecture.md). This is a proxy for real footage, not a
# substitute for it -- lavfi patterns don't have real camera noise or a real
# compression history. See docs/benchmarking.md's cover-video section.
COVER_VIDEO_SOURCES: dict[str, str] = {
    "flat": "color=c=gray:s={size}:r={fps}:d={duration}",
    "detailed": "mandelbrot=size={size}:rate={fps}",
    "motion": "testsrc2=size={size}:rate={fps}:duration={duration}",
}


def generate_test_videos(
    out_dir: str,
    width: int = 640,
    height: int = 360,
    fps: int = 30,
    duration: int = 6,
) -> dict[str, str]:
    """Short synthetic cover clips for benchmarking cover-video mode -- see
    COVER_VIDEO_SOURCES for what each one is meant to stress."""
    os.makedirs(out_dir, exist_ok=True)
    size = f"{width}x{height}"
    paths: dict[str, str] = {}
    for name, lavfi in COVER_VIDEO_SOURCES.items():
        p = os.path.join(out_dir, f"cover_{name}.mp4")
        source = lavfi.format(size=size, fps=fps, duration=duration)
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", source,
        ]
        if name == "detailed":  # mandelbrot has no built-in duration cutoff
            cmd += ["-t", str(duration)]
        cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", p]
        subprocess.run(cmd, check=True)
        paths[name] = p
    return paths

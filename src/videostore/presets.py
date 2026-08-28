"""Resolution and reliability-profile presets.

The profile parameters (block_size, margin, FEC redundancy) are NOT arbitrary
— they come directly from the channel-survival measurements in
docs/benchmarking.md (real ffmpeg H.264/H.265/AV1 round-trips at a range of
CRFs against a flat-cover test pattern). See that doc for the full table and
docs/architecture.md for the "why block-average luminance modulation beat
DCT-pair" writeup. Re-run `videostore benchmark` after changing hardware/ffmpeg
versions before trusting these numbers on a new machine.
"""
from __future__ import annotations

from dataclasses import dataclass

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}


@dataclass(frozen=True)
class Profile:
    name: str
    modulation_type: int  # matches ModulationScheme.scheme_id
    block_size: int
    margin: float
    fec_redundancy: float
    interleave_depth: int
    description: str


PROFILES: dict[str, Profile] = {
    "maximum-capacity": Profile(
        name="maximum-capacity",
        modulation_type=1,  # luminance-block
        block_size=8,
        margin=24.0,
        fec_redundancy=0.15,
        interleave_depth=16,
        description=(
            "Highest bits/frame (block_size=8). Measured 0% BER up to CRF~23-28; "
            "degrades above that. Use when you control (or already know) the "
            "target quality and it's not extremely aggressive."
        ),
    ),
    "balanced": Profile(
        name="balanced",
        modulation_type=1,
        block_size=8,
        margin=32.0,
        fec_redundancy=0.20,
        interleave_depth=24,
        description="Good capacity, measured 0% BER through CRF32 in local channel tests.",
    ),
    "youtube-safe": Profile(
        name="youtube-safe",
        modulation_type=1,
        block_size=16,
        margin=32.0,
        fec_redundancy=0.25,
        interleave_depth=32,
        description=(
            "Default/recommended. Measured 0% BER through CRF32 on libx264/libx265/"
            "libsvtav1 at a FIXED resolution, and separately survived a 2x "
            "downscale-and-rescale at CRF23. NOT validated for a large downscale "
            "(e.g. 1080p delivered at 480p) STACKED with a harsh CRF simultaneously "
            "— that combined stress measured as unreliable in this profile; use "
            "maximum-reliability if you expect both at once. See docs/benchmarking.md."
        ),
    ),
    "maximum-reliability": Profile(
        name="maximum-reliability",
        modulation_type=1,
        block_size=32,
        margin=48.0,
        fec_redundancy=0.35,
        interleave_depth=48,
        description="Lowest capacity, measured 0% BER even at CRF40 in local channel tests. Use for irreplaceable data.",
    ),
}

DEFAULT_PROFILE = "youtube-safe"

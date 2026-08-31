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
    spread_factor: int = 1  # masked-luminance only, see modulation/masked_luminance.py; default is a no-op for other schemes


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
    "stego-safe": Profile(
        name="stego-safe",
        modulation_type=2,  # masked-luminance -- only actually selected together with --cover-video (or --modulation masked-luminance); see build_payload_modulation.
        block_size=16,
        margin=32.0,
        fec_redundancy=0.25,
        interleave_depth=32,
        description=(
            "For --cover-video mode. Same block_size/FEC envelope as youtube-safe, but "
            "margin is a CEILING scaled down per-block by local contrast (docs/architecture.md) "
            "instead of a flat push. Measured 0% BER through youtube-medium and youtube-low "
            "(fixed 480p resolution) across three synthetic cover textures (flat/detailed/motion "
            "-- see benchmark/testdata.py's generate_test_videos). Honest caveat: capacity requires "
            "every block of every frame to carry payload, so full imperceptibility is fundamentally "
            "in tension with capacity parity -- measured cover-vs-encoded SSIM against these test "
            "clips was ~0.65-0.68 (meaningfully less visible than the always-100%-synthetic carrier, "
            "but a real, measurable difference from the source, not true invisibility). Not yet "
            "validated against real (non-synthetic) footage or a real YouTube upload -- see "
            "docs/benchmarking.md."
        ),
    ),
    "stego-invisible": Profile(
        name="stego-invisible",
        modulation_type=2,  # masked-luminance, built-in spread_factor -- see build_payload_modulation.
        block_size=32,
        margin=48.0,
        fec_redundancy=0.35,
        interleave_depth=48,
        spread_factor=8,
        description=(
            "For --cover-video mode, prioritizing invisibility over capacity (the opposite trade-off "
            "from stego-safe): same block_size/FEC envelope as maximum-reliability, plus a built-in "
            "spread_factor=8 (spends 8 raw blocks per logical bit at 1/8 the push each -- see "
            "docs/architecture.md's 'spread-spectrum mode'). Needs a MUCH longer cover video for the "
            "same payload than stego-safe (roughly 8x more frames). Measured: cover-vs-encoded SSIM "
            "0.83 (vs. stego-safe's ~0.65), reliable through youtube-medium at a fixed 480p resolution. "
            "spread_factor=16 measured marginally better (SSIM 0.832 vs 0.827) but sits right at the "
            "reliability cliff (32 is measurably unreliable) -- confirmed flaky under real system load "
            "(a full test-suite run failed once at spread_factor=16 where isolated runs always passed, "
            "consistent with libx264's multi-threaded encode not being perfectly deterministic). 8 was "
            "chosen instead for real safety margin at essentially the same measured invisibility. "
            "Honest ceiling either way: pushing spread_factor higher does NOT improve SSIM further "
            "(measured flat at ~0.83 through spread_factor=64) -- every block of every frame carries "
            "payload regardless of spread_factor, which is the structural ceiling this profile runs "
            "into. Not yet validated against real (non-synthetic) footage or a real YouTube upload -- "
            "see docs/benchmarking.md."
        ),
    ),
}

DEFAULT_PROFILE = "youtube-safe"

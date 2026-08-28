"""Local channel simulator: stands in for "upload to YouTube, then download
with yt-dlp" during development, so the modulation/FEC parameters can be
iterated on without needing a real upload for every test (spec section 5).

IMPORTANT — these profiles are NOT verified YouTube encoding parameters.
Nobody outside YouTube has its exact current transcoding ladder (codec
choice, CRF/bitrate, preset, keyframe interval — all of it changes over
time and by content). The CRF values here are deliberately-labeled
*approximations* chosen to span "clearly fine" to "aggressive" compression
with standard open-source encoders, so the benchmark suite has a meaningful
range to sweep. Treat a profile passing here as "survives a channel at least
this harsh," not "confirmed to survive YouTube" — only an actual
encode -> upload -> yt-dlp download -> decode round trip proves that. See
docs/youtube-channel.md.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Optional

from videostore.video.io import FFMPEG


@dataclass(frozen=True)
class ChannelProfile:
    name: str
    codec: str = "libx264"
    crf: int = 23
    preset: str = "medium"
    scale: Optional[tuple[int, int]] = None  # (width, height) or None = unchanged
    fps: Optional[int] = None  # target fps or None = unchanged
    pix_fmt: str = "yuv420p"
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    generations: int = 1  # re-encode this many times back-to-back (multi-generation loss)
    description: str = ""


CHANNEL_PROFILES: dict[str, ChannelProfile] = {
    "lossless": ChannelProfile(
        name="lossless",
        codec="libx264",
        crf=0,
        preset="veryfast",
        description="Control/sanity channel: near-lossless H.264. Use to isolate pipeline bugs from channel-survival issues.",
    ),
    "youtube-low": ChannelProfile(
        name="youtube-low",
        codec="libx264",
        crf=32,
        preset="fast",
        scale=(854, 480),
        description="Approximates a heavily bitrate-constrained/low-quality delivery rendition. UNVERIFIED against real YouTube.",
    ),
    "youtube-medium": ChannelProfile(
        name="youtube-medium",
        codec="libx264",
        crf=26,
        preset="medium",
        scale=(1280, 720),
        description="Approximates a mid-quality 720p delivery rendition. UNVERIFIED against real YouTube.",
    ),
    "youtube-high": ChannelProfile(
        name="youtube-high",
        codec="libx264",
        crf=20,
        preset="medium",
        scale=(1920, 1080),
        description="Approximates a high-quality 1080p delivery rendition. UNVERIFIED against real YouTube.",
    ),
    "youtube-1080p": ChannelProfile(
        name="youtube-1080p",
        codec="libx265",
        crf=24,
        preset="medium",
        scale=(1920, 1080),
        description="Approximates a modern H.265-ladder 1080p rendition some platforms serve. UNVERIFIED.",
    ),
    "youtube-1440p": ChannelProfile(
        name="youtube-1440p",
        codec="libx265",
        crf=26,
        preset="medium",
        scale=(2560, 1440),
        description="Approximates a 1440p delivery rendition. UNVERIFIED.",
    ),
    "youtube-4k": ChannelProfile(
        name="youtube-4k",
        codec="libsvtav1",
        crf=32,
        preset="8",
        scale=(3840, 2160),
        description="Approximates an AV1-ladder 4K rendition (many platforms prefer AV1/VP9 at this tier). UNVERIFIED.",
    ),
    "double-transcode": ChannelProfile(
        name="double-transcode",
        codec="libx264",
        crf=26,
        preset="medium",
        scale=(1280, 720),
        generations=2,
        description="Two back-to-back re-encodes — approximates 'uploaded a re-upload of a re-upload' or a platform re-transcoding its own already-transcoded copy.",
    ),
}


def apply_channel(src_path: str, dst_path: str, profile: ChannelProfile) -> None:
    current_src = src_path
    for gen in range(profile.generations):
        out = dst_path if gen == profile.generations - 1 else f"{dst_path}.gen{gen}.mp4"
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", current_src]
        vf = []
        if profile.scale:
            vf.append(f"scale={profile.scale[0]}:{profile.scale[1]}:flags=lanczos")
        if profile.fps:
            vf.append(f"fps={profile.fps}")
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-an", "-c:v", profile.codec, "-crf", str(profile.crf), "-preset", profile.preset, "-pix_fmt", profile.pix_fmt]
        cmd += list(profile.extra_args)
        cmd += [out]
        subprocess.run(cmd, check=True, capture_output=True)
        current_src = out
    if current_src != dst_path:
        import shutil

        shutil.move(current_src, dst_path)

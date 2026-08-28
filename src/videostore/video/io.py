"""Raw frame <-> video file I/O via ffmpeg subprocesses (we deliberately do not
reinvent a video codec — spec section 47). Payload data only ever lives in the
Y (luma) plane; U/V are filled with flat mid-gray. This sidesteps 4:2:0 chroma
subsampling entirely rather than trying to survive it (spec section 3 lists
chroma-subsampling robustness as a requirement — the simplest way to satisfy
it is to never rely on chroma in the first place).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass
class VideoInfo:
    width: int
    height: int
    fps_num: int
    fps_den: int
    nb_frames: Optional[int]
    codec_name: str

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den


def probe_video(path: str) -> VideoInfo:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,nb_frames,codec_name",
        "-of",
        "json",
        path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)["streams"][0]
    fps_num, fps_den = (int(x) for x in data["r_frame_rate"].split("/"))
    nb_frames = int(data["nb_frames"]) if data.get("nb_frames", "N/A").isdigit() else None
    return VideoInfo(
        width=int(data["width"]),
        height=int(data["height"]),
        fps_num=fps_num,
        fps_den=fps_den,
        nb_frames=nb_frames,
        codec_name=data.get("codec_name", "unknown"),
    )


def encode_video(
    frames: Iterator[np.ndarray],
    output_path: str,
    width: int,
    height: int,
    fps: int,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    extra_args: Optional[list[str]] = None,
    pix_fmt_out: str = "yuv420p",
) -> None:
    """`frames` yields (height, width) uint8 luma planes, one per video frame."""
    cmd = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        codec,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        pix_fmt_out,
        *(extra_args or []),
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    chroma_h, chroma_w = height // 2, width // 2
    chroma_plane = np.full((chroma_h, chroma_w), 128, dtype=np.uint8)
    chroma_bytes = chroma_plane.tobytes()
    try:
        for y_plane in frames:
            assert y_plane.shape == (height, width), f"frame shape {y_plane.shape} != ({height},{width})"
            proc.stdin.write(y_plane.astype(np.uint8).tobytes())
            proc.stdin.write(chroma_bytes)
            proc.stdin.write(chroma_bytes)
    finally:
        proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg encode failed (exit {ret}): {stderr.decode(errors='replace')}")


def decode_video_luma(path: str, expected_width: Optional[int] = None, expected_height: Optional[int] = None) -> Iterator[np.ndarray]:
    """Yields (height, width) uint8 luma planes decoded from `path`. If
    expected_width/height are given and differ from the file's actual
    resolution, ffmpeg rescales to the expected size (see docs/protocol.md,
    "resolution normalization") so downstream block-grid math doesn't need to
    know about arbitrary YouTube-served resolutions."""
    info = probe_video(path)
    width = expected_width or info.width
    height = expected_height or info.height

    cmd = [FFMPEG, "-loglevel", "error", "-i", path]
    if (info.width, info.height) != (width, height):
        cmd += ["-vf", f"scale={width}:{height}:flags=lanczos"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-an", "-"]

    frame_bytes = width * height + 2 * ((width // 2) * (height // 2))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            y_plane = np.frombuffer(buf, dtype=np.uint8, count=width * height).reshape(height, width)
            yield y_plane.copy()
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        ret = proc.wait()
        if ret not in (0, None) and ret != 0:
            # ffmpeg often exits nonzero on pipes closed early by the consumer; only
            # surface it if we didn't get any frames at all.
            pass

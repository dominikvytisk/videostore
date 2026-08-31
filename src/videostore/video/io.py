"""Raw frame <-> video file I/O via ffmpeg subprocesses (we deliberately do not
reinvent a video codec — spec section 47). In the default (synthetic-cover)
mode, payload data only ever lives in the Y (luma) plane; U/V are filled with
flat mid-gray. This sidesteps 4:2:0 chroma subsampling entirely rather than
trying to survive it (spec section 3 lists chroma-subsampling robustness as a
requirement — the simplest way to satisfy it is to never rely on chroma in the
first place).

Cover-video mode (`encode_video_yuv420` / `extract_cover_yuv420` /
`read_cover_frames_looped`) is the exception: there, U/V carry the real cover
footage's color so the output doesn't look grayscale, and only Y carries the
payload.
"""
from __future__ import annotations

import json
import os
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


def _ffmpeg_encode_cmd(
    width: int,
    height: int,
    fps: int,
    codec: str,
    crf: int,
    preset: str,
    extra_args: Optional[list[str]],
    pix_fmt_out: str,
    output_path: str,
) -> list[str]:
    return [
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
    """`frames` yields (height, width) uint8 luma planes, one per video frame.
    Chroma is always flat mid-gray (see module docstring)."""
    cmd = _ffmpeg_encode_cmd(width, height, fps, codec, crf, preset, extra_args, pix_fmt_out, output_path)
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


def encode_video_yuv420(
    frames: Iterator[tuple[np.ndarray, np.ndarray, np.ndarray]],
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
    """Like `encode_video`, but `frames` yields real (Y, U, V) uint8 planes —
    Y at (height, width), U/V at (height//2, width//2) — instead of luma-only.
    Used by cover-video mode so the output carries the cover's real color
    instead of `encode_video`'s flat mid-gray chroma."""
    cmd = _ffmpeg_encode_cmd(width, height, fps, codec, crf, preset, extra_args, pix_fmt_out, output_path)
    chroma_h, chroma_w = height // 2, width // 2
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for y_plane, u_plane, v_plane in frames:
            assert y_plane.shape == (height, width), f"Y shape {y_plane.shape} != ({height},{width})"
            assert u_plane.shape == (chroma_h, chroma_w), f"U shape {u_plane.shape} != ({chroma_h},{chroma_w})"
            assert v_plane.shape == (chroma_h, chroma_w), f"V shape {v_plane.shape} != ({chroma_h},{chroma_w})"
            proc.stdin.write(y_plane.astype(np.uint8).tobytes())
            proc.stdin.write(u_plane.astype(np.uint8).tobytes())
            proc.stdin.write(v_plane.astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg encode failed (exit {ret}): {stderr.decode(errors='replace')}")


def yuv420_frame_bytes(width: int, height: int) -> int:
    chroma_h, chroma_w = height // 2, width // 2
    return width * height + 2 * (chroma_w * chroma_h)


def extract_cover_yuv420(path: str, width: int, height: int, fps: int, out_raw_path: str, max_frames: Optional[int] = None) -> int:
    """Decodes the cover video at `path` to raw planar yuv420p at the target
    (width, height, fps), writing it to `out_raw_path`. Rescales/resamples via
    ffmpeg if the source doesn't already match. Returns the number of whole
    frames written.

    `max_frames`, when given, caps decoding via ffmpeg's own `-frames:v`
    instead of decoding the entire source and discarding the rest -- a raw
    yuv420p frame at 1080p is ~3.1MB, so a cover video that's minutes long
    can be tens of GB of raw data if decoded in full, almost always far more
    than the payload actually needs (`encoder/pipeline.py` passes the exact
    `total_frames` the encode requires here)."""
    cmd = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-i",
        path,
        "-vf",
        f"scale={width}:{height}:flags=lanczos,fps={fps}",
    ]
    if max_frames is not None:
        cmd += ["-frames:v", str(max_frames)]
    cmd += [
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-an",
        out_raw_path,
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg cover extraction failed (exit {proc.returncode}): {proc.stderr.decode(errors='replace')}")
    frame_bytes = yuv420_frame_bytes(width, height)
    return os.path.getsize(out_raw_path) // frame_bytes


def read_cover_frames_looped(
    raw_path: str,
    width: int,
    height: int,
    source_frame_count: int,
    total_frames_needed: int,
) -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Yields (Y, U, V) uint8 planes read from a raw planar yuv420p file
    written by `extract_cover_yuv420`, one frame at a time (bounded memory).
    Cycles back to frame 0 (looping the cover video) if `total_frames_needed`
    exceeds `source_frame_count`."""
    if source_frame_count <= 0:
        raise ValueError("cover video has zero usable frames at the target resolution/fps")
    y_size = width * height
    chroma_h, chroma_w = height // 2, width // 2
    c_size = chroma_w * chroma_h
    frame_bytes = y_size + 2 * c_size
    with open(raw_path, "rb") as f:
        for i in range(total_frames_needed):
            src_idx = i % source_frame_count
            f.seek(src_idx * frame_bytes)
            buf = f.read(frame_bytes)
            if len(buf) < frame_bytes:
                raise RuntimeError(f"cover raw frame file truncated at frame {src_idx}")
            y = np.frombuffer(buf, dtype=np.uint8, count=y_size).reshape(height, width).copy()
            u = np.frombuffer(buf, dtype=np.uint8, count=c_size, offset=y_size).reshape(chroma_h, chroma_w).copy()
            v = np.frombuffer(buf, dtype=np.uint8, count=c_size, offset=y_size + c_size).reshape(chroma_h, chroma_w).copy()
            yield y, u, v


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

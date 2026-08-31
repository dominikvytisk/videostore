"""Cover-video ingestion I/O: extraction to raw yuv420p, looped frame
reading, and chroma-preserving encode (see docs/architecture.md, cover-video
mode)."""
import os
import subprocess

import numpy as np
import pytest

from videostore.video.io import (
    FFMPEG,
    encode_video_yuv420,
    extract_cover_yuv420,
    read_cover_frames_looped,
)


def _make_lavfi_clip(path, width=64, height=64, fps=10, duration=1, source="testsrc2"):
    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"{source}=size={width}x{height}:rate={fps}:duration={duration}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", str(path),
        ],
        check=True,
    )


def test_extract_and_read_cover_frames_shapes(tmp_path):
    clip = tmp_path / "cover.mp4"
    _make_lavfi_clip(clip, width=64, height=64, fps=10, duration=1)

    raw_path = str(tmp_path / "cover_raw.yuv")
    n = extract_cover_yuv420(str(clip), 64, 64, 10, raw_path)
    assert n >= 8  # ~10 frames at 1s/10fps; allow a little ffmpeg rounding slack

    frames = list(read_cover_frames_looped(raw_path, 64, 64, n, n))
    assert len(frames) == n
    for y, u, v in frames:
        assert y.shape == (64, 64)
        assert u.shape == (32, 32)
        assert v.shape == (32, 32)
        assert y.dtype == np.uint8


def test_extract_cover_yuv420_max_frames_caps_decoding(tmp_path):
    """Regression: extracting a long cover video for a short payload must not
    decode the whole thing to raw yuv420p -- that's tens of GB for a
    multi-minute clip at 1080p and can fill the disk (see
    docs/troubleshooting.md). max_frames should bound the raw output size
    regardless of the source's actual length."""
    clip = tmp_path / "cover.mp4"
    _make_lavfi_clip(clip, width=64, height=64, fps=10, duration=2)  # ~20 frames available

    raw_path = str(tmp_path / "cover_raw.yuv")
    n = extract_cover_yuv420(str(clip), 64, 64, 10, raw_path, max_frames=5)
    assert n == 5

    frame_bytes = 64 * 64 + 2 * (32 * 32)
    assert os.path.getsize(raw_path) == 5 * frame_bytes


def test_read_cover_frames_looped_wraps_around(tmp_path):
    clip = tmp_path / "cover.mp4"
    _make_lavfi_clip(clip, width=64, height=64, fps=10, duration=1)
    raw_path = str(tmp_path / "cover_raw.yuv")
    n = extract_cover_yuv420(str(clip), 64, 64, 10, raw_path)

    total_needed = n * 3 + 2
    frames = list(read_cover_frames_looped(raw_path, 64, 64, n, total_needed))
    assert len(frames) == total_needed
    y0, u0, v0 = frames[0]
    yn, un, vn = frames[n]  # first frame of the second loop must match frame 0
    assert np.array_equal(y0, yn)
    assert np.array_equal(u0, un)
    assert np.array_equal(v0, vn)


def test_read_cover_frames_looped_rejects_empty_source(tmp_path):
    raw_path = str(tmp_path / "empty.yuv")
    open(raw_path, "wb").close()
    with pytest.raises(ValueError):
        list(read_cover_frames_looped(raw_path, 64, 64, 0, 5))


def test_encode_video_yuv420_preserves_chroma(tmp_path):
    width, height, fps = 32, 32, 5
    chroma_h, chroma_w = height // 2, width // 2

    def frames():
        for i in range(5):
            y = np.full((height, width), 100, dtype=np.uint8)
            u = np.full((chroma_h, chroma_w), 40 + i * 10, dtype=np.uint8)
            v = np.full((chroma_h, chroma_w), 200 - i * 10, dtype=np.uint8)
            yield y, u, v

    out = str(tmp_path / "out.mp4")
    encode_video_yuv420(frames(), out, width, height, fps, crf=0, preset="ultrafast")

    raw_path = str(tmp_path / "readback.yuv")
    n = extract_cover_yuv420(out, width, height, fps, raw_path)
    assert n >= 1
    _, u, v = next(read_cover_frames_looped(raw_path, width, height, n, 1))
    # not flat mid-gray (128) -- proves chroma survived, unlike encode_video's
    # always-flat-128 chroma
    assert not np.all(u == 128)
    assert not np.all(v == 128)

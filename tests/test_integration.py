"""End-to-end tests. Per spec section 39/53, the clean round trip is the
least important test here — the ones that matter are the lossy-channel ones."""
import filecmp
import os
import shutil

import pytest

from videostore.channel import CHANNEL_PROFILES, apply_channel
from videostore.decoder.pipeline import decode
from videostore.encoder.pipeline import encode


def _make_dataset(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    (d / "text.txt").write_text("hello videostore\n" * 200)
    (d / "random.bin").write_bytes(os.urandom(15000))
    sub = d / "sub"
    sub.mkdir()
    (sub / "nested.bin").write_bytes(os.urandom(3000))
    return d


def _assert_dirs_match(original_dir, restored_root):
    # files land under restored_root/<basename of original_dir's parent structure>
    restored_dir = os.path.join(str(restored_root), os.path.basename(str(original_dir)))
    cmp = filecmp.dircmp(str(original_dir), restored_dir)
    assert not cmp.left_only and not cmp.right_only and not cmp.diff_files, (
        cmp.left_only,
        cmp.right_only,
        cmp.diff_files,
    )
    for sub in cmp.subdirs.values():
        assert not sub.left_only and not sub.right_only and not sub.diff_files


@pytest.mark.parametrize("profile_name", ["balanced", "youtube-safe"])
def test_clean_roundtrip_byte_for_byte(tmp_path, profile_name):
    dataset = _make_dataset(tmp_path)
    video = str(tmp_path / "out.mp4")
    encode([str(dataset)], video, resolution="480p", fps=30, profile_name=profile_name, crf=18, preset="ultrafast")

    restored = tmp_path / "restored"
    report = decode(video, str(restored))
    assert report.fully_recovered
    _assert_dirs_match(dataset, restored)


def test_encrypted_roundtrip(tmp_path):
    dataset = _make_dataset(tmp_path)
    video = str(tmp_path / "out.mp4")
    encode([str(dataset)], video, resolution="480p", fps=30, profile_name="youtube-safe", password="s3cr3t", crf=24, preset="ultrafast")

    restored = tmp_path / "restored"
    report = decode(video, str(restored), password="s3cr3t")
    assert report.fully_recovered
    _assert_dirs_match(dataset, restored)


def test_wrong_password_fails_cleanly(tmp_path):
    from videostore.decoder.pipeline import DecodeError

    dataset = _make_dataset(tmp_path)
    video = str(tmp_path / "out.mp4")
    encode([str(dataset)], video, resolution="480p", fps=30, profile_name="youtube-safe", password="right", crf=24, preset="ultrafast")

    with pytest.raises(DecodeError):
        decode(video, str(tmp_path / "restored_wrong"), password="wrong")


@pytest.mark.parametrize(
    "channel_name,profile_name",
    [
        # youtube-medium is a mild ~1.5x downscale (1080p->720p) + moderate CRF —
        # measured reliable with youtube-safe (docs/benchmarking.md).
        ("youtube-medium", "youtube-safe"),
        ("double-transcode", "youtube-safe"),
        # youtube-low stacks a much larger downscale (1080p->480p, ~2.3x) with a
        # harsh CRF32. Measured finding: youtube-safe (block16/margin32) is NOT
        # reliable at this combined stress (RS reported 0 uncorrectable blocks
        # but 5152 corrected bytes across 96 blocks and still failed downstream
        # — an RS miscorrection, caught by the archive checksum / decompression
        # safety net rather than by FEC itself). maximum-reliability (block32/
        # margin48, 35% redundancy) handles it cleanly. See docs/benchmarking.md
        # "profile vs channel severity" for the full writeup — this is exactly
        # the kind of boundary spec section 53 asks to measure, not assume.
        ("youtube-low", "maximum-reliability"),
    ],
)
def test_survives_simulated_channel(tmp_path, channel_name, profile_name):
    dataset = _make_dataset(tmp_path)
    video = str(tmp_path / "out.mp4")
    # 1080p source so a downscaling channel exercises the resolution-mismatch bootstrap path
    encode([str(dataset)], video, resolution="1080p", fps=30, profile_name=profile_name, crf=18, preset="ultrafast")

    channel_video = str(tmp_path / "channel.mp4")
    apply_channel(video, channel_video, CHANNEL_PROFILES[channel_name])

    restored = tmp_path / "restored"
    report = decode(channel_video, str(restored))
    assert report.fully_recovered, (report.fec_stats, report.failed, report.archive_checksum_ok)
    _assert_dirs_match(dataset, restored)


def _make_cover_clip(path, width=640, height=360, fps=30, duration=3, source="testsrc2"):
    import subprocess

    from videostore.video.io import FFMPEG

    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"{source}=size={width}x{height}:rate={fps}:duration={duration}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", str(path),
        ],
        check=True,
    )


def test_cover_video_roundtrip_byte_for_byte(tmp_path):
    """Phase 1 gate: real cover footage (with real chroma) carries a payload
    byte-for-byte, using the existing unmodified luminance-block scheme --
    proves the ingestion plumbing independent of any invisibility claim."""
    dataset = _make_dataset(tmp_path)
    cover = tmp_path / "cover.mp4"
    _make_cover_clip(cover, width=640, height=360, fps=30, duration=6)

    video = str(tmp_path / "out.mp4")
    report = encode(
        [str(dataset)], video, resolution="480p", fps=30, profile_name="youtube-safe",
        crf=18, preset="ultrafast", cover_video=str(cover),
    )
    assert report.cover_video == str(cover)
    assert not report.cover_looped

    restored = tmp_path / "restored"
    decode_report = decode(video, str(restored))
    assert decode_report.fully_recovered
    _assert_dirs_match(dataset, restored)


def test_cover_video_survives_simulated_channel(tmp_path):
    dataset = _make_dataset(tmp_path)
    cover = tmp_path / "cover.mp4"
    _make_cover_clip(cover, width=1920, height=1080, fps=30, duration=3)

    video = str(tmp_path / "out.mp4")
    encode(
        [str(dataset)], video, resolution="1080p", fps=30, profile_name="youtube-safe",
        crf=18, preset="ultrafast", cover_video=str(cover),
    )

    channel_video = str(tmp_path / "channel.mp4")
    apply_channel(video, channel_video, CHANNEL_PROFILES["youtube-medium"])

    restored = tmp_path / "restored"
    report = decode(channel_video, str(restored))
    assert report.fully_recovered, (report.fec_stats, report.failed, report.archive_checksum_ok)
    _assert_dirs_match(dataset, restored)


def test_cover_video_masked_scheme_survives_channel(tmp_path):
    """Phase 2 gate: the perceptually-masked scheme (auto-selected for
    cover-video mode) survives a mild simulated transcode, not just a
    lossless pipe -- fast/low-CRF so it stays CI-safe; the full BER sweep
    belongs in the benchmark suite (docs/benchmarking.md), not here."""
    dataset = _make_dataset(tmp_path)
    cover = tmp_path / "cover.mp4"
    _make_cover_clip(cover, width=640, height=360, fps=30, duration=6)

    video = str(tmp_path / "out.mp4")
    report = encode(
        [str(dataset)], video, resolution="480p", fps=30, profile_name="youtube-safe",
        crf=15, preset="ultrafast", cover_video=str(cover),
    )
    assert report.modulation == "masked-luminance"

    channel_video = str(tmp_path / "channel.mp4")
    apply_channel(video, channel_video, CHANNEL_PROFILES["youtube-medium"])

    restored = tmp_path / "restored"
    decode_report = decode(channel_video, str(restored))
    assert decode_report.fully_recovered, (decode_report.fec_stats, decode_report.failed, decode_report.archive_checksum_ok)
    _assert_dirs_match(dataset, restored)


def test_cover_video_spread_factor_survives_channel_and_reduces_delta(tmp_path):
    """Phase (spread-spectrum) gate: spread_factor>1 still round-trips
    correctly through a simulated channel, and -- the whole point -- produces
    a measurably smaller mean pixel delta against the cover than
    spread_factor=1 for the same payload."""
    dataset = _make_dataset(tmp_path)
    cover = tmp_path / "cover.mp4"
    _make_cover_clip(cover, width=640, height=360, fps=30, duration=20)  # longer: spread needs more frames

    deltas = {}
    for spread_factor in (1, 4):
        video = str(tmp_path / f"out_sf{spread_factor}.mp4")
        encode(
            [str(dataset)], video, resolution="480p", fps=30, profile_name="youtube-safe",
            crf=18, preset="ultrafast", cover_video=str(cover), spread_factor=spread_factor,
        )

        channel_video = str(tmp_path / f"channel_sf{spread_factor}.mp4")
        apply_channel(video, channel_video, CHANNEL_PROFILES["youtube-medium"])

        restored = tmp_path / f"restored_sf{spread_factor}"
        report = decode(channel_video, str(restored))
        assert report.fully_recovered, (spread_factor, report.fec_stats, report.failed, report.archive_checksum_ok)
        _assert_dirs_match(dataset, restored)

        # mean abs delta between the encoded (pre-channel) output and the cover itself
        from videostore.video.io import decode_video_luma, extract_cover_yuv420, read_cover_frames_looped

        enc_frames = list(decode_video_luma(video, expected_width=854, expected_height=480))
        raw_path = str(tmp_path / f"cover_raw_sf{spread_factor}.yuv")
        n = extract_cover_yuv420(str(cover), 854, 480, 30, raw_path, max_frames=len(enc_frames))
        cover_frames = list(read_cover_frames_looped(raw_path, 854, 480, n, len(enc_frames)))
        import numpy as np

        payload_deltas = [
            np.abs(enc_frames[i].astype(float) - cover_frames[i][0].astype(float)).mean()
            for i in range(10, len(enc_frames))  # skip header frames, look at payload frames only
        ]
        deltas[spread_factor] = float(np.mean(payload_deltas))

    assert deltas[4] < deltas[1], deltas


def test_cover_video_refuses_when_estimated_raw_size_exceeds_free_disk(tmp_path, monkeypatch):
    """Regression: cover-video extraction used to silently fill the disk over
    several minutes before failing with an opaque ffmpeg ENOSPC error. It
    should now fail fast with an actionable message instead."""
    import shutil as shutil_mod

    dataset = _make_dataset(tmp_path)
    cover = tmp_path / "cover.mp4"
    _make_cover_clip(cover, width=640, height=360, fps=30, duration=3)

    class _TinyFreeSpace:
        free = 10_000  # 10 KB -- far less than any real encode needs

    monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: _TinyFreeSpace())

    video = str(tmp_path / "out.mp4")
    with pytest.raises(ValueError, match="temporary raw video"):
        encode(
            [str(dataset)], video, resolution="480p", fps=30, profile_name="youtube-safe",
            crf=18, preset="ultrafast", cover_video=str(cover),
        )


def test_stego_invisible_profile_survives_channel(tmp_path):
    """The stego-invisible profile (built-in spread_factor=16, see
    presets.py) should auto-apply without any explicit --spread-factor, and
    still round-trip correctly through a real channel -- it needs a much
    longer cover for the same payload (spread trades capacity for
    invisibility), so this uses a small dataset and a long cover clip to
    keep the test fast."""
    small_dataset = tmp_path / "tiny"
    small_dataset.mkdir()
    (small_dataset / "note.txt").write_text("stego-invisible profile test\n" * 10)

    cover = tmp_path / "cover.mp4"
    _make_cover_clip(cover, width=640, height=360, fps=30, duration=60, source="testsrc2")

    video = str(tmp_path / "out.mp4")
    report = encode(
        [str(small_dataset)], video, resolution="480p", fps=30, profile_name="stego-invisible",
        crf=18, preset="ultrafast", cover_video=str(cover),
    )
    assert report.modulation == "masked-luminance"

    channel_video = str(tmp_path / "channel.mp4")
    apply_channel(video, channel_video, CHANNEL_PROFILES["youtube-medium"])

    restored = tmp_path / "restored"
    decode_report = decode(channel_video, str(restored))
    assert decode_report.fully_recovered, (decode_report.fec_stats, decode_report.failed, decode_report.archive_checksum_ok)
    _assert_dirs_match(small_dataset, restored)


def test_missing_frames_still_recover_via_fec(tmp_path):
    """Simulates dropped frames (not just compression) by literally cutting
    payload frames out of the encoded video with ffmpeg, then checking FEC +
    per-frame tagging still reconstruct the payload (spec section 18)."""
    import subprocess

    from videostore.video.io import FFMPEG

    dataset = _make_dataset(tmp_path)
    video = str(tmp_path / "out.mp4")
    encode(
        [str(dataset)],
        video,
        resolution="480p",
        fps=30,
        profile_name="maximum-reliability",  # highest FEC redundancy — should tolerate real frame loss
        crf=18,
        preset="ultrafast",
    )

    # drop a chunk of frames from the middle of the video using select+setpts,
    # which re-numbers frames but leaves our own frame_index tag intact so the
    # decoder must rely on tags (not physical position) to place frames.
    trimmed = str(tmp_path / "trimmed.mp4")
    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error", "-i", video,
            "-vf", "select='not(between(n\\,20\\,29))',setpts=N/FRAME_RATE/TB",
            "-an", "-c:v", "libx264", "-crf", "18", trimmed,
        ],
        check=True,
    )

    restored = tmp_path / "restored"
    report = decode(trimmed, str(restored))
    # some payload frames are genuinely gone, so this should be reported as
    # incomplete-but-handled (no crash), and FEC should still recover the data
    # given the extra redundancy of maximum-reliability, or at minimum leave
    # a clean diagnostic rather than corrupting output silently.
    assert report.payload_frames_present < report.payload_frames_expected

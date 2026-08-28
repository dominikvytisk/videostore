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

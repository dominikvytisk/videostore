"""Runs the actual encode -> [channel] -> decode -> compare matrix (spec
section 32). This is what answers "which modulation/FEC/profile survives
which channel" empirically instead of by assumption (spec section 39/53)."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np

from videostore.channel import CHANNEL_PROFILES, apply_channel
from videostore.decoder.pipeline import decode
from videostore.encoder.pipeline import _resolve_resolution, encode
from videostore.metrics import psnr, ssim
from videostore.presets import PROFILES, RESOLUTIONS
from videostore.video.io import decode_video_luma, extract_cover_yuv420, read_cover_frames_looped


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BenchmarkResult:
    test_file: str
    file_size: int
    profile: str
    modulation: str
    fec_redundancy: float
    channel: str
    resolution: str
    fps: int
    video_duration_seconds: float = 0.0
    video_file_size: int = 0
    physical_bitrate_mbps: float = 0.0
    effective_payload_bitrate_mbps: float = 0.0
    encode_time_seconds: float = 0.0
    channel_time_seconds: float = 0.0
    decode_time_seconds: float = 0.0
    success: bool = False
    sha256_match: bool = False
    blocks_total: int = 0
    blocks_uncorrectable: int = 0
    block_error_rate: float = 0.0
    psnr_db: Optional[float] = None
    ssim_index: Optional[float] = None
    cover_video: str = ""
    cover_looped: bool = False
    # "Invisibility" metrics: encoded (pre-channel) output vs. the cover
    # video itself -- distinct from psnr_db/ssim_index above, which measure
    # transcode damage (encoded vs. post-channel), not embedding visibility.
    # None whenever cover_video is empty (synthetic-carrier runs).
    cover_psnr_db: Optional[float] = None
    cover_ssim_index: Optional[float] = None
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _cover_invisibility_metrics(
    cover_video: str, encoded_path: str, width: int, height: int, fps: int, workdir: str
) -> tuple[Optional[float], Optional[float]]:
    """PSNR/SSIM between the encoded (pre-channel) output and the cover
    video itself, sampled at a handful of frames spread across the video
    (start/middle/end, so both header-frame and payload-frame visibility are
    represented -- they use different constants, see
    docs/architecture.md's cover-video section). Answers "does the output
    look different from the source", as opposed to psnr_db/ssim_index above
    (encoded vs. post-channel, which measures transcode damage instead)."""
    try:
        encoded_frames = list(decode_video_luma(encoded_path, expected_width=width, expected_height=height))
        if not encoded_frames:
            return None, None
        raw_path = os.path.join(workdir, "cover_ref_raw.yuv")
        # Cap decoding at the encoded output's own frame count -- otherwise a
        # long cover video gets decoded to raw yuv420p in full (tens of GB
        # for a multi-minute 1080p clip) even though only a few sampled
        # frames of it are ever read.
        n = extract_cover_yuv420(cover_video, width, height, fps, raw_path, max_frames=len(encoded_frames))
        if n <= 0:
            return None, None
        sample_idxs = sorted({0, len(encoded_frames) // 2, len(encoded_frames) - 1})
        cover_frames = list(read_cover_frames_looped(raw_path, width, height, n, len(encoded_frames)))
        psnrs, ssims = [], []
        for i in sample_idxs:
            cover_y, _, _ = cover_frames[i]
            psnrs.append(psnr(encoded_frames[i], cover_y))
            ssims.append(ssim(encoded_frames[i].astype(np.float64), cover_y.astype(np.float64)))
        return float(np.mean(psnrs)), float(np.mean(ssims))
    except Exception:
        return None, None


def run_one(
    input_path: str,
    *,
    profile_name: str,
    channel_name: str,
    resolution: str,
    fps: int,
    password: Optional[str] = None,
    modulation_override: Optional[str] = None,
    compute_quality: bool = True,
    workdir: Optional[str] = None,
    cover_video: Optional[str] = None,
    spread_factor: int = 1,
) -> BenchmarkResult:
    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="videostore_bench_")
    file_size = os.path.getsize(input_path) if os.path.isfile(input_path) else sum(
        os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(input_path) for f in fs
    )
    profile = PROFILES[profile_name]
    result = BenchmarkResult(
        test_file=os.path.basename(input_path),
        file_size=file_size,
        profile=profile_name,
        modulation=modulation_override or profile.modulation_type.__str__(),
        fec_redundancy=profile.fec_redundancy,
        channel=channel_name,
        resolution=resolution,
        fps=fps,
        cover_video=os.path.basename(cover_video) if cover_video else "",
    )
    try:
        encoded_path = os.path.join(workdir, "encoded.mp4")
        t0 = time.time()
        enc_report = encode(
            [input_path],
            encoded_path,
            resolution=resolution,
            fps=fps,
            spread_factor=spread_factor,
            profile_name=profile_name,
            compression="auto",
            password=password,
            codec="libx264",
            crf=18,
            preset="ultrafast",
            modulation_override=modulation_override,
            workdir=os.path.join(workdir, "enc_wd"),
            keep_temp=False,
            cover_video=cover_video,
        )
        result.encode_time_seconds = time.time() - t0
        result.modulation = enc_report.modulation
        result.video_duration_seconds = enc_report.duration_seconds
        result.video_file_size = os.path.getsize(encoded_path)
        result.cover_looped = enc_report.cover_looped
        if enc_report.duration_seconds > 0:
            result.physical_bitrate_mbps = (result.video_file_size * 8 / 1e6) / enc_report.duration_seconds
            result.effective_payload_bitrate_mbps = (enc_report.original_size * 8 / 1e6) / enc_report.duration_seconds

        if cover_video and compute_quality:
            width, height = _resolve_resolution(resolution)
            result.cover_psnr_db, result.cover_ssim_index = _cover_invisibility_metrics(
                cover_video, encoded_path, width, height, fps, workdir
            )

        channel_path = os.path.join(workdir, "channel.mp4")
        t0 = time.time()
        if channel_name == "lossless-passthrough":
            shutil.copyfile(encoded_path, channel_path)
        else:
            apply_channel(encoded_path, channel_path, CHANNEL_PROFILES[channel_name])
        result.channel_time_seconds = time.time() - t0

        if compute_quality:
            try:
                enc_frame = next(iter(decode_video_luma(encoded_path)))
                chan_frame = next(
                    iter(decode_video_luma(channel_path, expected_width=enc_frame.shape[1], expected_height=enc_frame.shape[0]))
                )
                result.psnr_db = psnr(enc_frame, chan_frame)
                result.ssim_index = ssim(enc_frame, chan_frame)
            except Exception:
                pass

        restored_dir = os.path.join(workdir, "restored")
        t0 = time.time()
        dec_report = decode(channel_path, restored_dir, password=password, workdir=os.path.join(workdir, "dec_wd"), keep_temp=False)
        result.decode_time_seconds = time.time() - t0
        result.blocks_total = dec_report.fec_stats.blocks_total
        result.blocks_uncorrectable = dec_report.fec_stats.blocks_uncorrectable
        result.block_error_rate = (
            dec_report.fec_stats.blocks_uncorrectable / dec_report.fec_stats.blocks_total
            if dec_report.fec_stats.blocks_total
            else 0.0
        )

        if os.path.isfile(input_path):
            restored_file = os.path.join(restored_dir, os.path.basename(input_path))
            match = os.path.isfile(restored_file) and _sha256_file(input_path) == _sha256_file(restored_file)
        else:
            match = dec_report.fully_recovered
        result.sha256_match = match
        result.success = match and dec_report.fully_recovered
    except Exception as exc:  # noqa: BLE001 — benchmark must never crash the sweep
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return result


def run_matrix(
    test_files: dict[str, str],
    *,
    profiles: list[str],
    channels: list[str],
    resolution: str = "480p",
    fps: int = 30,
    password: Optional[str] = None,
    modulations: Optional[list[Optional[str]]] = None,
    compute_quality: bool = True,
    progress=None,
    cover_video: Optional[str] = None,
) -> list[BenchmarkResult]:
    modulations = modulations or [None]
    results = []
    total = len(test_files) * len(profiles) * len(channels) * len(modulations)
    done = 0
    for name, path in test_files.items():
        for profile_name in profiles:
            for mod in modulations:
                for channel_name in channels:
                    r = run_one(
                        path,
                        profile_name=profile_name,
                        channel_name=channel_name,
                        resolution=resolution,
                        fps=fps,
                        password=password,
                        modulation_override=mod,
                        compute_quality=compute_quality,
                        cover_video=cover_video,
                    )
                    results.append(r)
                    done += 1
                    if progress:
                        progress(done, total, r)
    return results

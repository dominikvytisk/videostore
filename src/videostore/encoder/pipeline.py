"""Orchestrates the full encode pipeline: files -> archive -> compression ->
encryption -> FEC -> interleave -> frame layout -> video (spec section 1).

Every stage through interleaving streams through temp files with bounded
memory (archive build, compression, chunked AEAD, streaming Reed-Solomon,
memmap'd interleaving). The one deliberate exception is the final bit-split
of the interleaved payload into per-frame chunks, which currently loads the
whole interleaved blob as a bit array (8x its byte size) — a known v1 scaling
limit, see docs/troubleshooting.md, not a fundamental one (a bit-cursor
reader would remove it).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from videostore.archive import build_archive
from videostore.compression import Algorithm as CompAlgo
from videostore.compression.engine import compress_file, decide_auto
from videostore.container.format import Flags, GlobalHeader
from videostore.crypto import KdfParams, derive_key
from videostore.crypto.aead import Algorithm as AeadAlgo
from videostore.crypto.aead import encrypt_file, new_nonce_prefix
from videostore.fec import (
    encode_file as fec_encode_file,
    interleave_file,
    rs_config_for_redundancy,
)
from videostore.framing.layout import (
    HEADER_MODULATION_STEALTH,
    HEADER_MODULATION_SYNTHETIC,
    header_capacity_per_frame,
    payload_capacity_per_frame,
    frames_needed_for_payload,
    tile_header_bits,
)
from videostore.framing.regions import scatter_logical_bits
from videostore.modulation.dct_pair import DCTPairModulation
from videostore.modulation.luminance_block import LuminanceBlockModulation
from videostore.modulation.masked_luminance import PerceptualMaskedModulation
from videostore.presets import DEFAULT_PROFILE, PROFILES, RESOLUTIONS
from videostore.synchronization.frame_tag import (
    embed_tag,
    session_tag_from_id,
    TAG_MODULATION_STEALTH,
    TAG_MODULATION_SYNTHETIC,
)
from videostore.utils.bitstream import bytes_to_bits
from videostore.video.io import (
    encode_video,
    encode_video_yuv420,
    extract_cover_yuv420,
    read_cover_frames_looped,
    yuv420_frame_bytes,
)

ProgressCB = Optional[Callable[[str], None]]

HEADER_REPEAT_COUNT = 10
BASE_LUMA = 128.0


@dataclass
class EncodeReport:
    output_path: str
    original_size: int
    compressed_size: int
    encrypted_size: int
    fec_size: int
    interleaved_size: int
    total_frames: int
    header_frames: int
    payload_frames: int
    duration_seconds: float
    resolution: tuple[int, int]
    fps: int
    profile: str
    modulation: str
    codec: str
    encode_wall_seconds: float
    cover_video: Optional[str] = None
    cover_looped: bool = False


def _resolve_resolution(resolution: str) -> tuple[int, int]:
    if resolution in RESOLUTIONS:
        return RESOLUTIONS[resolution]
    if "x" in resolution:
        w, h = resolution.lower().split("x")
        return int(w), int(h)
    raise ValueError(f"unknown resolution: {resolution!r}")


def build_payload_modulation(profile_name: str, modulation_override: Optional[str], cover_video: bool = False, spread_factor: Optional[int] = None):
    profile = PROFILES[profile_name]
    if modulation_override == "dct-pair":
        return DCTPairModulation(block_size=8, margin=32.0)
    if modulation_override == "masked-luminance" or (modulation_override is None and cover_video):
        # cover-video mode defaults to the perceptually-masked scheme unless
        # the caller explicitly asked for something else -- the plain scheme
        # pushes a flat margin everywhere, which would defeat the point of
        # embedding into real footage (see modulation/masked_luminance.py).
        # spread_factor=None means "use the profile's own default" -- an
        # explicit value always overrides it (e.g. CLI --spread-factor).
        sf = spread_factor if spread_factor is not None else profile.spread_factor
        return PerceptualMaskedModulation(block_size=profile.block_size, margin=profile.margin, spread_factor=sf)
    return LuminanceBlockModulation(block_size=profile.block_size, margin=profile.margin)


def encode(
    inputs: list[str],
    output_path: str,
    *,
    resolution: str = "1080p",
    fps: int = 30,
    profile_name: str = DEFAULT_PROFILE,
    compression: str = "auto",
    password: Optional[str] = None,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    modulation_override: Optional[str] = None,
    header_repeat_count: int = HEADER_REPEAT_COUNT,
    workdir: Optional[str] = None,
    keep_temp: bool = False,
    progress: ProgressCB = None,
    cover_video: Optional[str] = None,
    spread_factor: Optional[int] = None,
) -> EncodeReport:
    t0 = time.time()
    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="videostore_enc_")
    os.makedirs(workdir, exist_ok=True)

    def report(stage: str) -> None:
        if progress:
            progress(stage)

    try:
        if profile_name not in PROFILES:
            raise ValueError(f"unknown profile: {profile_name!r} (choices: {list(PROFILES)})")
        width, height = _resolve_resolution(resolution)
        profile = PROFILES[profile_name]

        report("archive")
        archive_path = os.path.join(workdir, "archive.vsar")
        summary = build_archive(inputs, archive_path)
        original_size = os.path.getsize(archive_path)

        report("compress")
        comp_path = os.path.join(workdir, "compressed.bin")
        if compression == "auto":
            algo = decide_auto(archive_path)
        elif compression == "none":
            algo = CompAlgo.NONE
        elif compression == "zstd":
            algo = CompAlgo.ZSTD
        else:
            raise ValueError(f"unknown compression mode: {compression!r}")
        compressed_size = compress_file(archive_path, comp_path, algo)

        report("encrypt")
        enc_path = os.path.join(workdir, "encrypted.bin")
        if password:
            kdf_params = KdfParams.generate()
            key = derive_key(password, kdf_params)
            aead_algo = AeadAlgo.CHACHA20_POLY1305
            nonce_prefix = new_nonce_prefix()
        else:
            kdf_params = None
            key = b"\x00" * 32
            aead_algo = AeadAlgo.NONE
            nonce_prefix = b"\x00" * 8
        encrypted_size = encrypt_file(comp_path, enc_path, aead_algo, key, nonce_prefix)

        report("fec")
        rs_config = rs_config_for_redundancy(profile.fec_redundancy)
        fec_path = os.path.join(workdir, "fec.bin")
        fec_size = fec_encode_file(enc_path, fec_path, rs_config)

        report("interleave")
        il_path = os.path.join(workdir, "interleaved.bin")
        il_size = interleave_file(fec_path, il_path, rs_config.nsize, profile.interleave_depth)

        report("layout")
        payload_mod = build_payload_modulation(profile_name, modulation_override, cover_video=bool(cover_video), spread_factor=spread_factor)
        payload_bits_total = il_size * 8
        per_frame = payload_capacity_per_frame(width, height, payload_mod)
        payload_frames = frames_needed_for_payload(payload_bits_total, width, height, payload_mod)
        total_frames = header_repeat_count + payload_frames

        session_id = os.urandom(16)
        header = GlobalHeader(
            container_version=1,
            flags=(Flags.COMPRESSED if algo != CompAlgo.NONE else 0)
            | (Flags.ENCRYPTED if aead_algo != AeadAlgo.NONE else 0),
            session_id=session_id,
            original_size=original_size,
            compressed_size=compressed_size,
            encrypted_size=encrypted_size,
            compression_algo=int(algo),
            encryption_algo=int(aead_algo),
            kdf_algo=1 if password else 0,
            kdf_salt=kdf_params.salt if kdf_params else b"\x00" * 16,
            kdf_time_cost=kdf_params.time_cost if kdf_params else 0,
            kdf_memory_kib=kdf_params.memory_cost_kib if kdf_params else 0,
            kdf_parallelism=kdf_params.parallelism if kdf_params else 0,
            aead_nonce_prefix=nonce_prefix,
            fec_type=1,
            fec_nsize=rs_config.nsize,
            fec_nsym=rs_config.nsym,
            fec_interleave_depth=profile.interleave_depth,
            modulation_type=payload_mod.scheme_id,
            mod_margin=payload_mod.margin,
            mod_spread_factor=payload_mod.spread_factor,
            mod_symbol_bits=1,
            mod_block_size=payload_mod.block_size,
            frame_width=width,
            frame_height=height,
            fps_num=fps,
            fps_den=1,
            total_frames=total_frames,
            header_repeat_count=header_repeat_count,
            checkpoint_interval=0,
            archive_checksum=summary.archive_checksum,
        )
        header_bytes = header.pack()

        cover_looped = False
        raw_cover_path = None
        cover_frame_count = 0
        if cover_video:
            report("cover")
            raw_cover_path = os.path.join(workdir, "cover_raw.yuv")
            # Raw yuv420p at 1080p is ~3.1MB/frame; a high spread_factor and/or
            # a high resolution can need tens of thousands of frames of cover,
            # i.e. tens of GB of temporary raw data. Fail fast with a clear
            # message instead of silently filling the disk over several
            # minutes (see docs/troubleshooting.md).
            estimated_raw_bytes = total_frames * yuv420_frame_bytes(width, height)
            try:
                free_bytes = shutil.disk_usage(workdir).free
            except OSError:
                free_bytes = None
            if free_bytes is not None and estimated_raw_bytes > free_bytes * 0.9:
                raise ValueError(
                    f"cover-video extraction needs up to ~{estimated_raw_bytes / 1e9:.1f} GB of temporary "
                    f"raw video ({width}x{height}, {total_frames} frames) but only ~{free_bytes / 1e9:.1f} GB "
                    "is free on disk. Try a lower --resolution, a lower --spread-factor, a smaller payload, "
                    "or free up disk space."
                )
            cover_frame_count = extract_cover_yuv420(cover_video, width, height, fps, raw_cover_path, max_frames=total_frames)
            if cover_frame_count <= 0:
                raise ValueError("cover video has no usable frames at the target resolution/fps")
            cover_looped = total_frames > cover_frame_count

        report("modulate")
        cover_frames = None
        if raw_cover_path is not None:
            cover_frames = read_cover_frames_looped(raw_cover_path, width, height, cover_frame_count, total_frames)
        frames_iter = _generate_frames(
            header_bytes=header_bytes,
            header_repeat_count=header_repeat_count,
            payload_mod=payload_mod,
            payload_frames=payload_frames,
            per_frame=per_frame,
            il_path=il_path,
            width=width,
            height=height,
            session_id=session_id,
            cover_frames=cover_frames,
        )

        report("video-encode")
        if cover_video:
            encode_video_yuv420(frames_iter, output_path, width, height, fps, codec=codec, crf=crf, preset=preset)
        else:
            encode_video(frames_iter, output_path, width, height, fps, codec=codec, crf=crf, preset=preset)

        return EncodeReport(
            output_path=output_path,
            original_size=original_size,
            compressed_size=compressed_size,
            encrypted_size=encrypted_size,
            fec_size=fec_size,
            interleaved_size=il_size,
            total_frames=total_frames,
            header_frames=header_repeat_count,
            payload_frames=payload_frames,
            duration_seconds=total_frames / fps,
            resolution=(width, height),
            fps=fps,
            profile=profile_name,
            modulation=payload_mod.name,
            codec=codec,
            encode_wall_seconds=time.time() - t0,
            cover_video=cover_video,
            cover_looped=cover_looped,
        )
    finally:
        if own_workdir and not keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)


def _generate_frames(
    *,
    header_bytes: bytes,
    header_repeat_count: int,
    payload_mod,
    payload_frames: int,
    per_frame: int,
    il_path: str,
    width: int,
    height: int,
    session_id: bytes,
    cover_frames=None,
):
    """If `cover_frames` is given (an iterator of (Y, U, V) triples, one per
    total frame, header frames first), embeds into the cover's real Y content
    and yields (Y, U, V) triples. Otherwise embeds into a flat synthetic plane
    and yields Y-only planes (original behavior, unchanged)."""
    session_tag = session_tag_from_id(session_id)
    base_plane = np.full((height, width), BASE_LUMA, dtype=np.float64)

    def next_base():
        if cover_frames is None:
            return base_plane, None, None
        cover_y, cover_u, cover_v = next(cover_frames)
        return cover_y.astype(np.float64), cover_u, cover_v

    def finish(frame, cover_u, cover_v):
        y_out = np.clip(np.round(frame), 0, 255).astype(np.uint8)
        if cover_frames is None:
            return y_out
        return y_out, cover_u, cover_v

    # Cover-video mode uses the perceptually-masked tag/header constants
    # (much less visible against real footage); synthetic mode is byte-for-
    # byte unchanged from before this feature existed. The decoder learns
    # which pair was used by trying both against the tag region during
    # sync-scan (see decoder/pipeline.py::_sniff_resolution) -- these two
    # must always stay paired 1:1, never mixed.
    header_mod = HEADER_MODULATION_STEALTH if cover_frames is not None else HEADER_MODULATION_SYNTHETIC
    tag_mod = TAG_MODULATION_STEALTH if cover_frames is not None else TAG_MODULATION_SYNTHETIC

    hcap = header_capacity_per_frame(width, height)
    hcap_full = header_mod.capacity_blocks(width, height)
    header_stream = tile_header_bits(header_bytes, header_repeat_count * hcap)
    for i in range(header_repeat_count):
        chunk = header_stream[i * hcap : (i + 1) * hcap]
        full_bits = scatter_logical_bits(chunk, width, height, header_mod.block_size, hcap_full, spread_factor=header_mod.spread_factor)
        base, cover_u, cover_v = next_base()
        frame = header_mod.embed(base, full_bits)
        frame = embed_tag(frame, frame_index=i, session_tag=session_tag, frame_width=width, frame_height=height, modulation=tag_mod)
        yield finish(frame, cover_u, cover_v)

    pcap_full = payload_mod.capacity_blocks(width, height)
    with open(il_path, "rb") as f:
        payload_bits_all = bytes_to_bits(f.read())
    total_payload_bits = payload_frames * per_frame
    if len(payload_bits_all) < total_payload_bits:
        pad = np.zeros(total_payload_bits - len(payload_bits_all), dtype=np.uint8)
        payload_bits_all = np.concatenate([payload_bits_all, pad])

    for j in range(payload_frames):
        bits = payload_bits_all[j * per_frame : (j + 1) * per_frame]
        full_bits = scatter_logical_bits(bits, width, height, payload_mod.block_size, pcap_full, spread_factor=payload_mod.spread_factor)
        base, cover_u, cover_v = next_base()
        frame = payload_mod.embed(base, full_bits)
        frame_index = header_repeat_count + j
        frame = embed_tag(frame, frame_index=frame_index, session_tag=session_tag, frame_width=width, frame_height=height, modulation=tag_mod)
        yield finish(frame, cover_u, cover_v)

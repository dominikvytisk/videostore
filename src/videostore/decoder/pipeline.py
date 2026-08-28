"""Inverse of encoder/pipeline.py: video -> demodulation -> FEC -> decrypt ->
decompress -> archive extraction (spec section 1).

Bootstrapping order (see synchronization/frame_tag.py and framing/layout.py
for why each step is possible without already knowing the next one):
  1. ffprobe the file for its *actual* delivered resolution.
  2. Read frame tags at that actual resolution (tag region is a fixed pixel
     rect, so this needs no other knowledge) to learn the ENCODER's logical
     resolution and to filter out frames from an unrelated video.
  3. Re-decode (rescaling if the actual and logical resolutions differ) and
     accumulate HEADER_MODULATION-decoded bits from frames tagged 0, 1, 2...
     until the (strongly checksummed) GlobalHeader unpacks successfully.
  4. Everything else the header declares (FEC config, modulation, payload
     frame count) is now known, so payload frames can be demodulated and
     placed by their tag's frame_index — tolerating drops/dupes/reordering.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from videostore.archive import extract_archive, compute_archive_checksum
from videostore.archive.manifest import FileEntry
from videostore.compression import Algorithm as CompAlgo
from videostore.compression.engine import decompress_file
from videostore.container.format import GlobalHeader
from videostore.crypto import KdfParams, derive_key
from videostore.crypto.aead import Algorithm as AeadAlgo
from videostore.crypto.aead import DecryptResult, decrypt_file
from videostore.fec import RSConfig, decode_file, deinterleave_file, fec_output_size, padded_block_count, DecodeStats
from videostore.framing.layout import HEADER_MODULATION, HEADER_BITS, header_capacity_per_frame, payload_capacity_per_frame, recover_header_bits
from videostore.framing.regions import gather_logical_bits
from videostore.modulation import get_modulation  # noqa: F401 (import registers concrete schemes)
from videostore.presets import RESOLUTIONS
from videostore.synchronization.frame_tag import extract_tag
from videostore.utils.bitstream import bits_to_bytes
from videostore.video.io import decode_video_luma, probe_video

ProgressCB = Optional[Callable[[str], None]]

TAG_SCAN_FRAMES = 60  # how many initial physical frames to sniff resolution/session from
MAX_HEADER_SCAN_FRAMES = 128
ERASURE_CONFIDENCE_THRESHOLD = 0.3


class DecodeError(Exception):
    pass


@dataclass
class DecodeReport:
    header: GlobalHeader
    frames_scanned: int
    payload_frames_present: int
    payload_frames_expected: int
    fec_stats: DecodeStats
    decrypt: Optional[DecryptResult]
    archive_checksum_ok: bool
    recovered: list[FileEntry] = field(default_factory=list)
    failed: list[tuple] = field(default_factory=list)
    decode_wall_seconds: float = 0.0

    @property
    def fully_recovered(self) -> bool:
        return self.fec_stats.blocks_uncorrectable == 0 and not self.failed and self.archive_checksum_ok


def decode(
    video_path: str,
    output_dir: str,
    *,
    password: Optional[str] = None,
    workdir: Optional[str] = None,
    keep_temp: bool = False,
    progress: ProgressCB = None,
) -> DecodeReport:
    t0 = time.time()
    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="videostore_dec_")
    os.makedirs(workdir, exist_ok=True)

    def report(stage: str) -> None:
        if progress:
            progress(stage)

    try:
        report("probe")
        info = probe_video(video_path)

        report("sync-scan")
        declared = _sniff_resolution(video_path, info)
        if declared is None:
            raise DecodeError(
                "no valid VideoStore frame tags found in the first "
                f"{TAG_SCAN_FRAMES} frames — this doesn't look like a VideoStore video "
                "(or it's too badly damaged to locate)"
            )
        declared_w, declared_h, session_tag = declared

        report("full-decode")
        frame_planes: dict[int, np.ndarray] = {}
        frames_scanned = 0
        for phys_idx, plane in enumerate(decode_video_luma(video_path, expected_width=declared_w, expected_height=declared_h)):
            frames_scanned += 1
            tag = extract_tag(plane.astype(np.float64))
            if not tag.valid or tag.session_tag != session_tag:
                continue
            if tag.frame_index not in frame_planes:
                frame_planes[tag.frame_index] = plane

        report("header-recovery")
        header = _recover_header(frame_planes, declared_w, declared_h)
        if header is None:
            raise DecodeError(
                f"could not recover a valid header after scanning up to {MAX_HEADER_SCAN_FRAMES} frames "
                "— too much damage, or this isn't a VideoStore video"
            )

        report("demodulate-payload")
        w, h = header.frame_width, header.frame_height
        payload_mod = get_modulation(header.modulation_type, header.mod_block_size, header.mod_margin)
        per_frame = payload_capacity_per_frame(w, h, payload_mod)
        payload_frames_expected = header.total_frames - header.header_repeat_count
        total_bits = payload_frames_expected * per_frame

        payload_bits = np.zeros(total_bits, dtype=np.uint8)
        payload_conf = np.zeros(total_bits, dtype=np.float64)
        frames_present = 0
        for j in range(payload_frames_expected):
            fidx = header.header_repeat_count + j
            plane = frame_planes.get(fidx)
            if plane is None:
                continue
            frames_present += 1
            bits, conf = payload_mod.extract(plane.astype(np.float64))
            lb, lc = gather_logical_bits(bits, conf, w, h, payload_mod.block_size)
            payload_bits[j * per_frame : (j + 1) * per_frame] = lb
            payload_conf[j * per_frame : (j + 1) * per_frame] = lc

        report("fec-decode")
        rs_config = RSConfig(nsize=header.fec_nsize, nsym=header.fec_nsym)
        fec_stats, enc_path = _fec_decode(
            workdir, payload_bits, payload_conf, rs_config, header.fec_interleave_depth, header.encrypted_size
        )

        report("decrypt")
        comp_path = os.path.join(workdir, "comp_recovered.bin")
        decrypt_result: Optional[DecryptResult] = None
        if header.is_encrypted:
            if not password:
                raise DecodeError("this video is encrypted — pass --password")
            kdf_params = KdfParams(
                salt=header.kdf_salt,
                time_cost=header.kdf_time_cost,
                memory_cost_kib=header.kdf_memory_kib,
                parallelism=header.kdf_parallelism,
            )
            key = derive_key(password, kdf_params)
            decrypt_result = decrypt_file(
                enc_path, comp_path, AeadAlgo(header.encryption_algo), key, header.aead_nonce_prefix, best_effort=True
            )
            if decrypt_result.total_chunks > 0 and decrypt_result.failed_chunks == decrypt_result.total_chunks:
                raise DecodeError(
                    "decryption failed for every chunk — this usually means the "
                    "password is wrong (or the payload was not recovered at all by FEC)"
                )
        else:
            shutil.copyfile(enc_path, comp_path)

        report("decompress")
        archive_path = os.path.join(workdir, "archive_recovered.vsar")
        if header.is_compressed:
            try:
                decompress_file(comp_path, archive_path, CompAlgo(header.compression_algo))
            except Exception as exc:
                raise DecodeError(
                    f"decompression failed ({exc}) — the recovered payload is corrupted; "
                    "check fec_stats.blocks_uncorrectable and try a --profile with more redundancy next time"
                ) from exc
        else:
            shutil.copyfile(comp_path, archive_path)

        report("verify")
        archive_checksum_ok = False
        try:
            archive_checksum_ok = compute_archive_checksum(archive_path) == header.archive_checksum
        except (OSError, ValueError):
            pass

        report("extract")
        os.makedirs(output_dir, exist_ok=True)
        try:
            recovered, failed = extract_archive(archive_path, output_dir)
        except ValueError as exc:
            recovered, failed = [], [(None, str(exc))]

        return DecodeReport(
            header=header,
            frames_scanned=frames_scanned,
            payload_frames_present=frames_present,
            payload_frames_expected=payload_frames_expected,
            fec_stats=fec_stats,
            decrypt=decrypt_result,
            archive_checksum_ok=archive_checksum_ok,
            recovered=recovered,
            failed=failed,
            decode_wall_seconds=time.time() - t0,
        )
    finally:
        if own_workdir and not keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)


def _sniff_resolution(video_path: str, info) -> Optional[tuple[int, int, int]]:
    """Find the encoder's logical (frame_width, frame_height) and session_tag
    from frame tags. The tag lives at a fixed *pixel* offset, so it's only
    directly readable without knowing the target resolution when the
    delivered video's actual resolution matches what was encoded — the
    common case (spec section 19 notes YouTube usually preserves the upload
    resolution for its best-quality rendition).

    When that fails (the channel rescaled the video — spec section 3 requires
    surviving this), there's no way to know the *arbitrary* original
    resolution from pixels alone without also transmitting it out-of-band.
    videostore only ever encodes at one of the resolutions in
    presets.RESOLUTIONS, so the practical fallback is to try rescaling to
    each of those candidates in turn (reusing the same ffmpeg lanczos
    rescale validated in docs/benchmarking.md to preserve the modulation)
    and see which one produces a valid tag. This covers every video encoded
    via the documented --resolution presets; a custom "WxH" resolution that
    also gets rescaled by the channel is a known gap (see
    docs/troubleshooting.md).
    """
    candidates = [(info.width, info.height)] + [
        rh for rh in RESOLUTIONS.values() if rh != (info.width, info.height)
    ]
    for w, h in candidates:
        votes: Counter = Counter()
        for i, plane in enumerate(decode_video_luma(video_path, expected_width=w, expected_height=h)):
            if i >= TAG_SCAN_FRAMES:
                break
            tag = extract_tag(plane.astype(np.float64))
            if tag.valid and tag.frame_width == w and tag.frame_height == h:
                votes[(tag.frame_width, tag.frame_height, tag.session_tag)] += 1
        if votes:
            return votes.most_common(1)[0][0]
    return None


def _recover_header(frame_planes: dict[int, np.ndarray], width: int, height: int) -> Optional[GlobalHeader]:
    hcap_full = HEADER_MODULATION.capacity_blocks(width, height)
    all_bits: list[np.ndarray] = []
    all_conf: list[np.ndarray] = []
    for k in range(MAX_HEADER_SCAN_FRAMES):
        plane = frame_planes.get(k)
        if plane is None:
            continue
        bits, conf = HEADER_MODULATION.extract(plane.astype(np.float64))
        lb, lc = gather_logical_bits(bits, conf, width, height, HEADER_MODULATION.block_size)
        all_bits.append(lb)
        all_conf.append(lc)
        total = sum(len(b) for b in all_bits)
        if total < HEADER_BITS:
            continue
        result = recover_header_bits(np.concatenate(all_bits), np.concatenate(all_conf))
        if result.crc_ok:
            return GlobalHeader.unpack(result.header_bytes)
    return None


def _fec_decode(
    workdir: str,
    payload_bits: np.ndarray,
    payload_conf: np.ndarray,
    rs_config: RSConfig,
    interleave_depth: int,
    encrypted_size: int,
) -> tuple[DecodeStats, str]:
    fec_size = fec_output_size(encrypted_size, rs_config)
    n_fec_blocks = fec_size // rs_config.nsize
    il_size = padded_block_count(n_fec_blocks, interleave_depth) * rs_config.nsize
    il_bits_needed = il_size * 8

    if len(payload_bits) < il_bits_needed:
        pad = np.zeros(il_bits_needed - len(payload_bits), dtype=np.uint8)
        payload_bits = np.concatenate([payload_bits, pad])
        payload_conf = np.concatenate([payload_conf, np.zeros(len(pad), dtype=np.float64)])

    il_bytes = bits_to_bytes(payload_bits[:il_bits_needed])[:il_size]
    byte_conf = payload_conf[:il_bits_needed].reshape(-1, 8).min(axis=1)[: len(il_bytes)]
    mask_bytes = (byte_conf < ERASURE_CONFIDENCE_THRESHOLD).astype(np.uint8).tobytes()

    il_path = os.path.join(workdir, "il_recovered.bin")
    mask_path = os.path.join(workdir, "il_mask.bin")
    with open(il_path, "wb") as f:
        f.write(il_bytes)
    with open(mask_path, "wb") as f:
        f.write(mask_bytes)

    fec_path = os.path.join(workdir, "fec_recovered.bin")
    fec_mask_path = os.path.join(workdir, "fec_mask.bin")
    deinterleave_file(il_path, fec_path, rs_config.nsize, interleave_depth)
    deinterleave_file(mask_path, fec_mask_path, rs_config.nsize, interleave_depth)

    enc_path = os.path.join(workdir, "enc_recovered.bin")
    stats = decode_file(fec_path, enc_path, rs_config, erasure_mask_path=fec_mask_path)

    trimmed_path = os.path.join(workdir, "enc_recovered_trimmed.bin")
    with open(enc_path, "rb") as src, open(trimmed_path, "wb") as dst:
        dst.write(src.read(encrypted_size))
    return stats, trimmed_path

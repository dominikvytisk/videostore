from __future__ import annotations

import getpass
import json
import os
import sys
import time

import click

from videostore.archive import build_archive, extract_archive, list_archive
from videostore.compression.engine import decide_auto, Algorithm as CompAlgo
from videostore.container.format import GlobalHeader
from videostore.decoder.pipeline import DecodeError, decode as _decode
from videostore.encoder.pipeline import EncodeReport, encode as _encode, build_payload_modulation, _resolve_resolution
from videostore.fec import rs_config_for_redundancy
from videostore.framing.layout import frames_needed_for_payload, payload_capacity_per_frame
from videostore.presets import DEFAULT_PROFILE, PROFILES
from videostore.synchronization.frame_tag import extract_tag
from videostore.utils import progress as ui
from videostore.video.io import decode_video_luma, probe_video


def _yt_dlp_download(url: str) -> str:
    import shutil as _shutil
    import subprocess
    import tempfile

    if not _shutil.which("yt-dlp"):
        raise click.ClickException("yt-dlp not found on PATH — install it (e.g. `brew install yt-dlp`) to use --youtube-url")
    out_dir = tempfile.mkdtemp(prefix="videostore_ytdlp_")
    out_template = os.path.join(out_dir, "download.%(ext)s")
    ui.step(f"Downloading {url} with yt-dlp (best available quality)...")
    cmd = ["yt-dlp", "-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4", "-o", out_template, url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise click.ClickException(f"yt-dlp failed:\n{result.stderr}")
    for f in os.listdir(out_dir):
        if f.startswith("download."):
            return os.path.join(out_dir, f)
    raise click.ClickException("yt-dlp reported success but no output file was found")


def _get_password(flag: bool) -> str | None:
    if not flag:
        return None
    pw = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if pw != confirm:
        raise click.ClickException("passwords did not match")
    return pw


@click.group()
@click.version_option()
def cli():
    """videostore — store arbitrary files inside a video that survives YouTube transcoding."""


# ---------------------------------------------------------------- encode ---
@cli.command()
@click.argument("inputs", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path())
@click.option("--resolution", default="1080p", show_default=True, help="Preset (480p/720p/1080p/1440p/2160p) or WxH.")
@click.option("--fps", default=30, show_default=True)
@click.option("--profile", "profile_name", default=DEFAULT_PROFILE, show_default=True, type=click.Choice(list(PROFILES)))
@click.option("--compression", default="auto", show_default=True, type=click.Choice(["auto", "none", "zstd"]))
@click.option("--encrypt", is_flag=True, help="Prompt for a password and encrypt the payload.")
@click.option("--password", default=None, help="Password (insecure on shared machines — prefer --encrypt to be prompted).")
@click.option("--codec", default="libx264", show_default=True)
@click.option("--crf", default=18, show_default=True)
@click.option("--preset", "x264_preset", default="medium", show_default=True)
@click.option("--modulation", "modulation_override", default=None, type=click.Choice(["luminance-block", "dct-pair"]))
def encode(inputs, output, resolution, fps, profile_name, compression, encrypt, password, codec, crf, x264_preset, modulation_override):
    """Encode INPUTS (files and/or directories) into a video at OUTPUT."""
    pw = password or _get_password(encrypt)
    t0 = time.time()

    def cb(stage: str) -> None:
        ui.info(f"  [{time.time()-t0:6.1f}s] {stage}")

    ui.step(f"Encoding {len(inputs)} input(s) -> {output}")
    try:
        report: EncodeReport = _encode(
            list(inputs),
            output,
            resolution=resolution,
            fps=fps,
            profile_name=profile_name,
            compression=compression,
            password=pw,
            codec=codec,
            crf=crf,
            preset=x264_preset,
            modulation_override=modulation_override,
            progress=cb,
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    ui.ok("Done.")
    mins, secs = divmod(int(report.duration_seconds), 60)
    ui.info(f"  Resolution:        {report.resolution[0]}x{report.resolution[1]} @ {report.fps}fps")
    ui.info(f"  Profile/modulation: {report.profile} / {report.modulation}")
    ui.info(f"  Frames:            {report.total_frames} ({report.header_frames} header + {report.payload_frames} payload)")
    ui.info(f"  Video duration:    {mins}:{secs:02d}")
    ui.info(f"  Original size:     {report.original_size:,} bytes")
    ui.info(f"  After compression: {report.compressed_size:,} bytes")
    ui.info(f"  After FEC:         {report.fec_size:,} bytes")
    ui.info(f"  Encode wall time:  {report.encode_wall_seconds:.1f}s")
    ui.info(f"  Output:            {output}")


# ---------------------------------------------------------------- decode ---
@cli.command()
@click.argument("video", required=False, type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path())
@click.option("--password", default=None)
@click.option("--ask-password", is_flag=True)
@click.option("--debug", is_flag=True, help="Print extra diagnostics.")
@click.option(
    "--youtube-url",
    default=None,
    help="Download the video with yt-dlp first (best available quality), then decode it. "
    "Never uploads anything and never needs credentials.",
)
def decode(video, output, password, ask_password, debug, youtube_url):
    """Decode VIDEO back into files under OUTPUT. Pass --youtube-url instead of
    VIDEO to download+decode directly (see docs/youtube-channel.md)."""
    if not video and not youtube_url:
        raise click.ClickException("pass either VIDEO or --youtube-url")
    if youtube_url:
        video = _yt_dlp_download(youtube_url)
    pw = password or (getpass.getpass("Password: ") if ask_password else None)
    t0 = time.time()

    def cb(stage: str) -> None:
        ui.info(f"  [{time.time()-t0:6.1f}s] {stage}")

    ui.step(f"Analyzing {video}...")
    try:
        report = _decode(video, output, password=pw, progress=cb)
    except DecodeError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"unexpected error: {exc}") from exc

    h = report.header
    ui.info(f"  Detected format:   VideoStore v{h.protocol_version}")
    ui.info(f"  Resolution:        {h.frame_width}x{h.frame_height} @ {h.fps_num/h.fps_den:g}fps")
    ui.info(f"  Frames:            {h.total_frames}")
    ui.info(f"  Payload frames:    {report.payload_frames_present}/{report.payload_frames_expected} present")
    ui.info(
        f"  FEC recovery:      {report.fec_stats.block_success_rate:.2%} "
        f"({report.fec_stats.blocks_uncorrectable} uncorrectable / {report.fec_stats.blocks_total} blocks)"
    )
    if report.decrypt:
        ui.info(f"  Decrypt:           {report.decrypt.total_chunks - report.decrypt.failed_chunks}/{report.decrypt.total_chunks} chunks OK")
    ui.info(f"  Archive checksum:  {'OK' if report.archive_checksum_ok else 'MISMATCH'}")

    if report.recovered:
        ui.ok("Successfully restored:")
        for e in report.recovered:
            ui.info(f"    {e.path}    {e.size:,} bytes")
    if report.failed:
        ui.error(f"{len(report.failed)} file(s) could not be recovered:")
        for entry, reason in report.failed:
            name = entry.path if entry is not None else "(archive)"
            ui.info(f"    {name}: {reason}")

    total = sum(e.size for e in report.recovered)
    ui.info(f"  Total recovered:   {total:,} bytes")
    if debug:
        ui.info(json.dumps({"header": h.__dict__ if hasattr(h, "__dict__") else str(h)}, default=str, indent=2))

    if not report.fully_recovered:
        sys.exit(1)


# --------------------------------------------------------------- inspect ---
@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--resolution", default="1080p", show_default=True)
@click.option("--fps", default=30, show_default=True)
@click.option("--profile", "profile_name", default=DEFAULT_PROFILE, show_default=True, type=click.Choice(list(PROFILES)))
@click.option("--encrypt", is_flag=True)
def inspect(path, resolution, fps, profile_name, encrypt):
    """Estimate encode outcome for PATH (files/dir), or show header info if
    PATH is a VideoStore-encoded video."""
    if os.path.isfile(path) and _looks_like_video(path):
        _inspect_video(path)
        return

    ui.step(f"Estimating encode of {path}")
    size = os.path.getsize(path) if os.path.isfile(path) else sum(
        os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(path) for f in fs
    )
    algo = decide_auto(path if os.path.isfile(path) else _first_file(path))
    compressed_estimate = int(size * (0.5 if algo == CompAlgo.ZSTD else 1.0))  # rough, see note below

    profile = PROFILES[profile_name]
    rs_config = rs_config_for_redundancy(profile.fec_redundancy)
    fec_estimate = int((compressed_estimate / rs_config.message_len) + 1) * rs_config.nsize
    w, h = _resolve_resolution(resolution)
    mod = build_payload_modulation(profile_name, None)
    per_frame = payload_capacity_per_frame(w, h, mod)
    payload_frames = frames_needed_for_payload(fec_estimate * 8, w, h, mod)
    total_frames = payload_frames + 10
    duration_s = total_frames / fps
    mbit_s = (size * 8 / 1e6) / duration_s if duration_s else 0

    ui.info(f"  Input:                 {size:,} bytes")
    ui.info(f"  Compression:           {'zstd (estimated ~2x, ESTIMATE ONLY)' if algo == CompAlgo.ZSTD else 'none (data looks incompressible)'}")
    ui.info(f"  Encryption:            {'ChaCha20-Poly1305' if encrypt else 'none'}")
    ui.info(f"  FEC redundancy:        {profile.fec_redundancy:.0%} ({profile_name})")
    ui.info(f"  Modulation:            {mod.name} (block={mod.block_size}, margin={mod.margin})")
    ui.info(f"  Resolution:            {w}x{h} @ {fps}fps")
    ui.info(f"  Estimated payload:     {mbit_s:.2f} Mbit/s (== original size / duration, NOT theoretical channel capacity)")
    ui.info(f"  Estimated frames:      {total_frames} ({payload_frames} payload + 10 header)")
    ui.info(f"  Estimated duration:    {int(duration_s)//60}:{int(duration_s)%60:02d}")
    ui.warn("All figures above are ESTIMATES based on presets.py profile parameters, not a real encode. Run `videostore benchmark` for measured numbers.")


def _looks_like_video(path: str) -> bool:
    return path.lower().endswith((".mp4", ".mkv", ".webm", ".mov"))


def _first_file(dir_path: str) -> str:
    for dp, _, fs in os.walk(dir_path):
        for f in fs:
            return os.path.join(dp, f)
    raise click.ClickException("empty directory")


def _inspect_video(path: str) -> None:
    ui.step(f"Inspecting {path}")
    info = probe_video(path)
    ui.info(f"  Actual delivered resolution: {info.width}x{info.height} @ {info.fps:g}fps, codec={info.codec_name}")
    frame0 = next(iter(decode_video_luma(path)), None)
    if frame0 is None:
        raise click.ClickException("could not decode any frames")
    import numpy as np

    tag = extract_tag(frame0.astype(np.float64))
    if not tag.valid:
        ui.warn("Frame 0's tag did not validate at native resolution (may have been rescaled by the channel).")
        ui.warn("Run `videostore decode` for the full resolution-recovery bootstrap; `inspect` only checks frame 0 directly.")
        return
    ui.info(f"  Encoder logical resolution: {tag.frame_width}x{tag.frame_height}")
    ui.info(f"  Session tag:                0x{tag.session_tag:04x}")
    ui.info(f"  Frame 0 tag confidence:     {tag.confidence:.3f}")


# ----------------------------------------------------------- test-channel --
@cli.command("test-channel")
@click.argument("video", type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path())
@click.option("--channel", "channel_name", required=True)
def test_channel(video, output, channel_name):
    """Run VIDEO through a local channel-simulation profile (see `videostore benchmark --list-channels`)."""
    from videostore.channel import CHANNEL_PROFILES, apply_channel

    if channel_name not in CHANNEL_PROFILES:
        raise click.ClickException(f"unknown channel {channel_name!r}; choices: {list(CHANNEL_PROFILES)}")
    ui.step(f"Simulating channel '{channel_name}' ({CHANNEL_PROFILES[channel_name].description})")
    apply_channel(video, output, CHANNEL_PROFILES[channel_name])
    ui.ok(f"Wrote {output}")


# ------------------------------------------------------------- benchmark ---
@cli.command()
@click.option("--profile", "profile_names", multiple=True, default=[DEFAULT_PROFILE])
@click.option("--channel", "channel_names", multiple=True, default=["lossless-passthrough", "youtube-medium"])
@click.option("--resolution", default="480p", show_default=True)
@click.option("--fps", default=30, show_default=True)
@click.option("--size", "size_bytes", default=200_000, show_default=True)
@click.option("-o", "--output-dir", default="./benchmark-results", show_default=True)
@click.option("--list-channels", is_flag=True)
def benchmark(profile_names, channel_names, resolution, fps, size_bytes, output_dir, list_channels):
    """Run the encode -> channel -> decode -> compare benchmark matrix."""
    from videostore.channel import CHANNEL_PROFILES

    if list_channels:
        for name, p in CHANNEL_PROFILES.items():
            ui.info(f"  {name}: {p.description}")
        return

    from videostore.benchmark import generate_test_files, run_matrix, write_json, write_csv, write_html

    os.makedirs(output_dir, exist_ok=True)
    testdata_dir = os.path.join(output_dir, "testdata")
    ui.step(f"Generating test corpus in {testdata_dir}")
    files = generate_test_files(testdata_dir, size_bytes)

    ui.step(f"Running matrix: {len(files)} files x {len(profile_names)} profile(s) x {len(channel_names)} channel(s)")

    def cb(done, total, r):
        status = "PASS" if r.success else "FAIL"
        ui.info(f"  [{done}/{total}] {r.test_file} / {r.profile} / {r.channel}: {status}")

    results = run_matrix(
        files,
        profiles=list(profile_names),
        channels=list(channel_names),
        resolution=resolution,
        fps=fps,
        progress=cb,
    )

    write_json(results, os.path.join(output_dir, "benchmark.json"))
    write_csv(results, os.path.join(output_dir, "benchmark.csv"))
    write_html(results, os.path.join(output_dir, "benchmark.html"))

    passed = sum(1 for r in results if r.success)
    ui.ok(f"{passed}/{len(results)} runs passed. Reports written to {output_dir}/benchmark.{{json,csv,html}}")


# ------------------------------------------------------------------ pack ---
@cli.command()
@click.argument("inputs", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path())
def pack(inputs, output):
    """Build a VSAR archive from INPUTS without the rest of the video pipeline."""
    summary = build_archive(list(inputs), output)
    ui.ok(f"Wrote {output}: {summary.file_count} file(s), {summary.total_size:,} bytes")


@cli.command()
@click.argument("archive", type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path())
def unpack(archive, output):
    """Extract a VSAR archive built with `pack`."""
    recovered, failed = extract_archive(archive, output)
    ui.ok(f"Recovered {len(recovered)} file(s)")
    for entry, reason in failed:
        ui.error(f"  {entry.path}: {reason}")


# -------------------------------------------------------------------- serve --
@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8420, show_default=True)
@click.option("--public", is_flag=True, help="Bind 0.0.0.0 instead of localhost-only. No authentication is implemented — only use this on a trusted network.")
def serve(host, port, public):
    """Launch the local web UI (encode/decode from a browser)."""
    import uvicorn

    from videostore.web.app import app as web_app

    bind_host = "0.0.0.0" if public else host
    if public:
        ui.warn("Binding to 0.0.0.0 — this exposes the UI (no authentication) to your network.")
    ui.step(f"videostore web UI: http://{host}:{port}")
    uvicorn.run(web_app, host=bind_host, port=port, log_level="warning")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

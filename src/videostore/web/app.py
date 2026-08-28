"""Local web UI: a thin HTTP layer over encoder/decoder.pipeline — no logic
lives here that isn't already in the CLI. Local-only tool: binds to
127.0.0.1 by default (see cli/main.py `serve` command), no authentication.
Session state (uploaded/output files) lives in per-request temp directories
under the system temp dir and is swept on a simple age-based schedule; this
is a local dev tool, not a multi-tenant service.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from videostore.channel import CHANNEL_PROFILES
from videostore.decoder.pipeline import DecodeError, decode as _decode
from videostore.encoder.pipeline import encode as _encode
from videostore.presets import DEFAULT_PROFILE, PROFILES, RESOLUTIONS
from videostore.utils.pathsafe import UnsafePathError, safe_extract_path

STATIC_DIR = Path(__file__).parent / "static"
SESSIONS_ROOT = Path(tempfile.gettempdir()) / "videostore_web_sessions"
SESSIONS_ROOT.mkdir(exist_ok=True)
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_MAX_AGE_SECONDS = 3600

app = FastAPI(title="videostore")


def _sweep_old_sessions() -> None:
    now = time.time()
    try:
        for entry in SESSIONS_ROOT.iterdir():
            try:
                if now - entry.stat().st_mtime > SESSION_MAX_AGE_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def _new_session_dir() -> Path:
    _sweep_old_sessions()
    sid = uuid.uuid4().hex
    d = SESSIONS_ROOT / sid
    d.mkdir(parents=True)
    return d


def _session_dir_or_404(session_id: str) -> Path:
    # session_id comes straight from the URL — validate the shape strictly
    # before joining it onto SESSIONS_ROOT (untrusted input; see security.md).
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(404, "session not found")
    d = SESSIONS_ROOT / session_id
    if not d.is_dir():
        raise HTTPException(404, "session not found or expired")
    return d


def _safe_rel_path(raw_name: str) -> Path:
    """Same rule as the archive layer: strip any leading path components an
    adversarial (or just careless) client might send, reject traversal."""
    parts = [p for p in raw_name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path(uuid.uuid4().hex)


def _top_level_inputs(inputs_dir: Path) -> list[str]:
    """`build_archive` (archive/pack.py) derives each entry's archive path
    relative to *the parent of whatever was passed in* — passing every
    individual uploaded file (e.g. via inputs_dir.rglob("*")) would flatten
    every subdirectory away (each file's archive path becomes just its
    basename). Passing inputs_dir's own top-level entries instead makes a
    top-level subfolder walk with its structure preserved (archive path
    relative to inputs_dir) while a top-level flat file stays flat — exactly
    matching what a user who dropped a mix of loose files and a folder would
    expect. See tests/test_web.py for the regression test that caught this."""
    return sorted(str(p) for p in inputs_dir.iterdir())


def _encode_report_dict(report) -> dict:
    return {
        "resolution": report.resolution,
        "fps": report.fps,
        "total_frames": report.total_frames,
        "header_frames": report.header_frames,
        "payload_frames": report.payload_frames,
        "duration_seconds": report.duration_seconds,
        "original_size": report.original_size,
        "compressed_size": report.compressed_size,
        "fec_size": report.fec_size,
        "profile": report.profile,
        "modulation": report.modulation,
        "encode_wall_seconds": report.encode_wall_seconds,
    }


def _decode_report_dict(report, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "fully_recovered": report.fully_recovered,
        "archive_checksum_ok": report.archive_checksum_ok,
        "payload_frames_present": report.payload_frames_present,
        "payload_frames_expected": report.payload_frames_expected,
        "resolution": [report.header.frame_width, report.header.frame_height],
        "fps": report.header.fps_num / report.header.fps_den,
        "fec_stats": {
            "blocks_total": report.fec_stats.blocks_total,
            "blocks_ok": report.fec_stats.blocks_ok,
            "blocks_uncorrectable": report.fec_stats.blocks_uncorrectable,
        },
        "recovered": [{"path": e.path, "size": e.size} for e in report.recovered],
        "failed": [{"path": (e.path if e else None), "reason": reason} for e, reason in report.failed],
    }


class ProgressBridge:
    """Lets a background *thread* (encode/decode run in a thread pool
    executor so they don't block the event loop) push progress events into
    an asyncio.Queue a websocket handler is concurrently draining. The
    pipelines' `progress` callback is synchronous and thread-local, so
    crossing back into the event loop needs call_soon_threadsafe."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()

    def emit(self, event: dict) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)


async def _pump_to_websocket(ws: WebSocket, bridge: ProgressBridge) -> dict:
    """Forwards bridge events to the websocket until a terminal (done/error)
    event; returns that terminal event."""
    while True:
        event = await bridge.queue.get()
        await ws.send_json(event)
        if event["type"] in ("done", "error"):
            return event


def _yt_dlp_download(url: str, dest_dir: Path, bridge: ProgressBridge) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found on PATH — install it (e.g. `brew install yt-dlp`)")
    bridge.emit({"type": "progress", "stage": "download (yt-dlp)"})
    out_template = str(dest_dir / "download.%(ext)s")
    cmd = ["yt-dlp", "-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4", "-o", out_template, url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr[-2000:]}")
    for f in dest_dir.iterdir():
        if f.name.startswith("download."):
            return f
    raise RuntimeError("yt-dlp reported success but produced no output file")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/config")
def config():
    return {
        "profiles": {
            name: {"description": p.description, "fec_redundancy": p.fec_redundancy}
            for name, p in PROFILES.items()
        },
        "default_profile": DEFAULT_PROFILE,
        "resolutions": list(RESOLUTIONS.keys()),
        "channels": {name: p.description for name, p in CHANNEL_PROFILES.items()},
    }


@app.post("/api/encode")
async def api_encode(
    files: list[UploadFile] = File(...),
    resolution: str = Form("1080p"),
    profile_name: str = Form(DEFAULT_PROFILE),
    compression: str = Form("auto"),
    password: Optional[str] = Form(None),
    codec: str = Form("libx264"),
    crf: int = Form(18),
    x264_preset: str = Form("medium"),
    modulation_override: Optional[str] = Form(None),
):
    if profile_name not in PROFILES:
        raise HTTPException(400, f"unknown profile {profile_name!r}")
    if not files:
        raise HTTPException(400, "no files uploaded")

    session_dir = _new_session_dir()
    inputs_dir = session_dir / "inputs"
    inputs_dir.mkdir()
    for f in files:
        dest = inputs_dir / _safe_rel_path(f.filename or uuid.uuid4().hex)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)

    output_path = session_dir / "output.mp4"
    try:
        report = _encode(
            _top_level_inputs(inputs_dir),
            str(output_path),
            resolution=resolution,
            fps=30,
            profile_name=profile_name,
            compression=compression,
            password=password or None,
            codec=codec,
            crf=crf,
            preset=x264_preset,
            modulation_override=modulation_override or None,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the browser as a normal error
        raise HTTPException(400, str(exc)) from exc

    headers = {"X-Videostore-Report": base64.b64encode(json.dumps(_encode_report_dict(report)).encode()).decode()}
    return FileResponse(str(output_path), media_type="video/mp4", filename="payload.mp4", headers=headers)


@app.post("/api/decode")
async def api_decode(
    video: UploadFile = File(...),
    password: Optional[str] = Form(None),
):
    session_dir = _new_session_dir()
    video_path = session_dir / "input_video.mp4"
    with open(video_path, "wb") as out:
        shutil.copyfileobj(video.file, out)

    restored_dir = session_dir / "restored"
    try:
        report = _decode(str(video_path), str(restored_dir), password=password or None)
    except DecodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"unexpected error: {exc}") from exc

    return JSONResponse(_decode_report_dict(report, session_dir.name))


@app.get("/api/decode/{session_id}/file/{file_path:path}")
def download_decoded_file(session_id: str, file_path: str):
    """Serves one recovered file directly, so the UI can offer a per-file
    download link instead of forcing a zip for every recovery — the zip
    endpoint below is kept as a "download everything at once" convenience."""
    session_dir = _session_dir_or_404(session_id)
    restored_dir = session_dir / "restored"
    if not restored_dir.is_dir():
        raise HTTPException(404, "no restored files for this session")
    try:
        # file_path is untrusted (comes straight from the URL) — reuse the
        # same traversal-safe resolution the archive extractor itself uses.
        target = Path(safe_extract_path(str(restored_dir), file_path))
    except UnsafePathError:
        raise HTTPException(400, "invalid file path")
    if not target.is_file():
        raise HTTPException(404, "file not found in this session")
    return FileResponse(str(target), media_type="application/octet-stream", filename=target.name)


@app.get("/api/decode/{session_id}/download")
def download_decoded(session_id: str):
    session_dir = _session_dir_or_404(session_id)
    restored_dir = session_dir / "restored"
    if not restored_dir.is_dir():
        raise HTTPException(404, "no restored files for this session")
    zip_path = session_dir / "restored.zip"
    if not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, filenames in os.walk(restored_dir):
                for name in filenames:
                    full = Path(root) / name
                    zf.write(full, full.relative_to(restored_dir))
    return FileResponse(str(zip_path), media_type="application/zip", filename="restored.zip")


# --------------------------------------------------------------------------
# "File explorer" flow: an upload/prepare step (fast, plain HTTP) followed by
# a websocket that streams the *actual* pipeline stage names as they run —
# archive/compress/encrypt/fec/interleave/layout/modulate/video-encode for
# encode, probe/sync-scan/.../extract for decode. No progress is faked; these
# are the same `progress` callback stages encoder/decoder.pipeline already
# report to the CLI (see cli/main.py).
# --------------------------------------------------------------------------


@app.post("/api/encode/prepare")
async def prepare_encode(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "no files uploaded")
    session_dir = _new_session_dir()
    inputs_dir = session_dir / "inputs"
    inputs_dir.mkdir()
    manifest = []
    for f in files:
        dest = inputs_dir / _safe_rel_path(f.filename or uuid.uuid4().hex)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        manifest.append({"path": str(dest.relative_to(inputs_dir)), "size": dest.stat().st_size})
    return {"session_id": session_dir.name, "files": manifest, "total_size": sum(m["size"] for m in manifest)}


@app.websocket("/ws/encode/{session_id}")
async def ws_encode(ws: WebSocket, session_id: str):
    await ws.accept()
    if not SESSION_ID_RE.match(session_id):
        await ws.send_json({"type": "error", "message": "invalid session id"})
        await ws.close()
        return
    session_dir = SESSIONS_ROOT / session_id
    inputs_dir = session_dir / "inputs"
    if not inputs_dir.is_dir():
        await ws.send_json({"type": "error", "message": "session not found or expired — re-upload your files"})
        await ws.close()
        return

    try:
        params = await ws.receive_json()
    except WebSocketDisconnect:
        return

    if params.get("profile_name", DEFAULT_PROFILE) not in PROFILES:
        await ws.send_json({"type": "error", "message": f"unknown profile {params.get('profile_name')!r}"})
        await ws.close()
        return

    input_paths = _top_level_inputs(inputs_dir)
    output_path = session_dir / "output.mp4"
    loop = asyncio.get_running_loop()
    bridge = ProgressBridge(loop)

    def run() -> None:
        try:
            report = _encode(
                input_paths,
                str(output_path),
                resolution=params.get("resolution", "1080p"),
                fps=int(params.get("fps", 30)),
                profile_name=params.get("profile_name", DEFAULT_PROFILE),
                compression=params.get("compression", "auto"),
                password=params.get("password") or None,
                codec=params.get("codec", "libx264"),
                crf=int(params.get("crf", 18)),
                preset=params.get("x264_preset", "medium"),
                modulation_override=params.get("modulation_override") or None,
                progress=lambda stage: bridge.emit({"type": "progress", "stage": stage}),
            )
            bridge.emit({"type": "done", "report": _encode_report_dict(report)})
        except Exception as exc:  # noqa: BLE001 — reported to the client, not a server crash
            bridge.emit({"type": "error", "message": str(exc)})

    task = loop.run_in_executor(None, run)
    try:
        await _pump_to_websocket(ws, bridge)
    except WebSocketDisconnect:
        pass
    finally:
        await task
        await ws.close()


@app.get("/api/encode/{session_id}/download")
def download_encoded(session_id: str):
    session_dir = _session_dir_or_404(session_id)
    output_path = session_dir / "output.mp4"
    if not output_path.is_file():
        raise HTTPException(404, "no encoded video for this session")
    return FileResponse(str(output_path), media_type="video/mp4", filename="payload.mp4")


@app.post("/api/decode/prepare")
async def prepare_decode(video: Optional[UploadFile] = File(None), youtube_url: Optional[str] = Form(None)):
    if not video and not youtube_url:
        raise HTTPException(400, "provide either a video file or a youtube_url")
    session_dir = _new_session_dir()
    if video:
        video_path = session_dir / "input_video.mp4"
        with open(video_path, "wb") as out:
            shutil.copyfileobj(video.file, out)
    return {"session_id": session_dir.name, "youtube_url": youtube_url}


@app.websocket("/ws/decode/{session_id}")
async def ws_decode(ws: WebSocket, session_id: str):
    await ws.accept()
    if not SESSION_ID_RE.match(session_id):
        await ws.send_json({"type": "error", "message": "invalid session id"})
        await ws.close()
        return
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.is_dir():
        await ws.send_json({"type": "error", "message": "session not found or expired"})
        await ws.close()
        return

    try:
        params = await ws.receive_json()
    except WebSocketDisconnect:
        return

    video_path = session_dir / "input_video.mp4"
    youtube_url = params.get("youtube_url")
    restored_dir = session_dir / "restored"
    loop = asyncio.get_running_loop()
    bridge = ProgressBridge(loop)

    def run() -> None:
        try:
            path = video_path
            if youtube_url:
                path = _yt_dlp_download(youtube_url, session_dir, bridge)
            elif not video_path.is_file():
                raise RuntimeError("no video uploaded and no youtube_url given")
            report = _decode(
                str(path),
                str(restored_dir),
                password=params.get("password") or None,
                progress=lambda stage: bridge.emit({"type": "progress", "stage": stage}),
            )
            bridge.emit({"type": "done", "report": _decode_report_dict(report, session_dir.name)})
        except DecodeError as exc:
            bridge.emit({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            bridge.emit({"type": "error", "message": f"unexpected error: {exc}"})

    task = loop.run_in_executor(None, run)
    try:
        await _pump_to_websocket(ws, bridge)
    except WebSocketDisconnect:
        pass
    finally:
        await task
        await ws.close()

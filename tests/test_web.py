"""Exercises the web UI's HTTP layer directly (no logic should live here that
isn't already covered by the CLI/pipeline tests — this just proves the
FastAPI plumbing doesn't corrupt anything end to end)."""
import base64
import io
import json
import zipfile

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from videostore.web.app import app

client = TestClient(app)


def test_index_serves_html():
    res = client.get("/")
    assert res.status_code == 200
    assert "videostore" in res.text.lower()


def test_config_lists_profiles_and_resolutions():
    res = client.get("/api/config")
    assert res.status_code == 200
    data = res.json()
    assert "youtube-safe" in data["profiles"]
    assert "1080p" in data["resolutions"]


def test_encode_then_decode_roundtrip_byte_for_byte():
    files = [
        ("files", ("hello.txt", io.BytesIO(b"hello from the web ui\n" * 50), "text/plain")),
        ("files", ("data.bin", io.BytesIO(bytes(range(256)) * 100), "application/octet-stream")),
    ]
    res = client.post(
        "/api/encode",
        files=files,
        data={
            "resolution": "480p",
            "profile_name": "youtube-safe",
            "compression": "auto",
            "codec": "libx264",
            "crf": "20",
            "x264_preset": "ultrafast",
        },
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "video/mp4"
    report = json.loads(base64.b64decode(res.headers["x-videostore-report"]))
    assert report["profile"] == "youtube-safe"
    video_bytes = res.content
    assert len(video_bytes) > 0

    res2 = client.post("/api/decode", files={"video": ("payload.mp4", io.BytesIO(video_bytes), "video/mp4")})
    assert res2.status_code == 200, res2.text
    decode_report = res2.json()
    assert decode_report["fully_recovered"] is True
    names = {f["path"] for f in decode_report["recovered"]}
    assert "hello.txt" in names
    assert "data.bin" in names

    session_id = decode_report["session_id"]
    res3 = client.get(f"/api/decode/{session_id}/download")
    assert res3.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(res3.content))
    assert zf.read("hello.txt") == b"hello from the web ui\n" * 50
    assert zf.read("data.bin") == bytes(range(256)) * 100


def test_encrypted_roundtrip_via_api():
    files = [("files", ("secret.txt", io.BytesIO(b"top secret payload\n" * 20), "text/plain"))]
    res = client.post(
        "/api/encode",
        files=files,
        data={
            "resolution": "480p",
            "profile_name": "youtube-safe",
            "compression": "auto",
            "codec": "libx264",
            "crf": "24",
            "x264_preset": "ultrafast",
            "password": "hunter2",
        },
    )
    assert res.status_code == 200
    video_bytes = res.content

    # wrong password -> clean error, not a crash
    res_wrong = client.post(
        "/api/decode",
        files={"video": ("payload.mp4", io.BytesIO(video_bytes), "video/mp4")},
        data={"password": "wrong"},
    )
    assert res_wrong.status_code == 400

    res_right = client.post(
        "/api/decode",
        files={"video": ("payload.mp4", io.BytesIO(video_bytes), "video/mp4")},
        data={"password": "hunter2"},
    )
    assert res_right.status_code == 200
    assert res_right.json()["fully_recovered"] is True


def test_decode_download_rejects_path_traversal_session_id():
    res = client.get("/api/decode/../../etc/download")
    assert res.status_code == 404


def test_decode_prepare_accepts_youtube_url_without_a_file():
    """Regression test: the "YouTube link" tab in the UI used to POST an
    empty form to /api/decode/prepare (the URL was only sent later, over the
    websocket), which always hit the "provide either a video file or a
    youtube_url" 400 — see index.html's decSubmit handler, which now also
    appends youtube_url to the prepare request when that mode is selected."""
    res = client.post("/api/decode/prepare", data={"youtube_url": "https://www.youtube.com/watch?v=bsPOQE9ycgE"})
    assert res.status_code == 200
    body = res.json()
    assert body["youtube_url"] == "https://www.youtube.com/watch?v=bsPOQE9ycgE"
    assert "session_id" in body


def test_decode_prepare_rejects_neither_file_nor_url():
    res = client.post("/api/decode/prepare", data={})
    assert res.status_code == 400


def test_per_file_download_serves_recovered_file_and_rejects_traversal():
    files = [("files", ("notes/todo.txt", io.BytesIO(b"buy milk\n" * 30), "text/plain"))]
    enc = client.post(
        "/api/encode",
        files=files,
        data={"resolution": "480p", "profile_name": "youtube-safe", "compression": "auto", "codec": "libx264", "crf": "20", "x264_preset": "ultrafast"},
    )
    assert enc.status_code == 200

    dec = client.post("/api/decode", files={"video": ("payload.mp4", io.BytesIO(enc.content), "video/mp4")})
    assert dec.status_code == 200
    report = dec.json()
    assert report["recovered"] == [{"path": "notes/todo.txt", "size": len(b"buy milk\n" * 30)}]
    session_id = report["session_id"]

    ok = client.get(f"/api/decode/{session_id}/file/notes/todo.txt")
    assert ok.status_code == 200
    assert ok.content == b"buy milk\n" * 30

    missing = client.get(f"/api/decode/{session_id}/file/nope.txt")
    assert missing.status_code == 404

    traversal = client.get(f"/api/decode/{session_id}/file/..%2F..%2Fetc%2Fpasswd")
    assert traversal.status_code in (400, 404)


def _drain_ws(ws):
    """Collect websocket messages until a terminal (done/error) event."""
    events = []
    while True:
        msg = ws.receive_json()
        events.append(msg)
        if msg["type"] in ("done", "error"):
            return events


def test_websocket_encode_decode_roundtrip_with_live_progress():
    """The prepare + websocket flow the file-explorer UI actually drives —
    checks real stage-by-stage progress events arrive in order, not just
    that the final result is correct."""
    prep = client.post(
        "/api/encode/prepare",
        files=[("files", ("sub/nested.txt", io.BytesIO(b"nested via explorer ui\n" * 40), "text/plain"))],
    )
    assert prep.status_code == 200
    session_id = prep.json()["session_id"]
    assert prep.json()["files"] == [{"path": "sub/nested.txt", "size": len(b"nested via explorer ui\n" * 40)}]

    with client.websocket_connect(f"/ws/encode/{session_id}") as ws:
        ws.send_json(
            {
                "resolution": "480p",
                "profile_name": "youtube-safe",
                "compression": "auto",
                "codec": "libx264",
                "crf": 20,
                "x264_preset": "ultrafast",
            }
        )
        events = _drain_ws(ws)

    assert events[-1]["type"] == "done"
    progress_stages = [e["stage"] for e in events if e["type"] == "progress"]
    assert progress_stages == [
        "archive", "compress", "encrypt", "fec", "interleave", "layout", "modulate", "video-encode",
    ]
    video_res = client.get(f"/api/encode/{session_id}/download")
    assert video_res.status_code == 200
    video_bytes = video_res.content

    dec_prep = client.post("/api/decode/prepare", files={"video": ("payload.mp4", io.BytesIO(video_bytes), "video/mp4")})
    assert dec_prep.status_code == 200
    dsid = dec_prep.json()["session_id"]

    with client.websocket_connect(f"/ws/decode/{dsid}") as ws:
        ws.send_json({})
        dec_events = _drain_ws(ws)

    assert dec_events[-1]["type"] == "done"
    report = dec_events[-1]["report"]
    assert report["fully_recovered"] is True
    assert report["recovered"] == [{"path": "sub/nested.txt", "size": len(b"nested via explorer ui\n" * 40)}]


def test_websocket_encode_with_cover_video_roundtrip():
    """Exercises the /api/encode/prepare cover_video upload + ws_encode
    cover_video plumbing end to end (Phase 1 gate for the web UI's carrier
    toggle)."""
    import subprocess

    from videostore.video.io import FFMPEG

    import tempfile

    payload = b"cover mode via the web ui\n" * 40
    with tempfile.NamedTemporaryFile(suffix=".mp4") as cover_f:
        subprocess.run(
            [
                FFMPEG, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", cover_f.name,
            ],
            check=True,
        )
        with open(cover_f.name, "rb") as cover_fh:
            prep = client.post(
                "/api/encode/prepare",
                files={
                    "files": ("payload.txt", io.BytesIO(payload), "text/plain"),
                    "cover_video": ("cover.mp4", cover_fh, "video/mp4"),
                },
            )
    assert prep.status_code == 200
    body = prep.json()
    assert body["has_cover_video"] is True
    session_id = body["session_id"]

    with client.websocket_connect(f"/ws/encode/{session_id}") as ws:
        ws.send_json({"resolution": "480p", "profile_name": "youtube-safe", "crf": 20, "x264_preset": "ultrafast"})
        events = _drain_ws(ws)

    assert events[-1]["type"] == "done"
    assert events[-1]["report"]["cover_video"] is True

    video_res = client.get(f"/api/encode/{session_id}/download")
    assert video_res.status_code == 200

    dec_prep = client.post("/api/decode/prepare", files={"video": ("payload.mp4", io.BytesIO(video_res.content), "video/mp4")})
    dsid = dec_prep.json()["session_id"]
    with client.websocket_connect(f"/ws/decode/{dsid}") as ws:
        ws.send_json({})
        dec_events = _drain_ws(ws)
    report = dec_events[-1]["report"]
    assert report["fully_recovered"] is True
    assert report["recovered"] == [{"path": "payload.txt", "size": len(payload)}]


def test_websocket_encode_rejects_unknown_session():
    with client.websocket_connect("/ws/encode/" + "0" * 32) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_websocket_encode_rejects_bad_session_id_shape():
    with client.websocket_connect("/ws/encode/not-a-valid-id") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"

# Web UI

`videostore serve` (needs `pip install -e ".[web]"`) starts a small FastAPI
app (`web/app.py`) serving a single static page (`web/static/index.html`,
vanilla HTML/CSS/JS, no build step, no external CDN dependency) at
`http://127.0.0.1:8420` by default.

**No logic lives in this layer.** Every endpoint is a thin wrapper calling
straight into `encoder.pipeline.encode` / `decoder.pipeline.decode` — the
same functions the CLI calls, with the same `progress` callback the CLI
prints stage names from. If encode/decode behaves differently through the
web UI than through the CLI, that's a bug in the web layer, not a different
code path to separately trust.

## UI

A drag-and-drop "file explorer" for Encode (accepts individual files *and*
whole folders — dropped folders are read recursively via the
`DataTransferItem.webkitGetAsEntry()` API, and there's also an explicit
"Add folder" button using `<input webkitdirectory>` for browsers/flows where
drag-and-drop of a directory doesn't fire), with a live per-stage pipeline
progress view and, on success, an inline `<video>` preview of the actual
encoded payload plus a download link. Decode accepts a dropped video file or
a pasted YouTube URL (downloaded locally via `yt-dlp`, never uploaded), shows
the same kind of live stage progress, then a file-explorer-style list of
recovered/failed entries and a `.zip` download.

## Endpoints

Two flows exist:

**Simple (single HTTP round trip, no live progress)** — kept for
programmatic/`curl` use and covered by the earliest web tests:
- `POST /api/encode` — multipart form (`files[]` + resolution/profile/
  compression/codec/crf/preset/modulation/password). Returns the `.mp4`
  directly as the response body, with an `X-Videostore-Report` header
  (base64-encoded JSON) carrying the same stats the CLI prints.
- `POST /api/decode` — multipart form (`video` + optional `password`).
  Returns a JSON report and a `session_id`.

**File-explorer flow (upload, then a websocket streams real progress)** —
what the UI actually drives:
- `POST /api/encode/prepare` — multipart form (`files[]`, each with its
  relative path as the multipart filename, e.g. `sub/nested.txt` for a
  dropped folder). Saves them into a fresh session and returns
  `{session_id, files: [...], total_size}`.
- `WS /ws/encode/{session_id}` — client sends one JSON message with the
  encode params; server runs `encode()` in a thread-pool executor (so it
  doesn't block the event loop) and streams `{"type":"progress","stage":
  "archive"|"compress"|"encrypt"|"fec"|"interleave"|"layout"|"modulate"|
  "video-encode"}` events — the literal stage names `encoder/pipeline.py`
  already reports — followed by `{"type":"done","report":{...}}` or
  `{"type":"error","message":...}`.
- `GET /api/encode/{session_id}/download` — the resulting `.mp4`.
- `POST /api/decode/prepare` — multipart `video` **or** a `youtube_url` form
  field (mutually exclusive with uploading a file). Returns `{session_id}`.
- `WS /ws/decode/{session_id}` — same pattern; if `youtube_url` was given,
  the first stage is `"download (yt-dlp)"` (runs the same download this
  project's CLI `--youtube-url` flag does — see
  [youtube-channel.md](youtube-channel.md) — never uploads anything), then
  the same stage names `decoder/pipeline.py` reports (`probe`, `sync-scan`,
  `full-decode`, `header-recovery`, `demodulate-payload`, `fec-decode`,
  `decrypt`, `decompress`, `verify`, `extract`), ending in `done`/`error`
  with the same report shape as `POST /api/decode`.
- `GET /api/decode/{session_id}/download` — zips and returns the recovered
  files for that session (shared by both flows).
- `GET /api/config` — profiles, resolutions, channel descriptions, for
  populating the UI's dropdowns from the same source of truth as the CLI
  (`presets.py`, `channel/simulator.py`).

Progress is never faked or interpolated client-side — each stepper update on
screen corresponds to one real websocket message from the pipeline actually
reaching that stage.

## A bug this design caught: folder structure preservation

`build_archive` (`archive/pack.py`) derives each entry's archive path
relative to *the parent of whatever path was passed in*. The first
implementation of `/api/encode/prepare` + `/ws/encode` passed every
individual uploaded file's full path to `encode()` (via `inputs_dir.rglob
("*")`), which flattened every subfolder away — a file uploaded as
`sub/nested.txt` came back out as just `nested.txt`. Fixed by passing
`inputs_dir`'s own *top-level* entries instead (`app.py::_top_level_inputs`):
a top-level file stays flat, a top-level folder gets walked with its
structure preserved, matching what a user who dragged in a mix of loose
files and a folder would expect. Caught by
`tests/test_web.py::test_websocket_encode_decode_roundtrip_with_live_progress`,
which specifically uploads a nested path — the earlier flat-file-only tests
didn't exercise this and passed anyway.

## Session handling

Uploaded/output files live under `$TMPDIR/videostore_web_sessions/<uuid4>/`,
one directory per request. There's no database and no session cookie — the
`session_id` is just that directory's name, and is strictly validated as a
32-hex-char UUID before being joined onto the sessions root
(`app.py::_session_dir_or_404`, and inline in the websocket handlers)
specifically so a crafted `session_id` can't path-traverse out of it — see
`tests/test_web.py::test_decode_download_rejects_path_traversal_session_id`
and `test_websocket_encode_rejects_bad_session_id_shape`. Sessions older than
an hour are swept (deleted) opportunistically on the next request; this is a
local single-user dev tool, not a service with SLAs on cleanup timing.

## Security notes

- **Binds to `127.0.0.1` by default.** `--public` binds `0.0.0.0`, which
  exposes the UI (and therefore file upload/download) to your network —
  there is **no authentication**, so only do this on a network you trust,
  and ideally never on a machine with a public IP.
- **No rate limiting, no upload size cap.** Fine for local single-user use;
  don't put this behind a public reverse proxy without adding both.
- Encode input filenames from the browser are sanitized the same way archive
  paths are (stripping `..`/absolute segments) before being joined onto the
  per-session temp directory — see `app.py::_safe_rel_path`.
- Passwords are sent as normal form/JSON fields over whatever transport
  you're using (plain HTTP/WS on localhost by default). If you bind
  `--public`, put a TLS-terminating reverse proxy in front of it yourself;
  this app doesn't do TLS.

## Testing

`tests/test_web.py` drives the FastAPI app directly via `TestClient` (no
real network socket, including its websocket support) and covers: both the
simple and the prepare+websocket encode→decode round trips (byte-for-byte,
including a nested folder path), the encrypted round trip with a
wrong-password rejection, and session-id validation (unknown session,
malformed session id, path-traversal attempt). A live-server smoke test
(`videostore serve` + `curl`, including a real `/ws/encode` round trip) was
also run manually during development; it's not part of the automated suite
since it needs a real bound port, but the `TestClient` websocket tests
exercise the identical code path.

# The YouTube channel: workflow and what's verified

## What this tool does and doesn't do

`videostore` never uploads anything and never needs YouTube credentials.
The intended workflow is manual on the upload side:

```bash
videostore encode ./my_files -o payload.mp4 --resolution 1080p --profile youtube-safe
# → upload payload.mp4 to YouTube yourself, however you normally would
# → wait for processing to finish
yt-dlp "https://youtube.com/watch?v=XXXXXXXX" -f "bestvideo+bestaudio/best" \
    --merge-output-format mp4 -o downloaded.mp4
videostore decode downloaded.mp4 -o ./restored
```

or let `videostore decode` invoke `yt-dlp` for you (still download-only):

```bash
videostore decode --youtube-url "https://youtube.com/watch?v=XXXXXXXX" -o ./restored
```

Always request the **best available video+audio** (`-f
"bestvideo+bestaudio/best"`) — a lower-quality rendition (YouTube offers
several) is a harsher channel than necessary. The decoder ignores the audio
track entirely; it's only present because `-f best` sometimes bundles one,
and some tooling behaves oddly with a video-only container.

## What's actually verified vs. simulated

**Not verified in this environment**: an actual upload → YouTube processing
→ `yt-dlp` download → decode round trip. That requires a YouTube account and
uploading, which this project deliberately doesn't automate and this
development environment doesn't have credentials for.

**What is verified**: real `ffmpeg` encode → local channel simulation →
decode round trips, across H.264/H.265/AV1, multiple CRFs, resolution
downscaling, and multi-generation re-encoding. See
[benchmarking.md](benchmarking.md) for the actual numbers and how to
reproduce them.

**The gap between the two**: YouTube's actual transcoding ladder (codec
choice, target bitrate vs. CRF, keyframe interval, exact scaler, any
additional filtering) is not public and changes over time. The channel
profiles in `channel/simulator.py` are labeled `UNVERIFIED` in their own
descriptions for exactly this reason — they're a reasonable *span* of
plausible severity (from "clearly fine" to "aggressive"), not a claim about
what YouTube specifically does. **If you run the real upload/download round
trip, that result is the one to trust over anything in this repo.**

## The channel simulator — `channel/simulator.py`

```python
from videostore.channel import CHANNEL_PROFILES, apply_channel
apply_channel("payload.mp4", "simulated.mp4", CHANNEL_PROFILES["youtube-medium"])
```

or via the CLI:

```bash
videostore test-channel payload.mp4 -o simulated.mp4 --channel youtube-medium
videostore benchmark --list-channels   # see all profiles + descriptions
```

Built-in profiles: `lossless` (control/sanity check), `youtube-low` (crf 32,
854x480), `youtube-medium` (crf 26, 1280x720), `youtube-high` (crf 20,
1920x1080), `youtube-1080p`/`youtube-1440p` (libx265), `youtube-4k`
(libsvtav1), `double-transcode` (two back-to-back re-encodes — approximates a
re-upload of a re-upload, or a platform re-transcoding its own already-
transcoded copy). Add a profile by constructing a `ChannelProfile` — every
parameter (codec, crf, preset, scale, fps, extra ffmpeg args, generations) is
explicit and documented, per the project's own rule against hardcoding
"official YouTube behavior" without verification.

## If you validate against real YouTube

Please do, and consider recording what you find: upload resolution vs.
delivered resolution, codec YouTube served back (`ffprobe -show_streams` on
the downloaded file tells you), and whether `videostore decode` succeeded —
ideally at more than one `--profile` setting. That's the one measurement this
repo is missing.

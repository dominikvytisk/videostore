# Development

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
brew install ffmpeg yt-dlp   # or platform equivalent; ffmpeg needs libx264/
                             # libx265/libsvtav1/libvmaf built in
pytest -q
```

## Layout and how to extend a layer

Each stage behind a narrow interface (spec's own closing requirement — a
future developer should be able to replace one without touching the others):

| Layer | Interface | To add a new implementation |
|---|---|---|
| Compression | `compress_file`/`decompress_file`(src, dst, Algorithm) | Add an `Algorithm` enum value + branch in `compression/engine.py` |
| Encryption | `encrypt_file`/`decrypt_file`(src, dst, Algorithm, key, nonce) | Add an `Algorithm` enum value + branch in `crypto/aead.py::_cipher` |
| FEC | `encode_file`/`decode_file`(src, dst, RSConfig-like object) | Implement the same signature in a new module under `fec/`; the pipelines only import from `fec/__init__.py` |
| Modulation | `ModulationScheme.capacity_blocks/embed/extract` | Subclass `modulation.base.ModulationScheme`, decorate with `@register` — it's picked up automatically by `get_modulation(scheme_id, ...)` |
| Video codec | ffmpeg CLI args in `video/io.py::encode_video` | Add a `codec=` value; anything ffmpeg's `-c:v` accepts works already, no code change needed for a new *codec name* |
| Channel profile | `ChannelProfile` dataclass | Add an entry to `channel.simulator.CHANNEL_PROFILES` |

## Adding a modulation scheme

```python
from videostore.modulation.base import ModulationScheme, register

@register
class MyScheme(ModulationScheme):
    name = "my-scheme"
    scheme_id = 2  # must be unique across MODULATIONS

    def capacity_blocks(self, width, height): ...
    def embed(self, plane, bits): ...          # plane: (H,W) float64 luma
    def extract(self, plane): ...              # -> (bits uint8, confidence float64 in [0,1])
```

Then benchmark it against the existing ones **before** wiring it into a
profile — see [benchmarking.md](benchmarking.md) for the exact experiment
that decided `luminance-block` over `dct-pair`. Don't add a profile entry
(`presets.py`) for an unmeasured scheme; that's how `youtube-safe`'s
docstring ended up citing exact numbers instead of vibes.

## Known follow-ups (deliberately out of scope for v1)

- **Bit-cursor streaming for the final frame-split stage**
  (`encoder/pipeline.py::_generate_frames`) — currently loads the whole
  interleaved payload as a bit array (8x its byte size) to split it across
  frames. Every earlier stage streams through temp files with bounded
  memory; this one doesn't yet. Fine for the payload sizes tested here (up
  to a few hundred MB); would need a persistent bit-cursor reader for
  multi-GB inputs.
- **Hardware encode** (NVENC/QuickSync/VideoToolbox) — `video/io.py` calls
  ffmpeg with software encoders only. Detecting and opting into a hardware
  encoder is a small addition (`-c:v h264_videotoolbox` etc. on this
  machine) but wasn't validated for how it affects channel survival, and the
  spec explicitly warns not to trade robustness for encode speed without
  checking.
- **LDPC/BCH/fountain codes, true spread-spectrum spreading** — see
  [architecture.md](architecture.md), "what wasn't built."
- **`--optimize-for` auto-tuning** (spec section 23) — the CLI's `inspect`
  command estimates outcomes from `presets.py`, but there's no search-based
  optimizer picking resolution/FPS/modulation/FEC from an objective. Given
  fixed measured profiles, this would be a reasonably small addition — walk
  the profile/resolution grid and pick the cheapest one that meets a
  reliability floor from benchmark data.

## Running the test suite

```bash
pytest -q                          # everything, ~20s
pytest tests/test_integration.py   # the end-to-end / channel tests specifically
```

The integration tests are the ones that matter most (spec's own
"failure-driven development" principle) — they run real `ffmpeg` encodes and
channel simulations, not mocks. `test_missing_frames_still_recover_via_fec`
actually cuts frames out of an encoded video and checks the tag-based
frame-index system (not physical position) is what the decoder relies on.

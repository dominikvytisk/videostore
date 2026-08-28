# Architecture

## The channel model

```
files → archive → compress → encrypt → FEC → interleave → modulate → video
                                                                          |
                                                                     YouTube
                                                                          |
                                                                     yt-dlp
                                                                          v
files ← archive ← decompress ← decrypt ← FEC ← deinterleave ← demodulate ← video
```

YouTube is treated as a noisy channel: `encoder → codec/transcoder → decoder`.
The decoder never assumes pixel equality with what was encoded. Every stage
above is designed around that assumption specifically:

- **Compression before encryption, never after** — encrypted data is
  indistinguishable from random and doesn't compress; compressing after
  encryption just burns CPU.
- **FEC after encryption** — so a single bit flip in transit can't be
  amplified by a compression format's structure before error correction gets
  a chance at it, and so the same FEC layer protects the header, ciphertext,
  and everything else uniformly.
- **Interleaving after FEC** — video-transcoding damage is bursty (a bad
  macroblock, a rescale seam, an entire frame lost), and Reed-Solomon only
  guarantees correcting `nsym/2` byte errors *per 255-byte block*.
  Interleaving spreads a physical burst across many RS blocks so each block
  sees a small, correctable fraction of it instead of the whole thing (see
  `fec/interleave.py`'s docstring, and the interleave-depth-vs-burst-size
  worked example below).
- **Modulation last, chosen by measurement, not assumption** — see below.

## The modulation experiment

The spec this was built against assumed DCT mid-band coefficient relationships
(a Koch-Zhao-style scheme: bias `coeff[u1,v1] - coeff[u2,v2]` to encode a bit,
decode by sign, use magnitude as confidence) would be the strongest channel,
with pixel-domain block averaging expected to be "destroyed by lossy
compression." A real `ffmpeg libx264` round trip disproved that:

| modulation | block | margin | CRF 18 | CRF 23 | CRF 28 | CRF 32 | CRF 36/40 |
|---|---|---|---|---|---|---|---|
| dct-pair, coeffs (2,3)/(3,2) | 8 | 32 | 0.00% | 0.49% | 6.90% | 49.9% | — |
| dct-pair, near-DC (1,0)/(0,1) | 8 | 32 | — | 0.03% | 2.74% | 26.5% | 45.5% |
| luminance-block (top/bottom avg) | 8 | 32 | 0.00% | 0.00% | 0.01% | — | 22.8%(40) |
| luminance-block | 16 | 32 | 0.00% | 0.00% | 0.00% | 0.00% | 0.40%(40) |
| luminance-block | 32 | 16 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00%(40) |

(Bit-error rate on a flat-gray 640x480 cover, 5 frames per point. Full sweep —
including trying every near-DC coefficient pair, H.265/libsvtav1, a 2x
resolution downscale-and-rescale, and the full CRF range — is reproducible via
`videostore benchmark`; see [benchmarking.md](benchmarking.md) for the exact
commands and more data points.)

**Why DCT-pair fails**: this system designs its own cover frames, so there's
no "existing complex footage" to hide in — the modulation *is* the entire
visual content of the frame. A codec's rate-distortion loop, deblocking
filter, and adaptive block-size decisions are all specifically tuned to
smooth out the kind of small, sharp, isolated coefficient perturbations
DCT-pair modulation creates — they look exactly like the compression
artifacts those filters exist to remove. And critically: **the codec's own
internal 4x4/8x8/16x16 transform partitioning does not have to align with the
8x8 grid this code assumes** — the encoder is free to re-partition, and
usually does on a flat source. Averaging over a large spatial region has no
such alignment dependency and is robust to exactly the operations (quantize,
deblock, re-encode) that kill fine coefficient structure.

**Conclusion / decision**: `luminance-block` is the default and only
profile-backed modulation. `dct-pair` remains available
(`--modulation dct-pair`) as the experimental option the interface was built
to support swapping in — useful for anyone extending this toward *encoder-
level* coefficient embedding (patching an encoder's actual quantized
coefficients, not recomputing an independent DCT in pixel space), which this
project did not attempt and which is the likely reason literature on
DCT-domain robust watermarking usually assumes cooperation with, or
knowledge of, the specific encoder — not a blind pixel-domain guess at where
its transform blocks will land.

## Profile vs. channel severity (a second measured finding)

Stacking two robustness stressors is harder than either alone. Encoding at
1080p and then having the channel both **downscale to 480p and apply CRF 32**
simultaneously is a harder combined stress than either "CRF 32 at a fixed
resolution" or "2x downscale at CRF 23" tested individually — and
`youtube-safe` (block16/margin32, 25% FEC redundancy), which is 0%-BER-clean
on each of those individually, is not reliable on the combination:

```
fec_stats: blocks_ok=96/96, blocks_uncorrectable=0, bytes_corrected=5152
fully_recovered: False   (archive checksum mismatch downstream)
```

96 blocks "succeeding" while correcting an average of ~54 bytes/block each
(RS(255, ~191) has ~32-byte blind correction capacity) is Reed-Solomon
**miscorrecting** — converging on a self-consistent but wrong codeword rather
than failing loudly. This is a known RS failure mode once the true error
count is pushed well past capacity. It was caught here by the archive-level
BLAKE3 checksum and the decompressor rejecting invalid zstd framing — not by
FEC itself reporting failure — which is exactly why videostore checks archive
integrity independently of "did FEC say it corrected everything" (see
[security.md](security.md), "defense in depth").

`maximum-reliability` (block32/margin48, 35% redundancy) handles the same
combined stress cleanly (`bytes_corrected=0`). **Decision**: if you expect
both a resolution downgrade *and* heavy compression simultaneously (e.g. you
don't control what rendition YouTube will actually serve, and your source
isn't obviously already low-quality), use `maximum-reliability`, not
`youtube-safe`. This is now stated in `presets.py`'s profile descriptions and
covered by `tests/test_integration.py::test_survives_simulated_channel`.

## Frame layout and synchronization

```
Frame 0 .. header_repeat_count-1:  header frames (repeated GlobalHeader)
Frame header_repeat_count .. N-1:  payload frames (FEC+interleaved payload)
```

Every single frame — header or payload — carries a small **tag** in a fixed
top-left pixel rectangle: `frame_index`, a `session_tag` (discriminates this
stream from an unrelated video), the encoder's logical `frame_width`/
`frame_height`, and a CRC (`synchronization/frame_tag.py`). This is a
deliberate departure from the spec's suggested sparse "every K frames: a
checkpoint" design — continuous per-frame self-location means the decoder can
correctly reassemble a video that had frames dropped, duplicated, or
reordered by an fps conversion, without needing to search for "the nearest
checkpoint." The cost is small (64-96 bits/frame at a very conservative
margin) relative to a payload frame's capacity.

**Bootstrap problem and its resolution**: the decoder needs a resolution to
interpret the block grid, but the resolution normally lives inside the
header, and the header can't be read without a resolution. Two things solve
this:
1. The tag's own modulation parameters (block_size=16, margin=56) are a
   **protocol constant**, not stored in the header, and its region is a fixed
   *pixel* offset — readable without knowing the frame's logical size, as
   long as the actual delivered frame hasn't been rescaled.
2. If it *has* been rescaled (YouTube serving a different resolution than
   uploaded — common), the tag also carries the frame_width/frame_height the
   encoder used, but reading it at the wrong pixel offset in the first place
   won't produce a valid tag. Since videostore only ever encodes at one of a
   handful of preset resolutions, the decoder tries the actual delivered
   resolution first, then falls back to trying each preset resolution as an
   ffmpeg lanczos rescale target until one produces a valid tag+header. See
   `decoder/pipeline.py::_sniff_resolution`. A custom (non-preset) resolution
   combined with a channel that also rescales is a known gap — see
   [troubleshooting.md](troubleshooting.md).

The header itself uses the same bootstrap trick recursively: the decoder
doesn't need to know `header_repeat_count` ahead of time — it just keeps
accumulating tagged frames 0, 1, 2, ... and calling
`framing/layout.py::recover_header_bits` after each one, using
`GlobalHeader.unpack`'s own 32-bit CRC as the "did I get enough good copies
yet" signal. False-accept probability is ~1/4 billion, so this converges
safely to the ~10 frames actually written well before the scan limit.

## Soft-decision-assisted FEC

Full soft-decision (belief-propagation / LLR-based) decoding would need an
LDPC-family code; that wasn't implemented here (see
[protocol.md](protocol.md) for why Reed-Solomon was chosen). Instead,
`modulation.extract()` returns a confidence value per bit (distance from the
decision threshold, normalized to the margin), and the decoder aggregates it
per byte, marking a byte as an **erasure** for Reed-Solomon if any of its
bits fall below a threshold. RS decoding with known erasure positions
corrects up to `nsym` errors instead of `nsym/2` for unknown-position
errors — a real, if partial, soft-decision benefit without needing a
rate-adaptive LDPC implementation.

## What wasn't built

Being explicit, per the project's own "don't overclaim" rule:
- **LDPC / BCH / fountain codes**: not implemented. Reed-Solomon +
  interleaving covers the burst-error pattern that matters here; the FEC
  interface (`fec/reed_solomon.py`) is narrow enough to swap in another code.
- **True spatial/temporal spread-spectrum spreading**: the interface
  supports it conceptually (framing controls the logical-bit → block
  mapping), but it wasn't needed — the cover frames are fully controlled
  (flat background), so there's no "busy region vs. quiet region" pattern to
  spread away from, unlike classic watermarking-in-real-content scenarios.
- **Hardware encode (NVENC/QuickSync/VideoToolbox)**: not wired up. `ffmpeg`
  is invoked with software encoders only; this machine's ffmpeg build does
  support VideoToolbox, so adding a `--hw-encode` flag is a small follow-up
  (see [development.md](development.md)).
- **Real YouTube upload validation**: not performed — no credentials, and by
  design this tool never automates uploading. Only the local channel
  simulator has been run.

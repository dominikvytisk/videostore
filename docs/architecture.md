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

## Cover-video ("stego") mode

`--cover-video PATH` (CLI) / the web UI's "Your own video" carrier toggle
embeds into a real, existing video instead of the synthetic flat-gray
carrier, so the output looks like a normal upload rather than TV static.
This is a materially harder problem than the default mode, for reasons the
default mode never had to face:

- **Chroma has to carry the cover's real color** (`video/io.py::encode_video`
  hardcodes U/V to flat mid-gray otherwise — fine when the whole frame is
  synthetic, an instant giveaway against real footage). `encode_video_yuv420`
  passes real chroma through; only Y ever carries payload.
- **The tag and header used to be fixed, aggressive, full-visibility
  constants applied to every frame regardless of the payload scheme** — the
  per-frame sync tag overwrites a static top-left 256x256 block on *every*
  frame, and the header frames overwrite the *entire* frame for the first
  `header_repeat_count` (10) frames. A perfectly invisible payload scheme
  alone would still leave an obviously synthetic corner-flicker and a
  glitchy first third-of-a-second. See "Tag/header invisibility" below.
- **Capacity requires every block of every frame to carry a bit** — unlike
  classic watermarking (embed a few hundred bits once, in a few chosen
  spots), this system streams continuous payload data, so there's no sparse
  subset of "safe" pixels to hide in; the whole frame is instrumented, all
  the time. This puts capacity and invisibility in direct, structural
  tension (see the measured SSIM numbers below) — worth stating plainly
  rather than glossing over, since it's the reason this mode can reduce
  visibility but can't make itself as invisible as classic sparse
  steganography.

### Perceptually-masked modulation (`masked-luminance`, scheme_id=2)

`modulation/masked_luminance.py`'s `PerceptualMaskedModulation` uses the
exact same top/bottom-half mean-difference mechanic as `luminance-block`,
except the push size ("local margin") isn't a flat constant — it scales with
each block's own local contrast, clamped between a `margin_floor` and the
profile's `margin` (now a *ceiling*, not a fixed push):

```
local_std = average of the top-half and bottom-half standard deviations
            (NOT the whole block's std -- see below for why)
local_margin = clip(mask_gain * local_std, margin_floor, margin)
```

A flat region (sky, wall, shadow) gets pushed only as hard as
`margin_floor`; a highly textured region gets pushed as hard as the flat
`luminance-block` scheme would (`capacity_blocks()` is identical between the
two, so every block stays usable and the framing/layout math is unaffected —
this is what keeps capacity close to parity with the synthetic mode instead
of silently dropping blocks).

**Why average-of-half-std, not whole-block std**: pushing top/bottom apart
by a uniform delta is a per-pixel *shift* within each half, which doesn't
change that half's own variance at all (variance is shift-invariant). So
this measurement is exactly as robust to the scheme's own embedding as the
underlying bit decision already is — computed identically by the encoder
(from the pre-embed cover frame) and the decoder (from the possibly
transcoded received frame), without either side's reading being
contaminated by the push itself. This directly targets the **mask-desync**
risk: if encoder and decoder disagree on how much a block's texture
justified pushing, that disagreement could otherwise corrupt bits in a way
uncorrelated with ordinary channel noise (the same failure class that sank
`dct-pair`, one level removed — masking parameters instead of coefficients).
Two more mitigations are folded in from the start (this project's
`--cover-video` build committed to the full design up front rather than
gating on an isolated feasibility experiment): `local_margin` is quantized
into a small number of discrete tiers (small transcode-induced statistic
shifts rarely cross a tier boundary), and `margin_floor` is kept above the
FEC layer's erasure-confidence threshold, so a genuinely weak block degrades
to an erasure (which Reed-Solomon corrects reliably) rather than a
confidently-wrong bit.

**The bit itself is still just `sign(top_mean - bottom_mean)`** — margin
only sizes the push at encode time and normalizes confidence at decode time,
never decides the bit. This property is also what makes the tag/header fix
below possible without any protocol/bootstrap changes.

### Tag/header invisibility

Cover-video mode uses its own, separately-tuned tag/header constants
(`TAG_MODULATION_STEALTH`, `HEADER_MODULATION_STEALTH` — same masked scheme,
lower `margin_floor`) instead of the synthetic mode's fixed, loud ones. The
decoder does **not** need to know or guess which pair was used: since a
block-mean-difference scheme's bit decision doesn't depend on the
margin/masking config that embedded it (only on `block_size`, which is
identical — 16 — for both variants), reading with the plain synthetic
constant recovers the same bits either way. Only the confidence estimate is
(harmlessly) approximate for stealth-mode content, which is fine since
neither the tag nor the header feed FEC's erasure mechanism — unlike the
payload, whose modulation is already correctly self-described per scheme_id
via the header's own `modulation_type`/`mod_margin` fields.

Measured effect (mean absolute pixel delta in the header/tag region, cover
footage vs. the flat-margin equivalent): **~10.6-11.2 vs. ~24.5** — roughly
halved, a real and measured reduction, not a claim of full invisibility.

### Spread-spectrum mode (`--spread-factor N`)

Optional, off by default (`spread_factor=1`). Spends `N` raw blocks per
logical bit instead of 1, each pushed only `local_margin/N`, decoded by
summing the group's diffs — the classic spread-spectrum trade: the same
aggregate "signal energy," spread thinner across more, smaller pushes, reads
as texture/grain rather than a few large, sharply-defined changes. Costs
capacity linearly (`N`x more raw blocks needed per bit, so `N`x more frames/
duration for the same payload). See `modulation/masked_luminance.py`'s
module docstring for the exact grouping mechanics and how the tag-exclusion
math (`framing/regions.py`) stays correct for grouped logical bits.

Measured (`youtube-safe` profile, `youtube-medium` channel, `motion` cover
texture, 20KB payload):

| spread_factor | cover SSIM | reliable? |
|---|---|---|
| 1 (off) | 0.654 | yes |
| 2 | 0.761 | yes |
| 4 | 0.809 | yes |
| 8 | 0.823 | **no** — decompression failure (payload corrupted) |

`spread_factor=8` fails specifically because `youtube-safe`'s margin=32
ceiling divided by 8 (=4) falls below what survives a real transcode's
noise floor — not an implementation bug (a clean, channel-free round trip at
`spread_factor=8` recovers perfectly). Switching to `maximum-reliability`
(margin=48 ceiling, more FEC redundancy) extends the reliable range further.
A second sweep on `maximum-reliability` (tiny payload, `youtube-medium`,
40s cover to rule out looping as a confound):

| spread_factor | cover SSIM | reliable? |
|---|---|---|
| 8 | 0.827 | yes |
| **16** | **0.832** | **yes** |
| 32 | 0.833 | **no** — FEC uncorrectable blocks |
| 64 | 0.832 | **no** — FEC uncorrectable blocks |

Two findings worth being explicit about: (1) SSIM **plateaus around 0.83**
regardless of how far `spread_factor` goes — 8→16 barely moves it (0.827→
0.832) and 16→64 doesn't move it at all, confirming the structural point
above (every block of every frame carries payload, spreading further just
divides an already-thin signal thinner without changing that fact); (2)
reliability breaks somewhere between 16 and 32 on this profile/channel.

`spread_factor=16` looked like the measured sweet spot at first, but sits
*right at* that reliability cliff — a full `pytest` run (86 tests, real
system load from many concurrent-ish ffmpeg encodes) failed once at
`spread_factor=16` where three isolated re-runs of the exact same test all
passed. libx264's multi-threaded encoding isn't perfectly deterministic
under load, and 16 didn't leave enough margin to absorb that. `stego-invisible`
(see below) therefore ships with **`spread_factor=8`** instead — same
measured invisibility for practical purposes (0.827 vs. 0.832) with real
headroom below the observed failure point, rather than sitting on the edge
of it. This is exactly the kind of thing a single clean benchmark sweep can
miss and repeated/loaded runs catch — worth remembering before trusting a
"sweet spot" that was only measured once.

**Practical guidance**: `spread_factor=4` on `youtube-safe` is the best
invisibility-per-unit-of-effort for a quick manual `--spread-factor` bump
(SSIM 0.65 → 0.81, same reliability, no profile change needed). For the
measured ceiling of this technique with real safety margin, use the new
`stego-invisible` profile (`--profile stego-invisible --cover-video ...`),
which bundles `maximum-reliability`'s margin/FEC envelope with
`spread_factor=8` (SSIM ≈0.83) as its own default — no manual
`--spread-factor` needed, though it can still be overridden (e.g. to 16, if
you've re-verified it holds up under repeated runs on your own setup). It
needs a proportionally longer cover video than `stego-safe` for the same
payload (roughly 8x more frames) — this is the direct, honest cost of the
capacity/invisibility trade the profile makes. Always re-benchmark for your
actual channel before trusting a boundary like this on different hardware/
ffmpeg versions, per the project's own rule.

### Measured findings

`videostore benchmark --cover-corpus` (synthetic ffmpeg lavfi clips spanning
flat/detailed/motion textures, see `benchmark/testdata.py`'s
`generate_test_videos` — a proxy, not a substitute for real footage):

- **0% block error rate** across all three textures, at a fixed 480p
  resolution, through both `youtube-medium` and `youtube-low` channel
  profiles, using `stego-safe`'s parameters (same block_size/FEC envelope as
  `youtube-safe`) — including the `flat` cover, the worst case for masking
  (everything sits at `margin_floor`).
- **Cover-vs-encoded SSIM: ~0.65-0.68** across all three textures (measured
  by `benchmark/runner.py::_cover_invisibility_metrics`, sampled at the
  start/middle/end of the video so both header-frame and payload-frame
  visibility are represented). This is meaningfully less visible than the
  always-100%-payload synthetic carrier, but it is **not** true
  imperceptibility — a real, measurable structural difference from the
  source remains, which is the direct consequence of the capacity/
  invisibility tension described above (every block of every frame carries
  payload). Don't oversell this mode as undetectable; it's "noticeably less
  obviously synthetic," measured, not "invisible."
- Not yet validated against real (non-synthetic) footage or a real YouTube
  upload — see [benchmarking.md](benchmarking.md).

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
- **True spatial/temporal spread-spectrum spreading**: for the synthetic
  carrier, the interface supports it conceptually (framing controls the
  logical-bit → block mapping) but it wasn't needed, since the cover frames
  are fully controlled (flat background) and there's no "busy region vs.
  quiet region" pattern to spread away from. Cover-video mode's
  `masked-luminance` scheme takes the more modest step of scaling push
  *magnitude* per-block by local contrast (see "Cover-video mode" above),
  not full spread-spectrum coding across blocks/frames — a genuinely spread
  scheme (redundant, spatially/temporally distributed bit encoding) remains
  unbuilt and would be the natural next step if `masked-luminance`'s
  measured SSIM/capacity trade-off (see above) isn't good enough for a given
  use case.
- **Hardware encode (NVENC/QuickSync/VideoToolbox)**: not wired up. `ffmpeg`
  is invoked with software encoders only; this machine's ffmpeg build does
  support VideoToolbox, so adding a `--hw-encode` flag is a small follow-up
  (see [development.md](development.md)).
- **Real YouTube upload validation**: not performed — no credentials, and by
  design this tool never automates uploading. Only the local channel
  simulator has been run.

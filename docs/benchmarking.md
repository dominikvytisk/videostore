# Benchmarking

All numbers below are **measured in this repository**, using the local
`ffmpeg`-based channel simulator (`channel/simulator.py`) — not a real
YouTube upload. See [youtube-channel.md](youtube-channel.md) for exactly what
that gap means. Every table states resolution/codec/CRF/profile explicitly,
per the project rule against unsupported capacity/reliability claims.

## Reproducing these results

```bash
videostore benchmark --profile youtube-safe --profile maximum-reliability \
    --channel lossless-passthrough --channel youtube-medium --channel youtube-low \
    --resolution 720p --size 100000 -o ./bench-results
# -> ./bench-results/benchmark.{json,csv,html}
```

`videostore benchmark --list-channels` shows all channel profiles and their
(unverified-against-real-YouTube) parameters.

## 1. Modulation scheme survival (the experiment that picked the default)

Setup: flat-gray 640x480 cover, `libx264`, 5 frames per point, bit-error
rate measured directly (not via the full pipeline — this isolates the
modulation layer). Full method and code in `docs/architecture.md`.

| modulation | block | margin | CRF18 | CRF23 | CRF28 | CRF32 | CRF36 | CRF40 |
|---|---|---|---|---|---|---|---|---|
| dct-pair (2,3)/(3,2) | 8 | 12 | 0.59% | 10.2% | 49.6% | 50.3% | — | — |
| dct-pair (2,3)/(3,2) | 8 | 32 | 0.00% | 0.49% | 6.90% | 49.9% | — | — |
| dct-pair (2,3)/(3,2) | 8 | 48 | 0.00% | 0.04% | 7.31% | — | 18.8% | — |
| dct-pair near-DC (1,0)/(0,1) | 8 | 32 | — | 0.03% | 2.74% | 26.5% | 45.5% | — |
| luminance-block | 4 | 32 | — | 0.00% | — | 4.74% | — | 45.4% |
| luminance-block | 8 | 32 | — | 0.00% | — | 0.01% | — | 22.8% |
| luminance-block | 16 | 16 | — | 0.00% | — | 0.00% | — | 8.73% |
| luminance-block | 16 | 32 | — | 0.00% | — | 0.00% | — | 0.40% |
| luminance-block | 32 | 16 | — | 0.00% | — | 0.00% | — | 0.00% |
| luminance-block | 32 | 32 | — | 0.00% | — | 0.00% | — | 0.00% |

**Conclusion**: `luminance-block` strictly dominates `dct-pair` at every
setting tried, contrary to the initial hypothesis. See
[architecture.md](architecture.md) for why.

Codec comparison (`luminance-block`, block=16, margin=32):

| codec | CRF23 | CRF32 | CRF40/50 |
|---|---|---|---|
| libx264 | 0.00% | 0.00% | 0.23% (crf40) |
| libx265 | 0.00% | 0.00% | 0.70% (crf40) |
| libsvtav1 | 0.00% (crf30) | 0.00% (crf40) | 0.00% (crf50) |

Resolution-downscale robustness (encode 640x480, channel rescales to
320x240, decoder rescales back up via ffmpeg lanczos before demodulating):
**0.00% BER** (block=16, margin=32, libx264 CRF23, n=6000 bits).

## 2. Reed-Solomon + interleaving (burst tolerance)

Direct FEC-layer test (`fec/interleave.py`'s docstring worked example):
RS(255, nsym=51) [20% redundancy], a 400-byte contiguous burst corruption
injected into the channel stream:

| arrangement | blocks_uncorrectable |
|---|---|
| no interleaving (depth=1) | 2 / 50 |
| interleaved, depth=8 | 8 / 50 |

This is a real, useful negative result, not cherry-picked: interleaving
**redistributes** damage rather than reducing it, and only *helps* when the
burst is large relative to a single block but the per-block share after
division across `depth` blocks drops back under each block's correction
capacity (`nsym/2` errors). Here `400/8=50` corrupted bytes/block still
exceeds the ~25-byte blind-correction capacity, so interleaving spread
failure across more blocks instead of concentrating it in fewer. Depth needs
to be sized relative to the *actual* burst length a channel produces — a
number best obtained from `videostore benchmark`, not assumed. The default
profile depths (`presets.py`) are moderate (16-48) and validated empirically
via the full-pipeline tests below, not via this isolated worst-case.

## 3. Full pipeline: encode → channel → decode → compare (SHA-256)

112 of 120 planned runs from one `videostore benchmark` invocation (720p
source, 100KB test files of 5 distinct compressibility profiles — random,
text, synthetic image, pre-compressed, structured-binary — × 4 reliability
profiles × 6 channels; the run was stopped for time budget in this authoring
session at 112/120, not due to any failure):

- **89 PASS, 8 FAIL** (byte-for-byte SHA-256 match + full FEC/archive
  integrity) among the captured results.
- **Every failure** was `maximum-capacity` or `balanced` (15%/20% FEC
  redundancy) against the `youtube-low` channel (CRF 32 + downscale to
  854x480). **Zero failures** for `youtube-safe` (25%) or
  `maximum-reliability` (35%) against any of the 6 channels tested at this
  resolution.
- Reproduces the general shape of the modulation-layer table above: more
  redundancy buys reliability against harsher channels, and the "safe" and
  "reliability" profiles were specifically sized (via the sweep above) to
  clear CRF 32 on a matched-resolution channel.

Separately (not from this sweep): stacking a **larger** downscale (1080p
source delivered at 480p, ~2.3x) simultaneously with CRF 32 broke
`youtube-safe` (`blocks_uncorrectable=0` but `bytes_corrected=5152` across 96
blocks — Reed-Solomon miscorrecting, caught by the archive checksum) while
`maximum-reliability` handled it cleanly. See
[architecture.md](architecture.md)'s "profile vs. channel severity" section
for the full writeup and exact numbers. This is captured as a permanent
regression test in `tests/test_integration.py::test_survives_simulated_channel`.

## 4. Cover-video ("stego") mode

```bash
videostore benchmark --profile stego-safe --cover-corpus \
    --channel youtube-medium --channel youtube-low --size 20000 -o ./bench-cover
```

`--cover-corpus` runs the matrix once per synthetic ffmpeg lavfi cover clip
(`flat`/`detailed`/`motion`, see `benchmark/testdata.py::generate_test_videos`
— a proxy for real footage, not a substitute for it) instead of a single
`--cover-video`. Each result gets two extra columns beyond the normal ones:
`cover_video` (which clip, and whether it had to loop to fit the payload) and
`cover_psnr_db`/`cover_ssim_index` — PSNR/SSIM between the encoded
(pre-channel) output and the cover video itself, sampled at the start/
middle/end of the video. This is the "does it look different from the
source" invisibility metric, distinct from the existing `psnr_db`/
`ssim_index` columns (encoded vs. post-channel, which measure transcode
damage, not embedding visibility).

Measured (20KB test corpus, 480p, `stego-safe` profile):

| cover texture | channel | block error rate | cover PSNR (dB) | cover SSIM |
|---|---|---|---|---|
| flat | youtube-medium | 0.00% | 29.5 | 0.670 |
| flat | youtube-low | 0.00% | 29.5 | 0.668 |
| detailed | youtube-medium | 0.00% | 27.2 | 0.681 |
| motion | youtube-medium | 0.00% | 27.0 | 0.667 |

**Reliability**: 0% block error rate held across every texture and both
channel severities tested, including the `flat` cover (the worst case for
masking — every block sits at `margin_floor` since there's no texture to
exploit). This mirrors `stego-safe`'s reuse of `youtube-safe`'s FEC envelope
at a fixed resolution (not the large-downscale-plus-harsh-CRF combined
stress that broke `youtube-safe` in section 3 above — that combined-stress
case hasn't been re-tested for cover-video mode yet).

**Invisibility**: cover SSIM sits around 0.65-0.68 regardless of cover
texture, essentially flat across flat/detailed/motion. This is a real,
somewhat surprising measured result worth being honest about: because
capacity requires every block of every frame to carry payload data (see
[architecture.md](architecture.md)'s cover-video section), there's no sparse
"safe" region for the masking to fully exploit the way classic sparse
steganography would — the whole frame is instrumented continuously, so even
the "detailed"/"motion" covers (which allow bigger legitimate pushes) don't
come out meaningfully more invisible than the "flat" one in aggregate. Read
this as "measurably, meaningfully less visible than the always-100%-payload
synthetic carrier," not as "invisible."

Not yet run: real (non-synthetic) footage, a real YouTube upload round trip,
or the combined downscale+harsh-CRF stress test from section 3.

**`--spread-factor N`** (`run_one`/`run_matrix`'s `spread_factor` param)
trades capacity for a real, measured invisibility improvement — SSIM 0.654 →
0.809 at `spread_factor=4` on `youtube-safe`/`youtube-medium`, with the same
0% block error rate. `spread_factor=8` breaks reliability on `youtube-safe`
specifically (margin/8 falls below the transcode's noise floor) but is fine
on `maximum-reliability`. A follow-up sweep on `maximum-reliability` found
SSIM **plateaus around 0.83** by `spread_factor=16` (further increases don't
help) and reliability breaks between 16 and 32. `spread_factor=16` initially
looked like the sweet spot, but a full `pytest` run under real system load
caught it sitting right at that cliff (one failure in 4 total runs, where
`spread_factor=8` never failed) — a single clean sweep isn't enough to trust
a boundary that close to the edge. The new `stego-invisible` profile
(`presets.py`) therefore bundles `spread_factor=8` as its built-in default
(same ~0.83 SSIM, real margin below the observed failure point) rather than
requiring a manual `--spread-factor` guess. Full tables and the reasoning in
[architecture.md](architecture.md)'s "Spread-spectrum mode" section — this
is exactly the kind of profile/parameter boundary that needs measuring per
combination (and re-measuring under load), not assuming.

## 5. What isn't measured yet

- **Real YouTube upload/download.** See [youtube-channel.md](youtube-channel.md).
- **VMAF/PSNR/SSIM across the full matrix** — the benchmark runner computes
  these per-run (`BenchmarkResult.psnr_db`/`ssim_index`) and they're in the
  JSON/CSV/HTML output, but no summary table is included here; `--list-
  channels` output and the HTML report's charts are the fastest way to see
  them for your own run.
- **Frame-drop-heavy channels** (aggressive fps conversion) beyond the
  literal frame-cutting integration test — the FEC/tag-based design should
  handle this (see `test_missing_frames_still_recover_via_fec`), but a
  systematic sweep over drop rates wasn't run.
- **Multi-GB payloads** — see [troubleshooting.md](troubleshooting.md), the
  bit-splitting memory limitation.

## Metric definitions (`benchmark/runner.py::BenchmarkResult`)

`effective_payload_bitrate_mbps` = original (pre-compression) payload size ×
8 / video duration — the actual "how much of my data per second of video"
number, not the video's physical bitrate (`physical_bitrate_mbps`, reported
alongside it). This is the metric spec section 51 asks to optimize:
recoverable payload bits per second of final video, not raw embedded bits.
`block_error_rate` = `blocks_uncorrectable / blocks_total` from the FEC
decode stats — an approximation of channel bit-error rate at the RS-block
granularity, not a true per-bit BER.

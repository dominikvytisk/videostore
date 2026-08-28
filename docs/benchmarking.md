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

## 4. What isn't measured yet

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

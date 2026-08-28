# Troubleshooting

## `decode` fails with "no valid VideoStore frame tags found"

The decoder couldn't find a valid tag in the first 60 frames at any of the
resolutions it tried (the actual delivered resolution, then every preset in
`presets.RESOLUTIONS`). Causes, roughly in likelihood order:

- **This isn't a videostore video**, or it's a different video entirely (you
  grabbed the wrong `yt-dlp` output, wrong URL, etc).
- **The video was encoded at a custom (non-preset) resolution** (`--resolution
  1234x567`) and the channel also rescaled it. The bootstrap fallback only
  tries preset resolutions (480p/720p/1080p/1440p/2160p) — see
  [architecture.md](architecture.md)'s "bootstrap problem" section. Fix:
  encode at a preset resolution, or extend `_sniff_resolution` to also try
  your custom resolution.
- **Extremely aggressive transcoding** destroyed even the tag region (very
  unlikely given the tag's conservative margin — see
  [benchmarking.md](benchmarking.md) — but not impossible at extreme
  settings/very low resolutions).

## `decode` fails with "could not recover a valid header"

Frame tags were found (so it is a videostore video, or at least gives every
appearance of being one), but no set of frames tagged 0..N produced a header
that passed its CRC32 within `MAX_HEADER_SCAN_FRAMES` (128). This means the
header region itself was damaged badly enough across all its repeats — check
whether `payload_frames_present` in the decode report looks reasonable; if
payload frames are mostly missing too, the video is likely truncated or
severely corrupted rather than this being a header-specific issue.

## `decode` succeeds but `fully_recovered` is `False`

Check the three things `fully_recovered` requires:
- `fec_stats.blocks_uncorrectable == 0` — some RS blocks couldn't be
  corrected even with erasure hints. Re-encode with a higher-redundancy
  profile (`--profile maximum-reliability`) if this happens consistently.
- `archive_checksum_ok` — the recovered archive doesn't match what was
  encoded, **even though FEC reported no uncorrectable blocks**. This is the
  RS-miscorrection scenario documented in
  [architecture.md](architecture.md)'s "profile vs. channel severity"
  section — it means the true error rate exceeded what the profile's
  redundancy was measured to handle for that channel severity. Use a
  stronger profile.
- `failed` is non-empty — specific files' checksums didn't match even though
  the archive as a whole parsed; check `decode --debug` output for per-file
  reasons.

## Decryption fails for every chunk

`DecodeError: decryption failed for every chunk`. Almost always a wrong
password. Can also mean FEC didn't recover *any* usable data (extremely
unlikely to manifest this way rather than as FEC/checksum failures first —
check `fec_stats` in the same error path if you hit this).

## Encode/decode is slow

- **Reed-Solomon is pure-Python-ish** (`reedsolo`) — this is the main CPU
  cost for large payloads. It's currently single-threaded per pipeline run;
  parallelizing block encode/decode across a process pool (blocks are
  independent) is a natural follow-up not yet implemented — see
  [development.md](development.md).
- **The final bit-splitting stage loads the whole interleaved payload into
  RAM** as a bit array (8x its byte size) — see
  [development.md](development.md)'s known-follow-ups. For very large
  inputs this dominates memory, not just time.
- Use `--preset ultrafast` (ffmpeg preset, not a videostore profile) for
  faster iteration during testing; it doesn't meaningfully affect the
  measured survivability numbers in [benchmarking.md](benchmarking.md),
  which were all run at `ultrafast`/ `medium` and showed CRF/resolution
  dominating over encoder preset.

## Large files run out of memory

See the bit-splitting limitation above. Practically: this has been tested up
to a few hundred MB of payload comfortably; multi-GB payloads will need the
streaming bit-cursor follow-up before they're safe on a memory-constrained
machine.

## "Not a real YouTube result"

Everything in [benchmarking.md](benchmarking.md) and the profile
descriptions in `presets.py` is measured against the **local channel
simulator**, not a real YouTube upload — see
[youtube-channel.md](youtube-channel.md) for exactly what that gap means and
how to close it yourself.

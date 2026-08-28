# Protocol

## Layering

```
CONTAINER (VSAR archive)
  header: magic, version, flags, file_count
  file data (concatenated)
  manifest (per-file: id, path, size, mtime, mode, BLAKE3 checksum, offset)
  manifest checksum
  footer: manifest_offset, manifest_len, archive checksum, magic
      ↓ zstd (optional, auto-detected) or none
      ↓ AES-256-GCM or ChaCha20-Poly1305, chunked (optional; requires --password)
      ↓ Reed-Solomon (configurable redundancy)
      ↓ block interleaving (configurable depth)
      ↓ modulation (bit → pixel block)
      ↓ ffmpeg (H.264/H.265/AV1 → mp4)
```

A binary format is used for the archive itself (not JSON) because this is the
payload that gets compressed/FEC-coded/embedded — JSON's overhead directly
costs channel capacity. JSON is fine (and used) for benchmark reports, which
never get embedded.

## Archive format (VSAR) — `archive/manifest.py`, `archive/pack.py`

ZIP-central-directory style: data first, manifest at the end. This lets the
archive be *built* in one streaming pass — per-file checksums are only known
once a file's bytes have been read, so the manifest (which needs those
checksums) has to come after the data it describes, not before it.

```
[HEADER]   magic 'VSAR', version u16, flags u32, file_count u32     (14 bytes)
[DATA]     file bytes, back to back, in manifest order
[MANIFEST] per entry: file_id u32, path_len u16, size u64, mtime i64,
           mode u32, offset u64 (relative to end of HEADER), checksum[32]
           followed by the path bytes (utf-8)
[MANIFEST_CHECKSUM]  BLAKE3(manifest bytes), 32 bytes
[FOOTER]   manifest_offset u64, manifest_len u64, archive_checksum[32], magic
```

`archive_checksum` = BLAKE3 over everything *except* the footer (the footer
contains the checksum, so it can't hash itself — see
`archive/pack.py::compute_archive_checksum`, which both the archive builder
and the decoder's final integrity check use, so they can never drift apart).

Extraction is defensive: a corrupted/truncated file only fails *that file*
(checksum mismatch is caught per-entry); a corrupted manifest/footer fails
the whole archive, since there's no way to locate anything without it. Paths
are sanitized on both write and read (`utils/pathsafe.py`) — `../../etc/passwd`
style entries are rejected outright, never partially trusted.

## GlobalHeader — `container/format.py`

Fixed-size (187 bytes), versioned struct. Fixed-size deliberately: it's the
one thing the decoder must recover before it can interpret anything else, so
it gets embedded redundantly and needs to be trivial to majority-vote /
tile-and-recombine — a variable-length or self-referential format would
complicate exactly the recovery path that has to work under the worst
conditions. Extensibility is by `protocol_version`, not variable length; a 32
-byte reserved block absorbs minor additions without a version bump.

Fields (see the struct in `container/format.py` for exact types/order):
magic, protocol/modulation/container versions, flags (encrypted/compressed),
session_id, original/compressed/encrypted sizes, compression algorithm, AEAD
algorithm + KDF parameters + nonce prefix, FEC type/nsize/nsym/interleave
depth, modulation type/margin/spread_factor/symbol_bits/block_size, frame
width/height/fps, total_frames, header_repeat_count, checkpoint_interval
(reserved, unused — see below), archive_checksum, a 32-byte header CRC32.

`checkpoint_interval` is present for forward-compatibility with the spec's
sparse-checkpoint design but unused by this version — continuous per-frame
tagging (below) supersedes it. A future protocol version could use it for a
scheme that doesn't tag every frame.

## Frame tag — `synchronization/frame_tag.py`

64+32 = 96 bits, protocol-fixed modulation (`LuminanceBlockModulation`,
block_size=16, margin=56), embedded in every frame's top-left 256x256 pixel
region:

```
frame_index      u32
session_tag      u16   (first 2 bytes of session_id)
frame_width      u16
frame_height     u16
crc16            u16   (low 16 bits of crc32 over the preceding fields)
```

See [architecture.md](architecture.md) for why this replaces sparse
checkpointing and how it resolves the resolution/header bootstrap problem.

## FEC — `fec/reed_solomon.py`, `fec/interleave.py`

Reed-Solomon over GF(256), `nsize=255`, `nsym` chosen from a redundancy
fraction (`--fec 0.25` → `nsym = round(255*0.25) = 64`). Systematic encoding
(message bytes are a verbatim prefix of the codeword). Chosen for maturity
and because burst-error correction (its classic strength) is exactly what a
transcoding channel needs — see [architecture.md](architecture.md) for what
wasn't built (LDPC/BCH/fountain) and why.

Interleaving groups `depth` consecutive RS blocks and transposes them
byte-column-wise before writing to the channel (and inverts on decode) — see
`fec/interleave.py`'s docstring for the burst-vs-block-capacity math. Both a
whole-buffer (`interleave`/`deinterleave`, for tests and small payloads) and
a `numpy.memmap`-backed streaming version (`interleave_file`/
`deinterleave_file`, O(depth × block_size) memory) exist; the pipelines use
the streaming version.

## Modulation — `modulation/`

Common interface: `capacity_blocks(w, h)`, `embed(plane, bits) -> plane`,
`extract(plane) -> (bits, confidence)`. Two implementations:
`LuminanceBlockModulation` (default — see architecture.md for why) and
`DCTPairModulation` (experimental). Both crop to a block-size-aligned usable
region rather than requiring exact divisibility (`modulation/base.py::
usable_dims`) — 1080 isn't a multiple of 32, for instance.

## Encryption — `crypto/aead.py`, `crypto/kdf.py`

Argon2id (via `argon2-cffi`) derives a 256-bit key from the password + a
random salt; salt and cost parameters are stored in the header, the password
and key never are. AEAD (AES-256-GCM or ChaCha20-Poly1305) is applied in a
**chunked STREAM construction** (à la `age`'s STREAM, Rogaway/Shrimpton):
each 1 MiB chunk is its own AEAD call, with `(chunk_index, is_last)` bound
into the AAD so truncation and chunk-reordering are still caught even though
each chunk authenticates independently. This bounds memory to one chunk
regardless of payload size, and — critically for partial recovery — lets a
`best_effort` decrypt mode zero-fill and count *individual* failed chunks
instead of aborting the whole file on the first bad one (spec's partial-
recovery goal would otherwise be defeated by AEAD's own integrity guarantee;
see [security.md](security.md)).

## Compression — `compression/engine.py`

zstd (streaming) or none. `auto` mode compresses a 1 MiB sample and only
commits to full compression if the sample shrinks below 97% of its original
size — avoids spending FEC/modulation capacity encoding a zstd header around
data that's already compressed (spec section 7).

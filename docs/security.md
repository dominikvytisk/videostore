# Security

## Threat model

The decoder treats every video it opens as **untrusted input** — it may be a
corrupted download, a video that isn't a videostore stream at all, or (in
principle) a maliciously crafted file. Nothing about the pipeline requires
trusting the video's contents to behave.

What's explicitly defended against:

- **Path traversal**: archive member paths (`../../etc/passwd`,
  absolute paths, drive letters, NUL bytes) are rejected at both write time
  and read time (`utils/pathsafe.py`). Extraction re-resolves and re-checks
  every path against the output directory immediately before writing,
  independent of whatever validation happened when the archive was built —
  a manifest recovered from a damaged/adversarial video is validated exactly
  as strictly as one built locally.
- **Malformed headers**: the `GlobalHeader` struct is fixed-size and CRC32-
  checked before any of its fields are trusted; `GlobalHeader.unpack` raises
  on a bad magic, unsupported version, or checksum mismatch rather than
  guessing.
- **Integer/size confusion**: archive entry offsets and sizes are checked
  against the actual file size and manifest boundaries before any seek/read
  happens (`archive/pack.py::extract_archive`); a claimed range that would
  read past available data is rejected per-file, not trusted.
- **Decompression bombs**: zstd decompression is streamed in bounded chunks
  (`compression/engine.py`), so memory is bounded regardless of the claimed
  or actual decompressed size. Disk space is not currently bounded — a
  malicious payload could still claim an enormous `original_size`; this is a
  known gap or if you're decoding untrusted videos, apply an external quota
  (see [troubleshooting.md](troubleshooting.md)).
- **Malformed FEC/modulation parameters**: FEC block size/redundancy and
  modulation block size/margin come from the header, but are only ever used
  to size numpy arrays / call `RSCodec` — invalid combinations fail with a
  Python exception rather than corrupting memory (no manual buffer
  arithmetic in unsafe languages is involved).

## Cryptography

- **Key derivation**: Argon2id (`argon2-cffi`), the winner of the Password
  Hashing Competition and current best-practice recommendation for
  password-based KDFs, memory-hard against GPU/ASIC attack. Default cost:
  time_cost=3, memory_cost=256 MiB, parallelism=4 — deliberately expensive;
  raise `time_cost`/`memory_cost_kib` for higher-value payloads.
- **Encryption**: AES-256-GCM or ChaCha20-Poly1305 (both from the
  well-audited `cryptography` package), applied as authenticated encryption
  (AEAD) — confidentiality *and* integrity, not confidentiality alone.
  ChaCha20-Poly1305 is used when a password is set (no hardware-AES
  dependency, consistent performance).
- **The password is never stored.** Only the Argon2id salt and cost
  parameters, and the AEAD nonce prefix, live in the header — everything
  needed to *re-derive* the key from the password, nothing that would let
  someone recover the key or password from the video alone.
- **Chunked AEAD** (`crypto/aead.py`): binds `(chunk_index, is_last)` into
  each chunk's AAD, so truncating the ciphertext or reordering chunks is
  caught by authentication, not just by an external length check.

## Defense in depth: three independent integrity checks

1. **AEAD authentication tag** (when encrypted) — proves the decrypted bytes
   are exactly what was encrypted, per chunk.
2. **Archive-level BLAKE3 checksum** (`archive_checksum` in the header,
   verified via `compute_archive_checksum` on decode) — proves the recovered
   archive bytes match what was originally packed, independent of encryption
   being enabled at all.
3. **Per-file BLAKE3 checksum** inside the archive manifest — lets a
   partially-damaged archive still recover the files that *are* intact
   instead of failing all-or-nothing.

These are intentionally redundant. See
[architecture.md](architecture.md)'s "profile vs. channel severity" section
for a real case where Reed-Solomon reported success but had actually
miscorrected — caught by check #2, not by FEC itself. Never trust "FEC
reported 0 uncorrectable blocks" as proof of correctness on its own; the
system doesn't, and `DecodeReport.fully_recovered` requires the archive
checksum to match too.

## What this does *not* protect against

- **Traffic analysis**: the fact that a video is a videostore-encoded
  payload is not hidden — frame content is visibly synthetic (flat gray with
  structured blocks), and the format is fully documented here. This is a
  storage/retrieval tool, not steganography-for-concealment.
- **A YouTube account compromise, or YouTube itself reading the payload**:
  encryption protects the *payload* from anyone without the password,
  including YouTube, but doesn't hide that a video exists or who uploaded
  it.
- **Disk-space exhaustion from a maliciously large claimed size** (see
  above) — apply an OS-level quota or `ulimit` if decoding untrusted
  third-party videos at scale.

"""The global header: a single fixed-size, versioned, self-describing struct
that tells the decoder everything it needs to reconstruct the payload without
the user remembering any encoding settings (spec section 35, "reproducibility").

Deliberately fixed-size (not TLV/variable-length): this header is the one
piece of data the decoder must recover before it can interpret anything else,
so it gets embedded redundantly across many frames with the *most*
conservative modulation settings available (see framing/layout.py). A
fixed-size struct is trivial to majority-vote/repeat-decode across those
repeats; a variable-length or self-referential format would not be.

Extensibility is handled by `protocol_version`, not by variable-length
encoding: a future v2 gets its own struct and the CLI can look at the first 6
bytes (magic + version) before deciding which parser to use. A small
`reserved` block is kept zeroed for minor additions without a version bump.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from videostore.utils.hashing import crc32

MAGIC = b"VSSF"
PROTOCOL_VERSION = 1


class Flags:
    ENCRYPTED = 1 << 0
    COMPRESSED = 1 << 1


_FMT = (
    ">4s"    # magic
    "H"      # protocol_version
    "H"      # modulation_scheme_version
    "H"      # container_version (archive format version)
    "I"      # flags
    "16s"    # session_id
    "Q"      # original_size (archive, uncompressed)
    "Q"      # compressed_size
    "Q"      # encrypted_size (== FEC input size)
    "B"      # compression_algo
    "B"      # encryption_algo
    "B"      # kdf_algo
    "16s"    # kdf_salt
    "B"      # kdf_time_cost
    "I"      # kdf_memory_kib
    "B"      # kdf_parallelism
    "8s"     # aead_nonce_prefix
    "B"      # fec_type
    "H"      # fec_nsize
    "H"      # fec_nsym
    "H"      # fec_interleave_depth
    "B"      # modulation_type
    "H"      # mod_margin_q8 (margin * 256, fixed point)
    "H"      # mod_spread_factor
    "B"      # mod_symbol_bits
    "B"      # mod_block_size
    "H"      # frame_width
    "H"      # frame_height
    "H"      # fps_num
    "H"      # fps_den
    "I"      # total_frames
    "H"      # header_repeat_count
    "I"      # checkpoint_interval (frames)
    "32s"    # archive_checksum (blake3 of the original VSAR archive)
    "32s"    # reserved
)
_STRUCT = struct.Struct(_FMT)
HEADER_LEN = _STRUCT.size + 4  # + trailing CRC32


@dataclass
class GlobalHeader:
    protocol_version: int = PROTOCOL_VERSION
    modulation_scheme_version: int = 1
    container_version: int = 1
    flags: int = 0
    session_id: bytes = b"\x00" * 16
    original_size: int = 0
    compressed_size: int = 0
    encrypted_size: int = 0
    compression_algo: int = 0
    encryption_algo: int = 0
    kdf_algo: int = 0
    kdf_salt: bytes = b"\x00" * 16
    kdf_time_cost: int = 0
    kdf_memory_kib: int = 0
    kdf_parallelism: int = 0
    aead_nonce_prefix: bytes = b"\x00" * 8
    fec_type: int = 1
    fec_nsize: int = 255
    fec_nsym: int = 32
    fec_interleave_depth: int = 32
    modulation_type: int = 0
    mod_margin: float = 0.0
    mod_spread_factor: int = 1
    mod_symbol_bits: int = 1
    mod_block_size: int = 8
    frame_width: int = 1920
    frame_height: int = 1080
    fps_num: int = 30
    fps_den: int = 1
    total_frames: int = 0
    header_repeat_count: int = 30
    checkpoint_interval: int = 60
    archive_checksum: bytes = b"\x00" * 32

    @property
    def is_encrypted(self) -> bool:
        return bool(self.flags & Flags.ENCRYPTED)

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & Flags.COMPRESSED)

    def pack(self) -> bytes:
        body = _STRUCT.pack(
            MAGIC,
            self.protocol_version,
            self.modulation_scheme_version,
            self.container_version,
            self.flags,
            self.session_id,
            self.original_size,
            self.compressed_size,
            self.encrypted_size,
            self.compression_algo,
            self.encryption_algo,
            self.kdf_algo,
            self.kdf_salt,
            self.kdf_time_cost,
            self.kdf_memory_kib,
            self.kdf_parallelism,
            self.aead_nonce_prefix,
            self.fec_type,
            self.fec_nsize,
            self.fec_nsym,
            self.fec_interleave_depth,
            self.modulation_type,
            int(round(self.mod_margin * 256)) & 0xFFFF,
            self.mod_spread_factor,
            self.mod_symbol_bits,
            self.mod_block_size,
            self.frame_width,
            self.frame_height,
            self.fps_num,
            self.fps_den,
            self.total_frames,
            self.header_repeat_count,
            self.checkpoint_interval,
            self.archive_checksum,
            b"\x00" * 32,
        )
        return body + struct.pack(">I", crc32(body))

    @classmethod
    def unpack(cls, buf: bytes) -> "GlobalHeader":
        if len(buf) < HEADER_LEN:
            raise ValueError(f"header buffer too short: {len(buf)} < {HEADER_LEN}")
        body, stored_crc_bytes = buf[: _STRUCT.size], buf[_STRUCT.size : HEADER_LEN]
        stored_crc = struct.unpack(">I", stored_crc_bytes)[0]
        if crc32(body) != stored_crc:
            raise ValueError("header checksum mismatch (header corrupted)")
        vals = _STRUCT.unpack(body)
        (
            magic,
            protocol_version,
            modulation_scheme_version,
            container_version,
            flags,
            session_id,
            original_size,
            compressed_size,
            encrypted_size,
            compression_algo,
            encryption_algo,
            kdf_algo,
            kdf_salt,
            kdf_time_cost,
            kdf_memory_kib,
            kdf_parallelism,
            aead_nonce_prefix,
            fec_type,
            fec_nsize,
            fec_nsym,
            fec_interleave_depth,
            modulation_type,
            mod_margin_q,
            mod_spread_factor,
            mod_symbol_bits,
            mod_block_size,
            frame_width,
            frame_height,
            fps_num,
            fps_den,
            total_frames,
            header_repeat_count,
            checkpoint_interval,
            archive_checksum,
            _reserved,
        ) = vals
        if magic != MAGIC:
            raise ValueError(f"bad magic: {magic!r} (this is not a VideoStore-encoded video)")
        if protocol_version != PROTOCOL_VERSION:
            raise ValueError(
                f"unsupported protocol version {protocol_version} (this build supports {PROTOCOL_VERSION})"
            )
        return cls(
            protocol_version=protocol_version,
            modulation_scheme_version=modulation_scheme_version,
            container_version=container_version,
            flags=flags,
            session_id=session_id,
            original_size=original_size,
            compressed_size=compressed_size,
            encrypted_size=encrypted_size,
            compression_algo=compression_algo,
            encryption_algo=encryption_algo,
            kdf_algo=kdf_algo,
            kdf_salt=kdf_salt,
            kdf_time_cost=kdf_time_cost,
            kdf_memory_kib=kdf_memory_kib,
            kdf_parallelism=kdf_parallelism,
            aead_nonce_prefix=aead_nonce_prefix,
            fec_type=fec_type,
            fec_nsize=fec_nsize,
            fec_nsym=fec_nsym,
            fec_interleave_depth=fec_interleave_depth,
            modulation_type=modulation_type,
            mod_margin=mod_margin_q / 256.0,
            mod_spread_factor=mod_spread_factor,
            mod_symbol_bits=mod_symbol_bits,
            mod_block_size=mod_block_size,
            frame_width=frame_width,
            frame_height=frame_height,
            fps_num=fps_num,
            fps_den=fps_den,
            total_frames=total_frames,
            header_repeat_count=header_repeat_count,
            checkpoint_interval=checkpoint_interval,
            archive_checksum=archive_checksum,
        )

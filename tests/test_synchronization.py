import numpy as np

from videostore.container.format import GlobalHeader
from videostore.framing.layout import (
    HEADER_MODULATION,
    header_capacity_per_frame,
    recover_header_bits,
    tile_header_bits,
)
from videostore.framing.regions import gather_logical_bits, scatter_logical_bits
from videostore.synchronization.frame_tag import embed_tag, extract_tag, session_tag_from_id


def test_tag_roundtrip_lossless():
    plane = np.full((480, 640), 128.0)
    tagged = embed_tag(plane, frame_index=42, session_tag=0xBEEF, frame_width=640, frame_height=480)
    tagged_u8 = np.clip(np.round(tagged), 0, 255).astype(np.uint8).astype(np.float64)
    tag = extract_tag(tagged_u8)
    assert tag.valid
    assert tag.frame_index == 42
    assert tag.session_tag == 0xBEEF
    assert (tag.frame_width, tag.frame_height) == (640, 480)


def test_tag_invalid_on_untouched_plane():
    plane = np.full((480, 640), 128.0)  # never embedded
    tag = extract_tag(plane)
    assert not tag.valid


def test_header_tiling_and_confidence_weighted_recovery():
    W, H = 1280, 720
    session_id = b"x" * 16
    header = GlobalHeader(
        session_id=session_id,
        frame_width=W,
        frame_height=H,
        original_size=999,
        archive_checksum=b"z" * 32,
        kdf_salt=b"k" * 16,
        aead_nonce_prefix=b"n" * 8,
    )
    header_bytes = header.pack()
    cap = header_capacity_per_frame(W, H)
    n_frames = 4
    tiled = tile_header_bits(header_bytes, cap * n_frames)

    plane = np.full((H, W), 128.0)
    full_cap = HEADER_MODULATION.capacity_blocks(W, H)
    all_bits, all_conf = [], []
    for i in range(n_frames):
        chunk = tiled[i * cap : (i + 1) * cap]
        full_bits = scatter_logical_bits(chunk, W, H, HEADER_MODULATION.block_size, full_cap)
        embedded = HEADER_MODULATION.embed(plane, full_bits)
        embedded_u8 = np.clip(np.round(embedded), 0, 255).astype(np.uint8).astype(np.float64)
        fb, fc = HEADER_MODULATION.extract(embedded_u8)
        lb, lc = gather_logical_bits(fb, fc, W, H, HEADER_MODULATION.block_size)
        all_bits.append(lb)
        all_conf.append(lc)

    result = recover_header_bits(np.concatenate(all_bits), np.concatenate(all_conf))
    assert result.crc_ok
    recovered = GlobalHeader.unpack(result.header_bytes)
    assert recovered.original_size == 999
    assert recovered.frame_width == W

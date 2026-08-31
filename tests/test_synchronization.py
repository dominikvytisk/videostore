import numpy as np

from videostore.container.format import GlobalHeader
from videostore.framing.layout import (
    HEADER_MODULATION,
    HEADER_MODULATION_STEALTH,
    header_capacity_per_frame,
    recover_header_bits,
    tile_header_bits,
)
from videostore.framing.regions import gather_logical_bits, scatter_logical_bits
from videostore.synchronization.frame_tag import (
    TAG_MODULATION_STEALTH,
    TAG_MODULATION_SYNTHETIC,
    embed_tag,
    extract_tag,
    session_tag_from_id,
)


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


def test_stealth_tag_roundtrip_on_non_flat_plane():
    """The stealth (cover-video) tag variant must still decode correctly
    against real, non-flat content -- not just the flat plane the synthetic
    variant is always used against."""
    rng = np.random.default_rng(5)
    plane = np.clip(rng.normal(128, 35, (480, 640)), 0, 255)
    tagged = embed_tag(
        plane, frame_index=7, session_tag=0xCAFE, frame_width=640, frame_height=480, modulation=TAG_MODULATION_STEALTH
    )
    tagged_u8 = np.clip(np.round(tagged), 0, 255).astype(np.uint8).astype(np.float64)
    tag = extract_tag(tagged_u8, modulation=TAG_MODULATION_STEALTH)
    assert tag.valid
    assert tag.frame_index == 7
    assert tag.session_tag == 0xCAFE


def test_synthetic_reader_decodes_stealth_embedded_tag():
    """The decoder never needs to know which embed-time constant was used
    (see decoder/pipeline.py's module docstring): a block-mean-difference
    scheme's bit decision is sign(top_mean - bottom_mean), which doesn't
    depend on the margin/masking config that embedded it -- only on
    block_size, which is identical (16) for both variants. So reading a
    stealth-embedded tag with the plain synthetic constant must still work;
    this is what lets the decoder skip guessing which mode was used."""
    plane = np.full((480, 640), 128.0)
    tagged = embed_tag(
        plane, frame_index=1, session_tag=0x1234, frame_width=640, frame_height=480, modulation=TAG_MODULATION_STEALTH
    )
    tagged_u8 = np.clip(np.round(tagged), 0, 255).astype(np.uint8).astype(np.float64)
    tag = extract_tag(tagged_u8, modulation=TAG_MODULATION_SYNTHETIC)
    assert tag.valid
    assert tag.frame_index == 1
    assert tag.session_tag == 0x1234


def test_stealth_header_roundtrip_on_non_flat_plane():
    """Regression backstop for Phase 3: HEADER_MODULATION (synthetic) itself
    is untouched (see test_header_tiling_and_confidence_weighted_recovery
    above, still passing unmodified), and HEADER_MODULATION_STEALTH must
    additionally recover cleanly against real content."""
    W, H = 640, 480
    session_id = b"y" * 16
    header = GlobalHeader(
        session_id=session_id,
        frame_width=W,
        frame_height=H,
        original_size=555,
        archive_checksum=b"q" * 32,
        kdf_salt=b"k" * 16,
        aead_nonce_prefix=b"n" * 8,
    )
    header_bytes = header.pack()
    cap = header_capacity_per_frame(W, H)
    n_frames = 4
    tiled = tile_header_bits(header_bytes, cap * n_frames)

    rng = np.random.default_rng(9)
    plane = np.clip(rng.normal(128, 30, (H, W)), 0, 255)
    full_cap = HEADER_MODULATION_STEALTH.capacity_blocks(W, H)
    all_bits, all_conf = [], []
    for i in range(n_frames):
        chunk = tiled[i * cap : (i + 1) * cap]
        full_bits = scatter_logical_bits(chunk, W, H, HEADER_MODULATION_STEALTH.block_size, full_cap)
        embedded = HEADER_MODULATION_STEALTH.embed(plane, full_bits)
        embedded_u8 = np.clip(np.round(embedded), 0, 255).astype(np.uint8).astype(np.float64)
        fb, fc = HEADER_MODULATION_STEALTH.extract(embedded_u8)
        lb, lc = gather_logical_bits(fb, fc, W, H, HEADER_MODULATION_STEALTH.block_size)
        all_bits.append(lb)
        all_conf.append(lc)

    result = recover_header_bits(np.concatenate(all_bits), np.concatenate(all_conf))
    assert result.crc_ok
    recovered = GlobalHeader.unpack(result.header_bytes)
    assert recovered.original_size == 555
    assert recovered.frame_width == W


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

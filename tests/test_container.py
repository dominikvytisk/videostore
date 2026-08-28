import pytest

from videostore.container.format import GlobalHeader, HEADER_LEN


def test_header_roundtrip():
    h = GlobalHeader(
        original_size=123456,
        compressed_size=100000,
        encrypted_size=100016,
        session_id=b"s" * 16,
        kdf_salt=b"k" * 16,
        aead_nonce_prefix=b"n" * 8,
        archive_checksum=b"c" * 32,
        frame_width=1920,
        frame_height=1080,
        total_frames=500,
    )
    packed = h.pack()
    assert len(packed) == HEADER_LEN
    h2 = GlobalHeader.unpack(packed)
    assert h2.original_size == 123456
    assert h2.frame_width == 1920
    assert h2.session_id == b"s" * 16
    assert h2.archive_checksum == b"c" * 32


def test_header_rejects_corrupted_checksum():
    h = GlobalHeader(original_size=1)
    packed = bytearray(h.pack())
    packed[10] ^= 0xFF
    with pytest.raises(ValueError):
        GlobalHeader.unpack(bytes(packed))


def test_header_rejects_bad_magic():
    h = GlobalHeader()
    packed = bytearray(h.pack())
    packed[0:4] = b"XXXX"
    with pytest.raises(ValueError):
        GlobalHeader.unpack(bytes(packed))

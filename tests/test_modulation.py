import numpy as np
import pytest

from videostore.modulation import DCTPairModulation, LuminanceBlockModulation


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        (LuminanceBlockModulation, dict(block_size=16, margin=32.0)),
        (DCTPairModulation, dict(block_size=8, margin=16.0)),
    ],
)
def test_embed_extract_lossless_roundtrip(cls, kwargs):
    mod = cls(**kwargs)
    rng = np.random.default_rng(3)
    h, w = 240, 320
    plane = np.full((h, w), 128.0)
    n = mod.capacity_blocks(w, h)
    bits = rng.integers(0, 2, n).astype(np.uint8)

    embedded = mod.embed(plane, bits)
    embedded_u8 = np.clip(np.round(embedded), 0, 255).astype(np.uint8).astype(np.float64)
    got_bits, confidence = mod.extract(embedded_u8)

    assert np.array_equal(got_bits, bits)
    assert confidence.shape == bits.shape
    assert np.all((confidence >= 0) & (confidence <= 1))


def test_non_multiple_resolution_crops_to_usable_region():
    # 1080 is not a multiple of 32 — must not raise, and capacity must be based
    # on the cropped usable region rather than failing outright.
    mod = LuminanceBlockModulation(block_size=32, margin=20.0)
    n = mod.capacity_blocks(1920, 1080)
    assert n == (1920 // 32) * (1080 // 32)

    plane = np.full((1080, 1920), 128.0)
    bits = np.zeros(n, dtype=np.uint8)
    out = mod.embed(plane, bits)
    assert out.shape == (1080, 1920)


def test_bit_polarity_reflected_in_sign():
    mod = LuminanceBlockModulation(block_size=16, margin=40.0)
    plane = np.full((16, 16), 128.0)
    zero = mod.embed(plane, np.array([0], dtype=np.uint8))
    one = mod.embed(plane, np.array([1], dtype=np.uint8))
    top_zero = zero[:8].mean()
    bottom_zero = zero[8:].mean()
    top_one = one[:8].mean()
    bottom_one = one[8:].mean()
    assert (top_zero - bottom_zero) < 0
    assert (top_one - bottom_one) > 0

import numpy as np
import pytest

from videostore.modulation import DCTPairModulation, LuminanceBlockModulation, PerceptualMaskedModulation
from videostore.video.dct import blocks_from_plane


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        (LuminanceBlockModulation, dict(block_size=16, margin=32.0)),
        (DCTPairModulation, dict(block_size=8, margin=16.0)),
        (PerceptualMaskedModulation, dict(block_size=16, margin=48.0)),
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


def test_masked_modulation_lower_delta_in_flat_regions():
    """Constructive proof of the masking behavior (not just bit correctness):
    a block-diagonal plane with a flat half and a high-contrast noisy half
    should get a measurably smaller push in the flat half."""
    block_size = 16
    blocks_per_side = 4
    size = block_size * blocks_per_side
    rng = np.random.default_rng(7)
    plane = np.zeros((size, size))
    for by in range(blocks_per_side):
        for bx in range(blocks_per_side):
            y0, x0 = by * block_size, bx * block_size
            if bx < blocks_per_side // 2:
                plane[y0 : y0 + block_size, x0 : x0 + block_size] = 128.0
            else:
                plane[y0 : y0 + block_size, x0 : x0 + block_size] = rng.normal(128, 45, (block_size, block_size))
    plane = np.clip(plane, 0, 255)

    mod = PerceptualMaskedModulation(block_size=block_size, margin=48.0, margin_floor=16.0)
    n = mod.capacity_blocks(size, size)
    bits = np.ones(n, dtype=np.uint8)
    embedded = mod.embed(plane, bits)
    delta = np.abs(embedded - plane)

    delta_blocks = blocks_from_plane(delta, block_size)
    mean_per_block = delta_blocks.mean(axis=(1, 2))
    flat_mask = np.array(
        [bx < blocks_per_side // 2 for by in range(blocks_per_side) for bx in range(blocks_per_side)]
    )
    assert mean_per_block[flat_mask].mean() < mean_per_block[~flat_mask].mean()


@pytest.mark.parametrize("spread_factor", [1, 2, 4, 8])
def test_masked_modulation_spread_factor_roundtrip(spread_factor):
    """0 BER for every spread_factor, on a textured plane -- spread_factor=1
    must reduce to exactly the single-block behavior (regression backstop for
    the refactor), and every other value must still round-trip correctly."""
    h, w = 128, 128
    rng = np.random.default_rng(13)
    gradient = np.tile(np.linspace(0, 255, w), (h, 1))
    noise = rng.normal(0, 20, (h, w))
    plane = np.clip(gradient + noise, 0, 255)

    mod = PerceptualMaskedModulation(block_size=16, margin=48.0, spread_factor=spread_factor)
    n = mod.capacity_blocks(w, h)
    assert n == mod._raw_block_count(w, h) // spread_factor
    bits = rng.integers(0, 2, n).astype(np.uint8)
    embedded = mod.embed(plane, bits)
    embedded_u8 = np.clip(np.round(embedded), 0, 255).astype(np.uint8).astype(np.float64)
    got_bits, confidence = mod.extract(embedded_u8)

    assert np.array_equal(got_bits, bits)
    assert np.all((confidence >= 0) & (confidence <= 1))


def test_masked_modulation_spread_factor_reduces_visible_delta():
    """The whole point of spreading: a higher spread_factor should produce a
    measurably SMALLER mean per-pixel delta for the same logical payload,
    since each raw block only gets local_margin/spread_factor pushed into it
    instead of the full local_margin."""
    h, w = 128, 128
    rng = np.random.default_rng(17)
    gradient = np.tile(np.linspace(0, 255, w), (h, 1))
    noise = rng.normal(0, 30, (h, w))  # enough texture that masking doesn't floor everything
    plane = np.clip(gradient + noise, 0, 255)

    deltas = {}
    for spread_factor in (1, 4):
        mod = PerceptualMaskedModulation(block_size=16, margin=48.0, spread_factor=spread_factor)
        n = mod.capacity_blocks(w, h)
        bits = rng.integers(0, 2, n).astype(np.uint8)
        embedded = mod.embed(plane, bits)
        deltas[spread_factor] = np.abs(embedded - plane).mean()

    assert deltas[4] < deltas[1]


def test_masked_modulation_roundtrip_on_textured_plane():
    """0 BER on a genuinely non-flat plane (gradient + noise), not just the
    degenerate flat case the shared parametrized test above also covers."""
    h, w = 128, 128
    rng = np.random.default_rng(11)
    gradient = np.tile(np.linspace(0, 255, w), (h, 1))
    noise = rng.normal(0, 20, (h, w))
    plane = np.clip(gradient + noise, 0, 255)

    mod = PerceptualMaskedModulation(block_size=16, margin=48.0)
    n = mod.capacity_blocks(w, h)
    bits = rng.integers(0, 2, n).astype(np.uint8)
    embedded = mod.embed(plane, bits)
    embedded_u8 = np.clip(np.round(embedded), 0, 255).astype(np.uint8).astype(np.float64)
    got_bits, confidence = mod.extract(embedded_u8)

    assert np.array_equal(got_bits, bits)
    assert np.all((confidence >= 0) & (confidence <= 1))

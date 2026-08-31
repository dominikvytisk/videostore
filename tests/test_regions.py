"""framing/regions.py: tag-region exclusion and logical-bit scatter/gather,
including the spread-spectrum-aware grouping (see
modulation/masked_luminance.py's spread_factor)."""
import numpy as np

from videostore.framing.regions import (
    gather_logical_bits,
    payload_capacity_bits,
    scatter_logical_bits,
    tag_block_indices,
)

WIDTH, HEIGHT, BLOCK_SIZE = 384, 320, 16  # tag region (256x256) covers only part of this grid


def test_spread_factor_1_matches_raw_block_exclusion():
    """spread_factor=1 must be byte-for-byte identical to the pre-spread
    behavior (a logical bit IS a raw block) -- regression backstop."""
    raw_n = (WIDTH // BLOCK_SIZE) * (HEIGHT // BLOCK_SIZE)
    raw_excluded = set(tag_block_indices(WIDTH, HEIGHT, BLOCK_SIZE).tolist())

    cap = payload_capacity_bits(WIDTH, HEIGHT, BLOCK_SIZE, raw_n, spread_factor=1)
    assert cap == raw_n - len(raw_excluded)

    logical_bits = np.ones(cap, dtype=np.uint8)
    full = scatter_logical_bits(logical_bits, WIDTH, HEIGHT, BLOCK_SIZE, raw_n, spread_factor=1)
    for i in raw_excluded:
        assert full[i] == 0
    assert full.sum() == cap  # every non-excluded slot got a 1


def test_group_excluded_if_any_raw_block_in_tag_region():
    """A logical bit group is excluded if ANY of its spread_factor raw blocks
    falls in the tag's reserved region -- not just when the whole group does."""
    spread_factor = 4
    raw_n = (WIDTH // BLOCK_SIZE) * (HEIGHT // BLOCK_SIZE)
    n_groups = raw_n // spread_factor
    raw_excluded = set(tag_block_indices(WIDTH, HEIGHT, BLOCK_SIZE).tolist())
    assert raw_excluded, "test setup expects a non-trivial tag exclusion region"

    cap = payload_capacity_bits(WIDTH, HEIGHT, BLOCK_SIZE, n_groups, spread_factor=spread_factor)
    expected_excluded_groups = {i // spread_factor for i in raw_excluded if i // spread_factor < n_groups}
    assert cap == n_groups - len(expected_excluded_groups)


def test_scatter_gather_roundtrip_with_spread_factor():
    spread_factor = 4
    raw_n = (WIDTH // BLOCK_SIZE) * (HEIGHT // BLOCK_SIZE)
    n_groups = raw_n // spread_factor
    cap = payload_capacity_bits(WIDTH, HEIGHT, BLOCK_SIZE, n_groups, spread_factor=spread_factor)

    rng = np.random.default_rng(21)
    logical_bits = rng.integers(0, 2, cap).astype(np.uint8)
    full = scatter_logical_bits(logical_bits, WIDTH, HEIGHT, BLOCK_SIZE, n_groups, spread_factor=spread_factor)
    assert full.shape == (n_groups,)

    confidence = np.ones(n_groups, dtype=np.float64)
    gathered_bits, gathered_conf = gather_logical_bits(full, confidence, WIDTH, HEIGHT, BLOCK_SIZE, spread_factor=spread_factor)
    assert np.array_equal(gathered_bits, logical_bits)
    assert gathered_conf.shape == logical_bits.shape

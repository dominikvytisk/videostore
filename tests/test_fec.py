import os

import numpy as np
import pytest

from videostore.fec import (
    RSConfig,
    decode_blocks,
    decode_file,
    deinterleave,
    deinterleave_file,
    encode_blocks,
    encode_file,
    fec_output_size,
    interleave,
    interleave_file,
    rs_config_for_redundancy,
)


def test_rs_roundtrip_no_errors():
    cfg = rs_config_for_redundancy(0.2)
    data = os.urandom(5000)
    encoded = encode_blocks(data, cfg)
    decoded, stats = decode_blocks(encoded, cfg)
    assert decoded[: len(data)] == data
    assert stats.blocks_uncorrectable == 0


def test_rs_corrects_errors_within_capacity():
    cfg = rs_config_for_redundancy(0.3)  # nsym ~ 76, corrects up to 38 errors/block
    data = os.urandom(cfg.message_len * 3)
    encoded = bytearray(encode_blocks(data, cfg))
    rng = np.random.default_rng(0)
    # corrupt 20 bytes within the first block (well under half of nsym)
    positions = rng.choice(cfg.nsize, size=20, replace=False)
    for p in positions:
        encoded[p] ^= 0xFF
    decoded, stats = decode_blocks(bytes(encoded), cfg)
    assert decoded[: len(data)] == data
    assert stats.blocks_uncorrectable == 0


def test_rs_erasure_decoding_recovers_more_errors():
    cfg = rs_config_for_redundancy(0.3)
    data = os.urandom(cfg.message_len)
    encoded = bytearray(encode_blocks(data, cfg))
    # corrupt more bytes than blind error-correction (nsym/2) could fix...
    n_corrupt = cfg.nsym - 2
    positions = list(range(n_corrupt))
    mask = bytearray(len(encoded))
    for p in positions:
        encoded[p] ^= 0xFF
        mask[p] = 1  # ...but tell the decoder exactly where, as erasures
    decoded, stats = decode_blocks(bytes(encoded), cfg, erasure_mask=bytes(mask))
    assert decoded[: len(data)] == data
    assert stats.blocks_uncorrectable == 0


def test_rs_file_streaming_matches_in_memory(tmp_path):
    cfg = rs_config_for_redundancy(0.2)
    data = os.urandom(50_000)
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    dst = tmp_path / "enc.bin"
    encode_file(str(src), str(dst), cfg)
    assert dst.read_bytes() == encode_blocks(data, cfg)


def test_fec_output_size_matches_actual_encoding():
    cfg = rs_config_for_redundancy(0.25)
    data = os.urandom(12345)
    encoded = encode_blocks(data, cfg)
    assert fec_output_size(len(data), cfg) == len(encoded)


@pytest.mark.parametrize("block_size,depth", [(255, 32), (32, 255), (255, 7)])
def test_interleave_file_matches_in_memory(tmp_path, block_size, depth):
    n_blocks = depth * 3 + 2
    data = os.urandom(n_blocks * block_size)
    ref = interleave(data, block_size, depth)

    src = tmp_path / "src.bin"
    src.write_bytes(data)
    dst = tmp_path / "il.bin"
    interleave_file(str(src), str(dst), block_size, depth)
    assert dst.read_bytes() == ref

    rt = tmp_path / "rt.bin"
    deinterleave_file(str(dst), str(rt), block_size, depth)
    assert rt.read_bytes()[: len(data)] == data

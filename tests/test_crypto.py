import os

import pytest

from videostore.crypto import KdfParams, derive_key
from videostore.crypto.aead import Algorithm, DecryptionError, decrypt_file, encrypt_file, new_nonce_prefix


def test_kdf_deterministic():
    params = KdfParams.generate(time_cost=1, memory_cost_kib=8192, parallelism=1)
    k1 = derive_key("hunter2", params)
    k2 = derive_key("hunter2", params)
    assert k1 == k2
    assert len(k1) == 32


def test_kdf_wrong_password_differs():
    params = KdfParams.generate(time_cost=1, memory_cost_kib=8192, parallelism=1)
    assert derive_key("right", params) != derive_key("wrong", params)


@pytest.mark.parametrize("algo", [Algorithm.AES256_GCM, Algorithm.CHACHA20_POLY1305])
def test_aead_roundtrip_multi_chunk(tmp_path, algo):
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(3_500_000))  # > CHUNK_SIZE, forces multiple chunks
    enc = tmp_path / "enc.bin"
    dec = tmp_path / "dec.bin"
    key = os.urandom(32)
    nonce_prefix = new_nonce_prefix()

    encrypt_file(str(src), str(enc), algo, key, nonce_prefix)
    assert enc.read_bytes() != src.read_bytes()
    decrypt_file(str(enc), str(dec), algo, key, nonce_prefix)
    assert dec.read_bytes() == src.read_bytes()


def test_aead_wrong_key_raises(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(1000))
    enc = tmp_path / "enc.bin"
    dec = tmp_path / "dec.bin"
    nonce_prefix = new_nonce_prefix()
    encrypt_file(str(src), str(enc), Algorithm.CHACHA20_POLY1305, os.urandom(32), nonce_prefix)
    with pytest.raises(DecryptionError):
        decrypt_file(str(enc), str(dec), Algorithm.CHACHA20_POLY1305, os.urandom(32), nonce_prefix)


def test_aead_best_effort_zero_fills_and_counts_failures(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(2_500_000))
    enc = tmp_path / "enc.bin"
    dec = tmp_path / "dec.bin"
    key = os.urandom(32)
    nonce_prefix = new_nonce_prefix()
    encrypt_file(str(src), str(enc), Algorithm.CHACHA20_POLY1305, key, nonce_prefix)

    result = decrypt_file(str(enc), str(dec), Algorithm.CHACHA20_POLY1305, os.urandom(32), nonce_prefix, best_effort=True)
    assert result.failed_chunks == result.total_chunks > 0

"""Argon2id password-based key derivation. The container stores everything needed
to re-derive the key (salt + cost parameters) but NEVER the password or the key
itself — see docs/security.md."""
from __future__ import annotations

from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw

KEY_LEN = 32  # 256-bit key for AES-256-GCM / ChaCha20-Poly1305
SALT_LEN = 16


@dataclass(frozen=True)
class KdfParams:
    salt: bytes
    time_cost: int = 3
    memory_cost_kib: int = 262144  # 256 MiB
    parallelism: int = 4

    @staticmethod
    def generate(*, time_cost: int = 3, memory_cost_kib: int = 262144, parallelism: int = 4) -> "KdfParams":
        import os

        return KdfParams(
            salt=os.urandom(SALT_LEN),
            time_cost=time_cost,
            memory_cost_kib=memory_cost_kib,
            parallelism=parallelism,
        )


def derive_key(password: str, params: KdfParams) -> bytes:
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=params.salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost_kib,
        parallelism=params.parallelism,
        hash_len=KEY_LEN,
        type=Type.ID,
    )

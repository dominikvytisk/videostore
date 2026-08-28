"""Bit-level packing helpers used by the modulation layer (symbols are often not
byte-aligned, e.g. 1-3 bits/symbol)."""
from __future__ import annotations

import numpy as np


def bytes_to_bits(data: bytes) -> np.ndarray:
    """Return a uint8 array of 0/1, MSB-first per byte."""
    arr = np.frombuffer(data, dtype=np.uint8)
    return np.unpackbits(arr, bitorder="big")


def bits_to_bytes(bits: np.ndarray) -> bytes:
    """Inverse of bytes_to_bits. `bits` length must be a multiple of 8 (pad with 0s
    beforehand if not)."""
    pad = (-len(bits)) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(bits, bitorder="big").tobytes()


class BitWriter:
    """Accumulates individual bits/symbols and yields whole bytes."""

    def __init__(self) -> None:
        self._bits: list[int] = []

    def write_bit(self, bit: int) -> None:
        self._bits.append(bit & 1)

    def write_bits(self, value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            self._bits.append((value >> i) & 1)

    def getvalue(self) -> bytes:
        arr = np.array(self._bits, dtype=np.uint8)
        return bits_to_bytes(arr)


class BitReader:
    def __init__(self, data: bytes) -> None:
        self._bits = bytes_to_bits(data)
        self._pos = 0

    def read_bit(self) -> int:
        b = int(self._bits[self._pos])
        self._pos += 1
        return b

    def read_bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.read_bit()
        return value

    def remaining_bits(self) -> int:
        return len(self._bits) - self._pos

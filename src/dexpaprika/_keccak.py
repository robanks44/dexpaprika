"""Vendored keccak-256 (the pre-NIST/Ethereum variant, 0x01 padding).

Why vendored: EIP-55 checksumming needs keccak-256; stdlib ``hashlib.sha3_256``
uses NIST SHA-3 padding (0x06) and produces different digests. Vendoring ~90
typed lines avoids a cryptography dependency for one hash. Correctness is
pinned by published vectors in ``tests/test_keccak.py``.

Not for secrets: this is used for public-address checksums only.
"""

from __future__ import annotations

_MASK64 = (1 << 64) - 1
_RATE_BYTES = 136  # 1088-bit rate for keccak-256

_ROUND_CONSTANTS = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)

# Rotation offsets r[x][y] from the Keccak reference.
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl(value: int, shift: int) -> int:
    if shift == 0:
        return value
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f(state: list[list[int]]) -> None:
    for round_constant in _ROUND_CONSTANTS:
        # theta
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]
        # rho + pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _ROTATIONS[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ (((b[(x + 1) % 5][y]) ^ _MASK64) & b[(x + 2) % 5][y])
        # iota
        state[0][0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte keccak-256 digest of ``data``."""
    padded = bytearray(data)
    pad_len = _RATE_BYTES - (len(padded) % _RATE_BYTES)
    if pad_len == 1:
        padded += b"\x81"
    else:
        padded += b"\x01" + b"\x00" * (pad_len - 2) + b"\x80"

    state: list[list[int]] = [[0] * 5 for _ in range(5)]
    for block_start in range(0, len(padded), _RATE_BYTES):
        block = padded[block_start : block_start + _RATE_BYTES]
        for i in range(_RATE_BYTES // 8):
            lane = int.from_bytes(block[8 * i : 8 * i + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)

    out = bytearray()
    for i in range(4):  # 4 lanes * 8 bytes = 32 bytes, all within the rate
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)

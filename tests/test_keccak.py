"""Vendored keccak-256 — pinned to published test vectors.

EIP-55 checksumming depends on keccak-256 (NOT NIST sha3_256 — different
padding). A silent hash bug would corrupt every EVM address normalization,
so the primitive is vector-pinned and property-tested.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dexpaprika._keccak import keccak256

# Published keccak-256 vectors (pre-NIST padding — the Ethereum variant).
KNOWN_VECTORS = [
    (
        b"",
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
    ),
    (
        b"abc",
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
    ),
    (
        b"The quick brown fox jumps over the lazy dog",
        "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15",
    ),
]


def test_known_vectors() -> None:
    for message, expected_hex in KNOWN_VECTORS:
        assert keccak256(message).hex() == expected_hex, message


def test_empty_input_is_not_sha3_256() -> None:
    # NIST SHA3-256("") = a7ffc6f8bf1ed766... — keccak256 must NOT equal it.
    assert keccak256(b"").hex() != (
        "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"
    )


@given(st.binary(max_size=512))
def test_digest_is_32_bytes_and_deterministic(data: bytes) -> None:
    digest = keccak256(data)
    assert len(digest) == 32
    assert digest == keccak256(data)


@given(st.binary(min_size=1, max_size=256))
def test_single_bit_flip_changes_digest(data: bytes) -> None:
    flipped = bytes([data[0] ^ 0x01]) + data[1:]
    assert keccak256(flipped) != keccak256(data)


def test_multiblock_input() -> None:
    # > rate (136 bytes) forces multi-block absorption.
    data = b"a" * 200
    digest = keccak256(data)
    assert len(digest) == 32
    assert digest != keccak256(b"a" * 199)

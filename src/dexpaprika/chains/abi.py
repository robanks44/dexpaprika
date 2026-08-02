"""Minimal ABI helpers (vendored — see S4.5 ADR: patterns over web3py dep).

Selectors come from the vendored keccak; encodings are pinned byte-for-byte
to calldata that executed live on Base/Arbitrum (probes/out/s45).
"""

from __future__ import annotations

from dexpaprika._keccak import keccak256

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"  # same address on Base + Arbitrum
ARBSYS = "0x0000000000000000000000000000000000000064"  # Arbitrum precompile
CHAIN_IDS = {"base": 8453, "arbitrum": 42161}

_WORD = 32


def selector(signature: str) -> str:
    """4-byte function selector, ``0x``-prefixed."""
    return "0x" + keccak256(signature.encode("ascii")).hex()[:8]


def encode_uint(value: int) -> str:
    """One 256-bit ABI word as 64 hex chars (no 0x)."""
    return f"{value:064x}"


def decode_uint(data: bytes) -> int:
    """First 256-bit word as unsigned int."""
    return int.from_bytes(data[:_WORD], "big")


def decode_address(data: bytes) -> str:
    """Address from the low 20 bytes of the first word."""
    return "0x" + data[:_WORD][-20:].hex()


def sign_extend(value: int, bits: int) -> int:
    """Two's-complement sign extension (int24 ticks live in 256-bit words)."""
    if value >= 1 << (bits - 1):
        return value - (1 << bits)
    return value


def encode_call_aggregate(calls: list[tuple[str, str]]) -> str:
    """Calldata for Multicall3 ``aggregate((address,bytes)[])``.

    Pinned byte-for-byte to the live probe calldata (test suite).
    """
    head = encode_uint(0x20)  # offset to the array
    array_len = encode_uint(len(calls))
    tuple_offsets: list[str] = []
    tuple_bodies: list[str] = []
    running = _WORD * len(calls)
    for to, data in calls:
        data_hex = data[2:] if data.startswith("0x") else data
        data_bytes = bytes.fromhex(data_hex)
        padded = data_bytes.hex() + "00" * ((_WORD - len(data_bytes) % _WORD) % _WORD)
        body = encode_uint(int(to, 16)) + encode_uint(0x40) + encode_uint(len(data_bytes)) + padded
        tuple_offsets.append(encode_uint(running))
        tuple_bodies.append(body)
        running += len(body) // 2
    return "0x252dba42" + head + array_len + "".join(tuple_offsets) + "".join(tuple_bodies)


def decode_aggregate(hexstr: str) -> tuple[int, list[bytes]]:
    """Decode ``aggregate`` return: (blockNumber, returnData[])."""
    blob = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
    block_number = int.from_bytes(blob[0:_WORD], "big")
    array_offset = int.from_bytes(blob[_WORD : 2 * _WORD], "big")
    count = int.from_bytes(blob[array_offset : array_offset + _WORD], "big")
    outputs: list[bytes] = []
    for i in range(count):
        offset_word = blob[array_offset + _WORD + _WORD * i : array_offset + 2 * _WORD + _WORD * i]
        element_offset = array_offset + _WORD + int.from_bytes(offset_word, "big")
        length = int.from_bytes(blob[element_offset : element_offset + _WORD], "big")
        outputs.append(blob[element_offset + _WORD : element_offset + _WORD + length])
    return block_number, outputs

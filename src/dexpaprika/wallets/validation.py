"""Address validation + normalization at the boundary (parse, don't validate-later).

Families: ``evm`` (EIP-55), ``btc`` (Base58Check + bech32/bech32m per
BIP-173/350), ``solana`` (base58, 32 bytes). Every validator either returns
the NORMALIZED address or raises :class:`AddressValidationError` with a
reason. Correctness is pinned by published vectors in the test suite.
"""

from __future__ import annotations

import hashlib

from dexpaprika._keccak import keccak256

CHAIN_FAMILIES = ("evm", "btc", "solana")

_HEX_DIGITS = set("0123456789abcdefABCDEF")
_BASE58_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"  # pragma: allowlist secret
)
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"  # pragma: allowlist secret
_BECH32M_CONST = 0x2BC830A3


class AddressValidationError(ValueError):
    """The supplied address is not valid for its chain family."""


def validate_address(chain_family: str, address: str) -> str:
    """Validate ``address`` for ``chain_family``; return the normalized form."""
    if chain_family == "evm":
        return _validate_evm(address)
    if chain_family == "btc":
        return _validate_btc(address)
    if chain_family == "solana":
        return _validate_solana(address)
    msg = f"unknown chain family {chain_family!r}: expected one of {', '.join(CHAIN_FAMILIES)}"
    raise AddressValidationError(msg)


# ----------------------------- EVM (EIP-55) -----------------------------


def _eip55_checksum(lower_body: str) -> str:
    digest = keccak256(lower_body.encode("ascii")).hex()
    chars = [
        ch.upper() if ch.isalpha() and int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(lower_body)
    ]
    return "0x" + "".join(chars)


def _validate_evm(address: str) -> str:
    if not address.startswith("0x"):
        msg = f"EVM address must start with '0x': {address!r}"
        raise AddressValidationError(msg)
    body = address[2:]
    if len(body) != 40:
        msg = f"EVM address must have 40 hex characters, got {len(body)}: {address!r}"
        raise AddressValidationError(msg)
    if not set(body) <= _HEX_DIGITS:
        msg = f"EVM address contains non-hex characters: {address!r}"
        raise AddressValidationError(msg)
    checksummed = _eip55_checksum(body.lower())
    if body in (body.lower(), body.upper()):
        return checksummed  # caseless input: normalize to EIP-55
    if address != checksummed:
        msg = f"EVM address fails EIP-55 checksum: {address!r} (expected {checksummed!r})"
        raise AddressValidationError(msg)
    return checksummed


# ------------------------- BTC (Base58Check + bech32) -------------------------


def _base58_decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _BASE58_ALPHABET.find(char)
        if index < 0:
            msg = f"invalid base58 character {char!r}"
            raise AddressValidationError(msg)
        number = number * 58 + index
    leading_zeros = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading_zeros + body


def _validate_base58check(address: str) -> str:
    decoded = _base58_decode(address)
    if len(decoded) != 25:
        msg = f"Base58Check BTC address must decode to 25 bytes, got {len(decoded)}: {address!r}"
        raise AddressValidationError(msg)
    payload, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        msg = f"BTC address checksum mismatch: {address!r}"
        raise AddressValidationError(msg)
    if payload[0] not in (0x00, 0x05):
        msg = f"unknown BTC address version byte {payload[0]:#x}: {address!r}"
        raise AddressValidationError(msg)
    return address


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            checksum ^= generator[i] if ((top >> i) & 1) else 0
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: list[int], from_bits: int, to_bits: int) -> list[int]:
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in data:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        msg = "invalid padding in bech32 data section"
        raise AddressValidationError(msg)
    return result


def _validate_bech32(address: str) -> str:
    if address not in (address.lower(), address.upper()):
        msg = f"bech32 BTC address must not mix cases: {address!r}"
        raise AddressValidationError(msg)
    lowered = address.lower()
    separator = lowered.rfind("1")
    hrp, data_part = lowered[:separator], lowered[separator + 1 :]
    if hrp != "bc":
        msg = f"BTC bech32 address must use hrp 'bc': {address!r}"
        raise AddressValidationError(msg)
    if len(data_part) < 7:
        msg = f"bech32 data section too short: {address!r}"
        raise AddressValidationError(msg)
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError:
        msg = f"invalid bech32 character in {address!r}"
        raise AddressValidationError(msg) from None
    constant = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    witness_version = data[0]
    program = _convert_bits(data[1:-6], 5, 8)
    if witness_version == 0:
        if constant != 1:
            msg = f"bech32 checksum mismatch: {address!r}"
            raise AddressValidationError(msg)
        if len(program) not in (20, 32):
            msg = f"segwit v0 program must be 20 or 32 bytes, got {len(program)}: {address!r}"
            raise AddressValidationError(msg)
    elif witness_version == 1:
        if constant != _BECH32M_CONST:
            msg = f"bech32m checksum mismatch: {address!r}"
            raise AddressValidationError(msg)
        if len(program) != 32:
            msg = f"taproot (v1) program must be 32 bytes, got {len(program)}: {address!r}"
            raise AddressValidationError(msg)
    else:
        msg = f"unsupported witness version {witness_version}: {address!r}"
        raise AddressValidationError(msg)
    return lowered


def _validate_btc(address: str) -> str:
    if not address:
        msg = "BTC address is empty"
        raise AddressValidationError(msg)
    if address.lower().startswith("bc1"):
        return _validate_bech32(address)
    # Everything else must be Base58Check — testnet/other version bytes get a
    # clear rejection there rather than a vague "unrecognized format".
    return _validate_base58check(address)


# ------------------------------- Solana -------------------------------


def _validate_solana(address: str) -> str:
    if not address:
        msg = "Solana address is empty"
        raise AddressValidationError(msg)
    decoded = _base58_decode(address)
    if len(decoded) != 32:
        msg = f"Solana address must decode to 32 bytes, got {len(decoded)}: {address!r}"
        raise AddressValidationError(msg)
    return address

"""Address validation — pinned to published vectors per family."""

from __future__ import annotations

import pytest

from dexpaprika.wallets.validation import (
    AddressValidationError,
    validate_address,
)

# Richard's live wallet (public; VERIFIED_FINDINGS §1) — already EIP-55 form.
RICHARD_EVM = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"

# EIP-55 spec examples.
EIP55_VALID = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


class TestEvm:
    def test_richards_wallet_valid(self) -> None:
        assert validate_address("evm", RICHARD_EVM) == RICHARD_EVM

    @pytest.mark.parametrize("address", EIP55_VALID)
    def test_eip55_spec_examples_valid(self, address: str) -> None:
        assert validate_address("evm", address) == address

    def test_all_lowercase_normalizes_to_checksum(self) -> None:
        assert validate_address("evm", RICHARD_EVM.lower()) == RICHARD_EVM

    def test_all_uppercase_hex_normalizes_to_checksum(self) -> None:
        addr = "0x" + RICHARD_EVM[2:].upper()
        assert validate_address("evm", addr) == RICHARD_EVM

    def test_bad_mixed_case_checksum_rejected(self) -> None:
        # Flip the case of one alphabetic hex digit → checksum breach.
        bad = "0xc155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
        with pytest.raises(AddressValidationError, match="checksum"):
            validate_address("evm", bad)

    @pytest.mark.parametrize(
        "address",
        [
            "C155A616e39D7B83E37e8FD9d2106E1BC056d7Fe",  # missing 0x
            "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7F",  # 39 hex chars
            "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe0",  # 41 hex chars
            "0xG155A616e39D7B83E37e8FD9d2106E1BC056d7Fe",  # non-hex
            "",
        ],
    )
    def test_malformed_rejected(self, address: str) -> None:
        with pytest.raises(AddressValidationError):
            validate_address("evm", address)


class TestBtc:
    @pytest.mark.parametrize(
        "address",
        [
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",  # genesis P2PKH
            "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",  # P2SH
        ],
    )
    def test_base58check_valid(self, address: str) -> None:
        assert validate_address("btc", address) == address

    def test_bech32_v0_valid_and_lowercased(self) -> None:
        addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP-173 vector
        assert validate_address("btc", addr) == addr
        # Uppercase form is valid bech32 and normalizes to lowercase.
        assert validate_address("btc", addr.upper()) == addr

    def test_bech32m_v1_taproot_valid(self) -> None:
        addr = "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297"
        assert validate_address("btc", addr) == addr

    @pytest.mark.parametrize(
        "address",
        [
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",  # bad base58check checksum
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",  # bad bech32 checksum
            "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3298",  # bad bech32m
            "bc1Qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # mixed case forbidden
            "bc2qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # wrong hrp
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfN",  # truncated
            "",
        ],
    )
    def test_invalid_rejected(self, address: str) -> None:
        with pytest.raises(AddressValidationError):
            validate_address("btc", address)


class TestSolana:
    @pytest.mark.parametrize(
        "address",
        [
            "11111111111111111111111111111111",  # system program (32 zero bytes)
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL token program
        ],
    )
    def test_known_program_ids_valid(self, address: str) -> None:
        assert validate_address("solana", address) == address

    @pytest.mark.parametrize(
        "address",
        [
            "abc",  # too short (decodes to < 32 bytes)
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5D0",  # 0 not in base58 alphabet
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DAaa",  # decodes to > 32 bytes
            "",
        ],
    )
    def test_invalid_rejected(self, address: str) -> None:
        with pytest.raises(AddressValidationError):
            validate_address("solana", address)


def test_unknown_chain_family_rejected() -> None:
    with pytest.raises(AddressValidationError, match="chain"):
        validate_address("dogecoin", "D...")  # boundary test of an invalid family string


class TestBtcErrorPaths:
    """Error-path coverage for the base58check/bech32 branches (core-logic gate)."""

    def test_testnet_version_byte_rejected(self) -> None:
        # Valid Base58Check, but version 0x6f (testnet P2PKH) — not a mainnet wallet.
        testnet = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn"  # pragma: allowlist secret
        with pytest.raises(AddressValidationError, match="version"):
            validate_address("btc", testnet)

    def test_bech32_data_section_too_short(self) -> None:
        with pytest.raises(AddressValidationError, match=r"short"):
            validate_address("btc", "bc1qqq")

    def test_bech32_invalid_charset_character(self) -> None:
        # 'b' is not in the bech32 charset.
        with pytest.raises(AddressValidationError, match="character"):
            validate_address("btc", "bc1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_unsupported_witness_version_rejected(self) -> None:
        # BIP-173 valid vector with witness version 16 ("bc1sw50...") — checksum
        # valid but outside the v0/v1 set this registry accepts.
        with pytest.raises(AddressValidationError, match="witness version"):
            validate_address("btc", "bc1sw50qgdz25j")

    def test_segwit_v0_wrong_program_length_rejected(self) -> None:
        # BIP-173 invalid-vector class: v0 with a program that is not 20/32 bytes.
        with pytest.raises(AddressValidationError):
            validate_address("btc", "bc1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqv8dvw0")


class TestSolanaErrorPaths:
    def test_leading_ones_shorter_than_32_bytes(self) -> None:
        with pytest.raises(AddressValidationError, match="32 bytes"):
            validate_address("solana", "1" * 31)  # 31 zero bytes only

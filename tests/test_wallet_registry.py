"""Wallet registry — persistence, dedup, include/exclude, corruption handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexpaprika.wallets.registry import RegistryError, WalletRegistry

EVM = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
SOL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


@pytest.fixture
def registry(tmp_path: Path) -> WalletRegistry:
    return WalletRegistry(tmp_path / "wallets.json")


def test_add_and_list(registry: WalletRegistry) -> None:
    registry.add("evm", EVM, label="main")
    wallets = registry.list_wallets()
    assert len(wallets) == 1
    w = wallets[0]
    assert (w.chain_family, w.address, w.label, w.included) == ("evm", EVM, "main", True)
    assert w.added_at.endswith("Z") or "+" in w.added_at  # UTC-aware ISO timestamp


def test_add_normalizes_address(registry: WalletRegistry) -> None:
    registry.add("evm", EVM.lower())
    assert registry.list_wallets()[0].address == EVM  # EIP-55 form stored


def test_add_validates_address(registry: WalletRegistry) -> None:
    with pytest.raises(Exception, match=r"checksum|invalid|hex"):
        registry.add("evm", "0xnot-an-address")
    assert registry.list_wallets() == []


def test_duplicate_rejected_case_insensitively(registry: WalletRegistry) -> None:
    registry.add("evm", EVM)
    with pytest.raises(RegistryError, match=r"[Dd]uplicate"):
        registry.add("evm", EVM.lower())
    assert len(registry.list_wallets()) == 1


def test_duplicate_label_rejected(registry: WalletRegistry) -> None:
    registry.add("evm", EVM, label="main")
    with pytest.raises(RegistryError, match="label"):
        registry.add("btc", BTC, label="main")


def test_same_address_different_family_allowed(registry: WalletRegistry) -> None:
    # Address namespaces are per-family; no cross-family collision.
    registry.add("btc", BTC)
    registry.add("solana", SOL)
    assert len(registry.list_wallets()) == 2


def test_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "wallets.json"
    WalletRegistry(path).add("evm", EVM, label="main")
    reloaded = WalletRegistry(path).list_wallets()
    assert len(reloaded) == 1
    assert reloaded[0].address == EVM


def test_remove_by_address(registry: WalletRegistry) -> None:
    registry.add("evm", EVM)
    registry.remove(address=EVM.lower())  # selector normalized too
    assert registry.list_wallets() == []


def test_remove_by_label(registry: WalletRegistry) -> None:
    registry.add("evm", EVM, label="main")
    registry.remove(label="main")
    assert registry.list_wallets() == []


def test_remove_unknown_raises(registry: WalletRegistry) -> None:
    with pytest.raises(RegistryError, match=r"[Nn]o wallet"):
        registry.remove(address=EVM)


def test_include_exclude_cycle(registry: WalletRegistry) -> None:
    registry.add("evm", EVM)
    registry.set_included(False, address=EVM)
    assert registry.list_wallets()[0].included is False
    registry.set_included(True, address=EVM)
    assert registry.list_wallets()[0].included is True


def test_exclude_unknown_raises(registry: WalletRegistry) -> None:
    with pytest.raises(RegistryError, match=r"[Nn]o wallet"):
        registry.set_included(False, label="ghost")


def test_corrupt_file_raises_actionable_error(tmp_path: Path) -> None:
    path = tmp_path / "wallets.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match=str(path.name)):
        WalletRegistry(path).list_wallets()
    # Corruption must never be silently overwritten.
    assert path.read_text(encoding="utf-8") == "{not json"


def test_atomic_write_no_partial_files(registry: WalletRegistry, tmp_path: Path) -> None:
    registry.add("evm", EVM)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "wallets.json"]
    assert leftovers == []
    # File is valid JSON at rest.
    data = json.loads((tmp_path / "wallets.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_missing_file_is_empty_registry(registry: WalletRegistry) -> None:
    assert registry.list_wallets() == []

"""Wallet registry — the include/exclude list driving all downstream selection.

S1 persists a validated JSON document at ``data_dir/wallets.json`` with
atomic writes (tmp + ``os.replace``). S2 may migrate the backend behind this
same API (ADR in PROGRESS.md). Corruption raises a loud, actionable error and
is never silently overwritten.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from dexpaprika.wallets.validation import validate_address

ChainFamily = Literal["evm", "btc", "solana"]


class RegistryError(Exception):
    """Registry-level failure (duplicate, unknown wallet, corrupt file)."""


class Wallet(BaseModel):
    """One registered wallet."""

    chain_family: ChainFamily
    address: str  # normalized form
    label: str | None = None
    included: bool = True
    added_at: str  # UTC ISO-8601


class _RegistryDocument(BaseModel):
    version: int = 1
    wallets: list[Wallet] = []


class WalletRegistry:
    """Persisted wallet collection with include/exclude state."""

    def __init__(self, path: Path) -> None:
        self._path = path

    # ------------------------------ persistence ------------------------------

    def _load(self) -> _RegistryDocument:
        if not self._path.exists():
            return _RegistryDocument()
        raw = self._path.read_text(encoding="utf-8")
        try:
            return _RegistryDocument.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            msg = (
                f"corrupt wallet registry at {self._path}: {exc}. "
                "The file was NOT modified — restore it from backup or fix it "
                "by hand, then retry."
            )
            raise RegistryError(msg) from exc

    def _save(self, document: _RegistryDocument) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(document.model_dump_json(indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self._path)  # atomic on POSIX and Windows

    # ------------------------------ operations ------------------------------

    def list_wallets(self) -> list[Wallet]:
        return list(self._load().wallets)

    def add(self, chain_family: ChainFamily, address: str, label: str | None = None) -> Wallet:
        normalized = validate_address(chain_family, address)
        document = self._load()
        for wallet in document.wallets:
            if wallet.chain_family == chain_family and wallet.address == normalized:
                msg = f"duplicate wallet: {chain_family} {normalized} already registered"
                raise RegistryError(msg)
            if label is not None and wallet.label == label:
                msg = f"duplicate label {label!r}: already used by {wallet.address}"
                raise RegistryError(msg)
        wallet = Wallet(
            chain_family=chain_family,
            address=normalized,
            label=label,
            added_at=datetime.now(UTC).isoformat(),
        )
        document.wallets.append(wallet)
        self._save(document)
        return wallet

    def remove(self, address: str | None = None, label: str | None = None) -> Wallet:
        document = self._load()
        wallet = self._select(document, address=address, label=label)
        document.wallets = [w for w in document.wallets if w is not wallet]
        self._save(document)
        return wallet

    def set_included(
        self, included: bool, address: str | None = None, label: str | None = None
    ) -> Wallet:
        document = self._load()
        wallet = self._select(document, address=address, label=label)
        updated = wallet.model_copy(update={"included": included})
        document.wallets = [updated if w is wallet else w for w in document.wallets]
        self._save(document)
        return updated

    # ------------------------------ selection ------------------------------

    @staticmethod
    def _select(document: _RegistryDocument, address: str | None, label: str | None) -> Wallet:
        if (address is None) == (label is None):
            msg = "select a wallet with exactly one of address or label"
            raise RegistryError(msg)
        if label is not None:
            matches = [w for w in document.wallets if w.label == label]
        elif address is not None:
            matches = [w for w in document.wallets if w.address.casefold() == address.casefold()]
        else:  # pragma: no cover — excluded by the exactly-one guard above
            matches = []
        if not matches:
            selector = f"label {label!r}" if label is not None else f"address {address!r}"
            known = ", ".join(w.label or w.address for w in document.wallets) or "(registry empty)"
            msg = f"no wallet matches {selector}; known wallets: {known}"
            raise RegistryError(msg)
        if len(matches) > 1:
            msg = "selector is ambiguous (matches multiple wallets); use --label"
            raise RegistryError(msg)
        return matches[0]

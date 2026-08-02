"""Native-BTC holdings client (S5.5) — Esplora peer ring.

Reference: ``bitcoin--integration-guide.md`` — Blockstream Esplora primary,
mempool.space fallback (same API shape); balance =
``chain_stats.funded_txo_sum - spent_txo_sum`` in satoshis; pending adds
the ``mempool_stats`` delta. Probe-verified against the real wallet
(``probes/out/s55/address_stats.json``).

Scope: balance tracking only (holdings group). No tx history, no UTXOs,
no BRC-20/Runes — the reference doc's own native-BTC-first recommendation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel

from dexpaprika.clients.base import HttpTransport, Sleeper, TransportError
from dexpaprika.config import Settings

_SATS_PER_BTC = Decimal(10) ** 8
VENUE = "native"
CHAIN = "bitcoin"


def _provider_for(peer_url: str) -> str:
    return "blockstream" if "blockstream" in peer_url else "mempool"


class BtcAddressStats(BaseModel):
    """One address's Esplora stats, sats exact, BTC as exact Decimal."""

    address: str
    confirmed_sats: int
    pending_sats: int
    tx_count: int
    unconfirmed_tx_count: int
    source: str  # which peer served the read

    @property
    def balance_btc(self) -> Decimal:
        """Confirmed balance in BTC — sats are ≤ 2.1e15, exact at any prec."""
        return Decimal(self.confirmed_sats) / _SATS_PER_BTC


class BtcClient:
    """Peer-rotating Esplora reads + holdings recording."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        clients: list[httpx.Client] | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._conn = conn
        cfg = settings or Settings.load()
        self._peers = list(cfg.btc_esplora_peers)
        http_clients = clients or [
            httpx.Client(base_url=peer, timeout=30.0) for peer in self._peers
        ]
        self._transports = [
            HttpTransport(
                base_url=peer,
                provider=_provider_for(peer),
                conn=conn,
                client=http_client,
                sleeper=sleeper or time.sleep,
            )
            for peer, http_client in zip(self._peers, http_clients, strict=False)
        ]

    def _get_json(self, path: str) -> tuple[Any, str]:
        errors: list[str] = []
        for peer, transport in zip(self._peers, self._transports, strict=False):
            try:
                return transport.get_json(path), peer
            except TransportError as exc:
                errors.append(f"{peer}: {exc}")
        msg = "all BTC Esplora peers failed — " + " | ".join(errors)
        raise TransportError(msg)

    def get_address(self, address: str) -> BtcAddressStats:
        payload, peer = self._get_json(f"/address/{address}")
        chain = payload["chain_stats"]
        mempool = payload["mempool_stats"]
        confirmed = int(chain["funded_txo_sum"]) - int(chain["spent_txo_sum"])
        pending = confirmed + int(mempool["funded_txo_sum"]) - int(mempool["spent_txo_sum"])
        return BtcAddressStats(
            address=address,
            confirmed_sats=confirmed,
            pending_sats=pending,
            tx_count=int(chain["tx_count"]),
            unconfirmed_tx_count=int(mempool["tx_count"]),
            source=peer,
        )

    def record(self, wallet: str, stats: BtcAddressStats, ts: str) -> None:
        """Holdings-group upsert + observed event (same lifecycle pipeline)."""
        self._conn.execute(
            "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag,"
            " opened_at) VALUES (?, ?, ?, 'holding', 'btc', 'holdings', ?)"
            " ON CONFLICT(wallet_ref, venue, chain, kind, external_id) DO NOTHING",
            (wallet, VENUE, CHAIN, ts),
        )
        position_id = self._conn.execute(
            "SELECT id FROM positions WHERE wallet_ref=? AND venue=? AND chain=?"
            " AND kind='holding' AND external_id='btc'",
            (wallet, VENUE, CHAIN),
        ).fetchone()["id"]
        state = {
            "symbol": "BTC",
            "amount": str(stats.balance_btc),
            "confirmed_sats": stats.confirmed_sats,
            "pending_sats": stats.pending_sats,
            "tx_count": stats.tx_count,
            "source": f"esplora:{_provider_for(stats.source)}",
        }
        self._conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'observed', '{}', ?)",
            (position_id, ts, json.dumps(state)),
        )

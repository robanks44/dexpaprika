"""Block-pinned JSON-RPC client with ring failover (S4.5).

Every read-cycle resolves ONE pin per chain and executes every read at that
block via a single Multicall3 batch. The batch carries a per-chain block
tripwire — Base uses ``Multicall3.getBlockNumber()``; Arbitrum uses
``ArbSys.arbBlockNumber()`` because Multicall3's ``block.number`` there is
the L1 (Ethereum) block, not the L2 block (probe-verified 2026-08-02; the
ENGINEERING_STANDARDS §2 tripwire as literally written always fails on
Arbitrum — deviation logged in PROGRESS.md).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from dexpaprika.chains.abi import (
    ARBSYS,
    CHAIN_IDS,
    MULTICALL3,
    decode_aggregate,
    decode_uint,
    encode_call_aggregate,
    selector,
)
from dexpaprika.clients.base import Clock, HttpTransport, Sleeper, TransportError
from dexpaprika.config import Settings

USER_AGENT = "dexpaprika/1.0"  # arb1 403s the default python UA (verified)

_TRIPWIRES = {
    "base": (MULTICALL3, selector("getBlockNumber()")),
    "arbitrum": (ARBSYS, selector("arbBlockNumber()")),
}
_CHAIN_ID_CALL = (MULTICALL3, selector("getChainId()"))


class ChainRpcError(TransportError):
    """On-chain read failed in a way the caller must handle."""


class PinMismatchError(ChainRpcError):
    """A node answered for a different block than the pinned one (lagging)."""


@dataclass(frozen=True)
class PinnedSnapshot:
    chain: str
    block_number: int
    ts: str
    results: list[bytes]


class EvmRpcClient:
    """Ring of public RPC endpoints for one chain, quota-gated per upstream."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        chain: str,
        *,
        settings: Settings | None = None,
        clients: list[httpx.Client] | None = None,
        sleeper: Sleeper | None = None,
        clock: Clock | None = None,
    ) -> None:
        if chain not in CHAIN_IDS:
            msg = f"unknown chain {chain!r}: expected one of {', '.join(CHAIN_IDS)}"
            raise ChainRpcError(msg)
        self._conn = conn
        self._chain = chain
        cfg = settings or Settings.load()
        self._urls = list(cfg.base_rpc_urls if chain == "base" else cfg.arbitrum_rpc_urls)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        http_clients = clients or [
            httpx.Client(base_url=url, timeout=10.0, headers={"User-Agent": USER_AGENT})
            for url in self._urls
        ]
        self._transports = [
            HttpTransport(
                base_url=url,
                provider=f"{chain}-rpc",
                conn=conn,
                client=http_client,
                clock=clock,
                sleeper=sleeper or time.sleep,
            )
            for url, http_client in zip(self._urls, http_clients, strict=False)
        ]

    # ------------------------------ transport ------------------------------

    def rpc(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call with ring failover; reverts fail fast."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        errors: list[str] = []
        for url, transport in zip(self._urls, self._transports, strict=False):
            try:
                response = transport.post_json("", payload, endpoint_label=method)
            except TransportError as exc:
                errors.append(f"{url}: {exc}")
                continue
            error = response.get("error") if isinstance(response, dict) else None
            if error is not None:
                message = str(error.get("message", error))
                if "revert" in message.lower():
                    msg = f"{self._chain} {method} reverted: {message}"
                    raise ChainRpcError(msg)
                errors.append(f"{url}: JSON-RPC error {message}")
                continue
            return response["result"]
        msg = f"{self._chain} RPC ring exhausted for {method} — " + " | ".join(errors)
        raise ChainRpcError(msg)

    # ------------------------------ reads ------------------------------

    def block_number(self) -> int:
        return int(str(self.rpc("eth_blockNumber", [])), 16)

    def resolve_pin(self, margin: int = 3) -> int:
        """Head minus a small reorg margin — the block every read uses."""
        return self.block_number() - margin

    def call(self, to: str, data: str, block: int | str) -> str:
        block_param = hex(block) if isinstance(block, int) else block
        result = self.rpc("eth_call", [{"to": to, "data": data}, block_param])
        return str(result)

    def aggregate(self, calls: list[tuple[str, str]], block: int | str) -> tuple[int, list[bytes]]:
        raw = self.call(MULTICALL3, encode_call_aggregate(calls), block)
        return decode_aggregate(raw)

    # ------------------------------ snapshot ------------------------------

    def snapshot(
        self, kind: str, extra_calls: list[tuple[str, str]] | None = None
    ) -> PinnedSnapshot:
        """Pinned, tripwire-verified batch; records a `snapshots` row."""
        pin = self.resolve_pin()
        calls = [_TRIPWIRES[self._chain], _CHAIN_ID_CALL, *(extra_calls or [])]
        _outer_block, outputs = self.aggregate(calls, pin)
        tripwire_block = decode_uint(outputs[0])
        if tripwire_block != pin:
            msg = (
                f"{self._chain}: pinned block {pin} but node answered for"
                f" {tripwire_block} — lagging load-balanced node; snapshot discarded"
            )
            raise PinMismatchError(msg)
        chain_id = decode_uint(outputs[1])
        if chain_id != CHAIN_IDS[self._chain]:
            msg = (
                f"chainId tripwire: expected {CHAIN_IDS[self._chain]} for"
                f" {self._chain}, node returned {chain_id} — misconfigured RPC ring"
            )
            raise ChainRpcError(msg)
        ts = self._clock().isoformat()
        self._conn.execute(
            "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, ?, ?, ?)",
            (ts, self._chain, pin, kind),
        )
        return PinnedSnapshot(chain=self._chain, block_number=pin, ts=ts, results=outputs[2:])

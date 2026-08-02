"""DexPaprika API client — market HISTORY and VOLUME recording (S3).

Role boundary (VERIFIED_FINDINGS §3): DexPaprika prices are indexed
aggregates with a verified ~2% skew vs on-chain. They are recorded for
history/volume analysis ONLY. Range/edge detection and hedge math read the
pool contract directly (S4.5/S5) — this client deliberately exposes no
"current price for hedge math" API.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from dexpaprika.clients.base import Clock, HttpTransport, Sleeper
from dexpaprika.config import Settings

PROVIDER = "dexpaprika"
INTERVALS = ("1m", "5m", "15m", "30m", "1h", "6h", "12h", "24h")


class Network(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    display_name: str | None = None


class PoolDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")
    network: str
    address: str
    dex_id: str
    dex_name: str | None = None
    last_price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    fee: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    txns_24h: int | None = None
    raw: dict[str, Any]


class Candle(BaseModel):
    model_config = ConfigDict(extra="ignore")
    time_open: str
    time_close: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None


class DexPaprikaClient:
    """Typed reads + DB recording with source and as_of on every datapoint."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        client: httpx.Client | None = None,
        settings: Settings | None = None,
        sleeper: Sleeper | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        cfg = settings or Settings.load()
        http = client or httpx.Client(base_url=cfg.dexpaprika_base_url, timeout=30.0)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._transport = HttpTransport(
            base_url=cfg.dexpaprika_base_url,
            provider=PROVIDER,
            conn=conn,
            client=http,
            clock=clock,
            sleeper=sleeper or time.sleep,
        )

    # ------------------------------ reads ------------------------------

    def get_networks(self) -> list[Network]:
        payload = self._transport.get_json("/networks")
        return [Network.model_validate(item) for item in payload]

    def get_pool(self, network: str, address: str) -> PoolDetails:
        payload = self._transport.get_json(f"/networks/{network}/pools/{address}")
        day = payload.get("24h") or {}
        return PoolDetails(
            network=network,
            address=address,
            dex_id=payload["dex_id"],
            dex_name=payload.get("dex_name"),
            last_price_usd=payload.get("last_price_usd"),
            liquidity_usd=payload.get("liquidity_usd"),
            fee=payload.get("fee"),
            volume_24h_usd=day.get("volume_usd"),
            txns_24h=day.get("txns"),
            raw=payload,
        )

    def get_ohlcv(
        self,
        network: str,
        address: str,
        *,
        start: str,
        interval: str = "24h",
        limit: int = 30,
    ) -> list[Candle]:
        if interval not in INTERVALS:
            msg = f"invalid interval {interval!r}: expected one of {', '.join(INTERVALS)}"
            raise ValueError(msg)
        payload = self._transport.get_json(
            f"/networks/{network}/pools/{address}/ohlcv",
            params={"start": start, "interval": interval, "limit": limit},
        )
        return [Candle.model_validate(item) for item in payload]

    # ------------------------------ recording ------------------------------

    def record_pool_metrics(self, pool: PoolDetails) -> None:
        """Persist a pool observation (source + as_of always recorded)."""
        self._conn.execute(
            "INSERT INTO pool_metrics (ts, network, pool_address, source, price_usd,"
            " liquidity_usd, volume_24h_usd, txns_24h, fee, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock().isoformat(),
                pool.network,
                pool.address,
                PROVIDER,
                _dec(pool.last_price_usd),
                _dec(pool.liquidity_usd),
                _dec(pool.volume_24h_usd),
                pool.txns_24h,
                _dec(pool.fee),
                json.dumps(pool.raw, default=str),
            ),
        )

    def record_ohlcv(self, network: str, address: str, interval: str, candles: list[Candle]) -> int:
        """Upsert candles; idempotent on (network, pool, interval, ts_start)."""
        as_of = self._clock().isoformat()
        for candle in candles:
            self._conn.execute(
                "INSERT INTO ohlcv (network, pool_address, interval, ts_start, ts_end,"
                " open, high, low, close, volume, source, as_of)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(network, pool_address, interval, ts_start) DO UPDATE SET"
                " ts_end=excluded.ts_end, open=excluded.open, high=excluded.high,"
                " low=excluded.low, close=excluded.close, volume=excluded.volume,"
                " source=excluded.source, as_of=excluded.as_of",
                (
                    network,
                    address,
                    interval,
                    candle.time_open,
                    candle.time_close,
                    _dec(candle.open),
                    _dec(candle.high),
                    _dec(candle.low),
                    _dec(candle.close),
                    _dec(candle.volume),
                    PROVIDER,
                    as_of,
                ),
            )
        return len(candles)


_DecInput = Decimal | None
_DecOutput = str | None


def _dec(value: _DecInput) -> _DecOutput:
    from dexpaprika.storage.db import decimal_to_text

    return decimal_to_text(value) if value is not None else None

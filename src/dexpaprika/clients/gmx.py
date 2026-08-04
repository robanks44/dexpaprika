"""GMX v2 REST client — the hedge leg's data source (S4).

Scaling discipline (VERIFIED_FINDINGS §2.1 — a bug here = silent wrong
alerts): all numerics arrive as JSON strings and are scaled with exact
Decimal arithmetic. `triggerPrice` divides by 10^(30 - indexTokenDecimals)
(= 1e12 for ETH), NOT 1e30. `sizeDeltaUsd == uint256max` means "full
position close" and is surfaced as a flag, never a fake number.

API notes: two peers, rotated on failure; empty `[]` positions is a VALID
"no open positions" (a closed/liquidated position looks exactly like this);
the API is labeled "Expanding" — parse defensively. `/markets` carries
`symbol`, not `indexName` (probe-verified 2026-08-02).
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

from dexpaprika.clients.base import Clock, HttpTransport, Sleeper, TransportError
from dexpaprika.config import Settings

PROVIDER = "gmx"
UINT256_MAX = 2**256 - 1

ORDER_KINDS = {
    0: "market-swap",
    1: "limit-swap",
    2: "market-increase",
    3: "limit-increase",
    4: "market-decrease",
    5: "limit-decrease",
    6: "stop-loss-decrease",
    7: "liquidation",
    8: "stop-increase",
}

# Token decimals registry (Arbitrum). Unknown addresses ERROR — never guess.
TOKEN_DECIMALS = {
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 18,  # WETH
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,  # USDC (native)
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,  # USDC.e
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": 8,  # WBTC
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,  # USDT
}


def _opt_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _shift(raw: str | int, places: int) -> Decimal:
    """Exact decimal-point shift via the tuple form — context-free, never rounds.

    (Both ``/`` and ``scaleb`` round to the active context precision; uint256
    raw values can carry ~78 significant digits, so arithmetic would silently
    truncate. Explicit-precision discipline per ENGINEERING_STANDARDS §1.)
    """
    value = Decimal(str(raw))
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # NaN/Inf sentinels
        msg = f"non-finite numeric from GMX: {raw!r}"
        raise ValueError(msg)
    return Decimal((sign, digits, exponent - places))


def scale_usd(raw: str | int) -> Decimal:
    """1e30-scaled USD/price fields."""
    return _shift(raw, 30)


def scale_factor(raw: str | int) -> Decimal:
    """1e4-scaled factors (leverage, pnlPercentage)."""
    return _shift(raw, 4)


def scale_tokens(raw: str | int, decimals: int) -> Decimal:
    """Token amounts in native decimals."""
    return _shift(raw, decimals)


def scale_trigger(raw: str | int, index_decimals: int) -> Decimal:
    """Order triggerPrice: 10^(30 - indexTokenDecimals) = 1e12 for ETH."""
    return _shift(raw, 30 - index_decimals)


def token_decimals(address: str) -> int:
    decimals = TOKEN_DECIMALS.get(address.lower())
    if decimals is None:
        msg = (
            f"unknown token {address} — add its decimals to"
            " dexpaprika.clients.gmx.TOKEN_DECIMALS before scaling amounts"
        )
        raise TransportError(msg)
    return decimals


class GmxOrder(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    order_type: int
    order_kind: str
    trigger_price: Decimal | None = None
    is_full_close: bool = False
    size_delta_usd: Decimal | None = None  # None when is_full_close
    auto_cancel: bool = False
    is_long: bool | None = None
    raw: dict[str, Any]


class GmxPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    account: str
    market_address: str
    index_name: str | None = None
    pool_name: str | None = None
    is_long: bool
    size_usd: Decimal
    size_tokens: Decimal
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    collateral_usd: Decimal | None = None
    collateral_amount: Decimal | None = None
    leverage: Decimal | None = None
    pnl: Decimal | None = None
    pending_borrowing_fees_usd: Decimal | None = None
    pending_funding_fees_usd: Decimal | None = None
    related_orders: list[GmxOrder] = []
    raw: dict[str, Any]


class GmxMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    market_token_address: str
    index_token_address: str
    long_token_address: str | None = None
    short_token_address: str | None = None
    is_listed: bool | None = None


class GmxClient:
    """Peer-rotating typed reads + hedge-leg recording."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        clients: list[httpx.Client] | None = None,
        sleeper: Sleeper | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        cfg = settings or Settings.load()
        self._peers = list(cfg.gmx_rest_peers)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        http_clients = clients or [
            httpx.Client(base_url=peer, timeout=30.0) for peer in self._peers
        ]
        self._transports = [
            HttpTransport(
                base_url=peer,
                provider=PROVIDER,
                conn=conn,
                client=http_client,
                clock=clock,
                sleeper=sleeper or time.sleep,
            )
            for peer, http_client in zip(self._peers, http_clients, strict=False)
        ]
        self._markets_cache: list[GmxMarket] | None = None

    # ------------------------------ transport ------------------------------

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        errors: list[str] = []
        for peer, transport in zip(self._peers, self._transports, strict=False):
            try:
                return transport.get_json(path, params=params)
            except TransportError as exc:
                errors.append(f"{peer}: {exc}")
        msg = "all GMX peers failed — " + " | ".join(errors)
        raise TransportError(msg)

    # ------------------------------ reads ------------------------------

    def get_markets(self) -> list[GmxMarket]:
        if self._markets_cache is None:
            payload = self._get_json("/markets")
            self._markets_cache = [
                GmxMarket(
                    symbol=item.get("symbol", ""),
                    market_token_address=item.get("marketTokenAddress", ""),
                    index_token_address=item.get("indexTokenAddress", ""),
                    long_token_address=item.get("longTokenAddress"),
                    short_token_address=item.get("shortTokenAddress"),
                    is_listed=item.get("isListed"),
                )
                for item in payload
            ]
        return self._markets_cache

    def _index_decimals_for_market(self, market_address: str) -> int:
        for market in self.get_markets():
            if market.market_token_address.lower() == market_address.lower():
                return token_decimals(market.index_token_address)
        msg = f"market {market_address} not found in /markets — cannot scale triggerPrice"
        raise TransportError(msg)

    def _parse_order(self, item: dict[str, Any], index_decimals: int) -> GmxOrder:
        order_type = int(item.get("orderType", -1))
        raw_delta = item.get("sizeDeltaUsd")
        is_full = raw_delta is not None and int(str(raw_delta)) == UINT256_MAX
        trigger_raw = item.get("triggerPrice")
        return GmxOrder(
            key=str(item.get("key", "")),
            order_type=order_type,
            order_kind=ORDER_KINDS.get(order_type, f"unknown-{order_type}"),
            trigger_price=(
                scale_trigger(str(trigger_raw), index_decimals) if trigger_raw is not None else None
            ),
            is_full_close=is_full,
            size_delta_usd=None if is_full or raw_delta is None else scale_usd(str(raw_delta)),
            auto_cancel=bool(item.get("autoCancel", False)),
            is_long=item.get("isLong"),
            raw=item,
        )

    def _parse_position(self, item: dict[str, Any]) -> GmxPosition:
        market_address = str(item.get("marketAddress", ""))
        collateral_addr = item.get("collateralTokenAddress")
        collateral_amount = None
        if item.get("collateralAmount") is not None and collateral_addr:
            collateral_amount = scale_tokens(
                str(item["collateralAmount"]), token_decimals(str(collateral_addr))
            )
        orders_raw = item.get("relatedOrders") or []
        index_decimals = self._index_decimals_for_market(market_address) if orders_raw else 18

        def usd_or_none(field: str) -> Decimal | None:
            value = item.get(field)
            return scale_usd(str(value)) if value is not None else None

        return GmxPosition(
            key=str(item.get("key", "")),
            account=str(item.get("account", "")),
            market_address=market_address,
            index_name=item.get("indexName"),
            pool_name=item.get("poolName"),
            is_long=bool(item.get("isLong")),
            size_usd=scale_usd(str(item["sizeInUsd"])),
            size_tokens=scale_tokens(str(item["sizeInTokens"]), 18),
            entry_price=usd_or_none("entryPrice"),
            mark_price=usd_or_none("markPrice"),
            liquidation_price=usd_or_none("liquidationPrice"),
            collateral_usd=usd_or_none("collateralUsd"),
            collateral_amount=collateral_amount,
            leverage=(
                scale_factor(str(item["leverage"])) if item.get("leverage") is not None else None
            ),
            pnl=usd_or_none("pnl"),
            pending_borrowing_fees_usd=usd_or_none("pendingBorrowingFeesUsd"),
            pending_funding_fees_usd=usd_or_none("pendingFundingFeesUsd"),
            related_orders=[self._parse_order(o, index_decimals) for o in orders_raw],
            raw=item,
        )

    def get_positions(self, address: str, *, include_orders: bool = True) -> list[GmxPosition]:
        payload = self._get_json(
            "/positions",
            params={
                "address": address,
                **({"includeRelatedOrders": "true"} if include_orders else {}),
            },
        )
        return [self._parse_position(item) for item in payload]

    def hedge_positions(self, address: str) -> list[GmxPosition]:
        """ETH/USD shorts — the hedge leg (filter on POSITIONS, not /markets)."""
        return [
            p for p in self.get_positions(address) if p.index_name == "ETH/USD" and not p.is_long
        ]

    # ------------------------------ recording ------------------------------

    def record_observation(self, position: GmxPosition) -> None:
        """Persist the hedge leg: position upsert + observed event + orders."""
        now = self._clock().isoformat()
        self._conn.execute(
            "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag,"
            " opened_at, metadata_json) VALUES (?, 'gmx', 'arbitrum', 'perp', ?, 'lp_hedge',"
            " ?, ?)"
            " ON CONFLICT(wallet_ref, venue, chain, kind, external_id) DO UPDATE SET"
            " metadata_json=excluded.metadata_json",
            (
                position.account,
                position.key,
                now,
                json.dumps({"market": position.market_address, "pool": position.pool_name}),
            ),
        )
        position_id = self._conn.execute(
            "SELECT id FROM positions WHERE wallet_ref=? AND venue='gmx' AND chain='arbitrum'"
            " AND kind='perp' AND external_id=?",
            (position.account, position.key),
        ).fetchone()["id"]
        state = position.model_dump(mode="json", exclude={"raw", "related_orders"})
        sl_orders = [
            o
            for o in position.related_orders
            if o.order_kind == "stop-loss-decrease" and o.trigger_price is not None
        ]
        state["stop_loss_triggers"] = [str(o.trigger_price) for o in sl_orders]
        # SL SIZE co-located in hedge state (S12a): trigger + size per SL order,
        # so the full-variable set does not require a join to the orders table.
        state["stop_loss_orders"] = [
            {
                "trigger": str(o.trigger_price),
                "size_usd": None if o.is_full_close else _opt_str(o.size_delta_usd),
                "is_full_close": o.is_full_close,
            }
            for o in sl_orders
        ]
        self._conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'observed', '{}', ?)",
            (position_id, now, json.dumps(state)),
        )
        for order in position.related_orders:
            self._conn.execute(
                "INSERT INTO orders (ts, venue, external_key, order_type, trigger_price,"
                " size_delta, status, raw_json) VALUES (?, 'gmx', ?, ?, ?, ?, 'open', ?)"
                " ON CONFLICT(venue, external_key, ts) DO UPDATE SET"
                " trigger_price=excluded.trigger_price, size_delta=excluded.size_delta,"
                " raw_json=excluded.raw_json",
                (
                    now,
                    order.key,
                    order.order_type,
                    str(order.trigger_price) if order.trigger_price is not None else None,
                    "FULL_CLOSE"
                    if order.is_full_close
                    else (str(order.size_delta_usd) if order.size_delta_usd is not None else None),
                    json.dumps(order.raw, default=str),
                ),
            )

"""Single recording cycle — the reusable core extracted from ``snapshot``.

Same DB effects as the CLI ``snapshot`` command (positions upsert + observed
``position_events`` + a ``snapshots`` row per source), plus:

- per-source isolation: one failing source is stamped not-ok and logged to
  ``recorder_heartbeat``; it never aborts the other sources (backoff, not crash);
- full-variable capture (S12a): LP state carries both token USD prices and the
  pool's 24h volume (DexPaprika, null-with-reason when absent); the hedge state
  carries the SL order size alongside its trigger.

A cycle is deterministic given ``now`` — no wall-clock reads here, so the
service can inject a clock in tests (ENGINEERING_STANDARDS §6).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dexpaprika.config import Settings

KINDS: tuple[str, ...] = ("lp", "hedge", "defi", "holdings")

ClientFactory = Callable[[str], Any]


@dataclass
class SourceStamp:
    """Per-source outcome of a cycle: honest freshness + last error."""

    ok: bool
    ts: str
    block: int | None = None
    error: str | None = None


@dataclass
class CycleResult:
    ts: str
    wallets: list[str]
    counts: dict[str, int] = field(default_factory=dict)
    sources: dict[str, SourceStamp] = field(default_factory=dict)

    def all_ok(self) -> bool:
        return all(stamp.ok for stamp in self.sources.values())


@dataclass
class RecorderClients:
    """Pre-built source clients, injectable for zero-network tests."""

    base_rpc: Any = None
    gmx: Any = None
    dexpaprika: Any = None
    btc: Any = None


def _default_factory(base_url: str) -> Any:
    import httpx

    return httpx.Client(
        base_url=base_url,
        timeout=30.0,
        headers={"User-Agent": "dexpaprika/1.0"},
    )


def build_clients(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    kinds: Sequence[str],
    wallets: Sequence[str],
    btc_wallets: Sequence[str] = (),
    client_factory: ClientFactory | None = None,
) -> RecorderClients:
    """Construct only the clients the requested kinds need."""
    from dexpaprika.chains import EvmRpcClient
    from dexpaprika.clients.btc import BtcClient
    from dexpaprika.clients.dexpaprika import DexPaprikaClient
    from dexpaprika.clients.gmx import GmxClient

    factory = client_factory or _default_factory
    clients = RecorderClients()
    need_base = bool(wallets) and any(k in kinds for k in ("lp", "defi", "holdings"))
    if need_base:
        clients.base_rpc = EvmRpcClient(
            conn,
            "base",
            settings=settings,
            clients=[factory(url) for url in settings.base_rpc_urls],
        )
    if "lp" in kinds and need_base:
        clients.dexpaprika = DexPaprikaClient(
            conn, settings=settings, client=factory(settings.dexpaprika_base_url)
        )
    if "hedge" in kinds:
        clients.gmx = GmxClient(
            conn,
            settings=settings,
            clients=[factory(peer) for peer in settings.gmx_rest_peers],
        )
    if "holdings" in kinds and btc_wallets:
        clients.btc = BtcClient(
            conn,
            settings=settings,
            clients=[factory(peer) for peer in settings.btc_esplora_peers],
        )
    return clients


def _write_heartbeat(
    conn: sqlite3.Connection,
    ts: str,
    kind: str,
    *,
    ok: bool,
    block: int | None,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO recorder_heartbeat (ts, kind, ok, block, detail_json) VALUES (?, ?, ?, ?, ?)",
        (ts, kind, 1 if ok else 0, block, json.dumps(detail, default=str)),
    )


def _max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM position_events").fetchone()
    return int(row["m"])


def _derive_lifecycle_since(conn: sqlite3.Connection, before: int) -> None:
    from dexpaprika.portfolio.lifecycle import observe

    rows = conn.execute(
        "SELECT DISTINCT position_id, ts FROM position_events WHERE type='observed' AND id > ?",
        (before,),
    ).fetchall()
    for row in rows:
        observe(conn, row["position_id"], row["ts"])


def _record_lp(
    conn: sqlite3.Connection,
    clients: RecorderClients,
    settings: Settings,
    wallets: Sequence[str],
    ts: str,
) -> tuple[int, int]:
    from dexpaprika.lp.discovery import VENUE as LP_VENUE
    from dexpaprika.lp.discovery import discover
    from dexpaprika.lp.discovery import record as record_lp
    from dexpaprika.portfolio.lifecycle import reconcile_closures

    pin = clients.base_rpc.resolve_pin()
    count = 0
    for wallet in wallets:
        positions = discover(clients.base_rpc, wallet, settings=settings, block=pin)
        for position in positions:
            if clients.dexpaprika is not None and position.pool and not position.pool_unresolved:
                # Pool volume is off-chain, best-effort: any failure degrades to
                # null-with-reason (never fabricated, never fatal to LP recording).
                try:
                    pool = clients.dexpaprika.get_pool("base", position.pool)
                    clients.dexpaprika.record_pool_metrics(pool)
                    position.pool_volume_usd_24h = pool.volume_24h_usd
                except Exception as exc:  # best-effort enrichment
                    position.warnings.append(f"pool volume unavailable: {type(exc).__name__}")
            record_lp(conn, wallet, position, ts)
            count += 1
        reconcile_closures(
            conn,
            wallet,
            LP_VENUE,
            "lp",
            [f"{p.nfpm.lower()}:{p.token_id}" for p in positions],
            ts,
        )
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', ?, 'lp')",
        (ts, pin),
    )
    return count, pin


def _record_hedge(
    conn: sqlite3.Connection, clients: RecorderClients, wallets: Sequence[str], ts: str
) -> int:
    from dexpaprika.portfolio.lifecycle import reconcile_closures

    count = 0
    for wallet in wallets:
        positions = clients.gmx.get_positions(wallet)
        for position in positions:
            clients.gmx.record_observation(position)
            count += 1
        reconcile_closures(conn, wallet, "gmx", "perp", [p.key for p in positions], ts)
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind)"
        " VALUES (?, 'arbitrum', NULL, 'hedge')",
        (ts,),
    )
    return count


def _record_defi(
    conn: sqlite3.Connection,
    clients: RecorderClients,
    settings: Settings,
    wallets: Sequence[str],
    ts: str,
) -> tuple[int, int]:
    from dexpaprika.portfolio.aave import read_account
    from dexpaprika.portfolio.aave import record as record_aave

    pin = clients.base_rpc.resolve_pin()
    count = 0
    for wallet in wallets:
        account = read_account(clients.base_rpc, wallet, settings=settings, block=pin)
        count += record_aave(conn, wallet, account, ts)
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', ?, 'defi')",
        (ts, pin),
    )
    return count, pin


def _record_holdings(
    conn: sqlite3.Connection,
    clients: RecorderClients,
    wallets: Sequence[str],
    btc_wallets: Sequence[str],
    ts: str,
) -> tuple[int, int | None]:
    from dexpaprika.portfolio.holdings import read_holdings
    from dexpaprika.portfolio.holdings import record as record_holdings

    count = 0
    pin: int | None = None
    if clients.base_rpc is not None and wallets:
        pin = clients.base_rpc.resolve_pin()
        for wallet in wallets:
            holdings = read_holdings(clients.base_rpc, "base", wallet, block=pin)
            count += record_holdings(conn, wallet, "base", holdings, ts)
        conn.execute(
            "INSERT INTO snapshots (ts, chain, block_number, kind)"
            " VALUES (?, 'base', ?, 'holdings')",
            (ts, pin),
        )
    if clients.btc is not None and btc_wallets:
        for wallet in btc_wallets:
            clients.btc.record(wallet, clients.btc.get_address(wallet), ts)
            count += 1
        conn.execute(
            "INSERT INTO snapshots (ts, chain, block_number, kind)"
            " VALUES (?, 'bitcoin', NULL, 'holdings')",
            (ts,),
        )
    return count, pin


def run_cycle(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    kinds: Sequence[str],
    wallets: Sequence[str],
    btc_wallets: Sequence[str] = (),
    address: str | None = None,  # kept for CLI signature parity (resolution is caller-side)
    now: datetime | None = None,
    clients: RecorderClients | None = None,
) -> CycleResult:
    """One recording cycle. Each source is isolated: a failure is stamped
    not-ok and heart-beated, never aborting the other sources."""
    ts = (now or datetime.now(UTC)).isoformat()
    if clients is None:
        clients = build_clients(
            conn, settings, kinds=kinds, wallets=wallets, btc_wallets=btc_wallets
        )
    result = CycleResult(ts=ts, wallets=list(wallets) + list(btc_wallets))

    def _run(kind: str, fn: Callable[[], tuple[int, int | None]]) -> None:
        # The connection is in autocommit mode ("explicit BEGIN/COMMIT only", db.py),
        # so transactions are driven by SQL statements — conn.commit()/rollback() are
        # no-ops here. One atomic transaction per source → no partial rows on failure.
        before = _max_event_id(conn)
        conn.execute("BEGIN")
        try:
            count, block = fn()
            _derive_lifecycle_since(conn, before)
            _write_heartbeat(conn, ts, kind, ok=True, block=block, detail={"count": count})
            conn.execute("COMMIT")
        except Exception as exc:  # per-source isolation is the whole point
            conn.execute("ROLLBACK")
            msg = f"{type(exc).__name__}: {exc}"
            result.sources[kind] = SourceStamp(ok=False, ts=ts, error=msg)
            # Heartbeat the failure in autocommit mode (outside the rolled-back txn).
            _write_heartbeat(conn, ts, kind, ok=False, block=None, detail={"error": str(exc)})
            return
        result.counts[kind] = count
        result.sources[kind] = SourceStamp(ok=True, ts=ts, block=block)

    if "lp" in kinds and clients.base_rpc is not None:
        _run("lp", lambda: _record_lp(conn, clients, settings, wallets, ts))
    if "hedge" in kinds and clients.gmx is not None:
        _run("hedge", lambda: (_record_hedge(conn, clients, wallets, ts), None))
    if "defi" in kinds and clients.base_rpc is not None:
        _run("defi", lambda: _record_defi(conn, clients, settings, wallets, ts))
    if "holdings" in kinds and (clients.base_rpc is not None or clients.btc is not None):
        _run("holdings", lambda: _record_holdings(conn, clients, wallets, btc_wallets, ts))
    return result

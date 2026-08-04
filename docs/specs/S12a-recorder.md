# S12a — Recorder service + full-variable recording (spec)

**Status:** COMPLETE (tag `s12a-complete`) — fresh-agent verdict PASS (honest-staleness
defect found + fixed on re-verify; 415 passed, 93.61% coverage).
**Depends:** S3 (DexPaprika), S4 (GMX), S5 (LP discovery — RESOLVED), S6 (recording seam).
**Decision basis:** PROGRESS decision log 2026-08-04 (full-variable DB + LIVE dashboard);
ENGINEERING_STANDARDS §6 amendment (daemon = liveness; correctness stays CLI + scheduler).

## Purpose

Turn the existing one-shot `snapshot` command into (1) a reusable **single recording
cycle**, and (2) a long-running **recorder service** that captures the FULL raw variable
set on a per-source cadence into SQLite (WAL) — while keeping a plain-CLI single-cycle
path so correctness never requires the daemon. Storage is RAW ONLY; derived metrics are a
read-time concern (S12b). This section does NOT build the dashboard (that is S12b).

## Public interface

- `dexpaprika.recorder.cycle.run_cycle(conn, settings, *, kinds, wallets, address=None, now) -> CycleResult`
  — the pure, reusable cycle extracted from `_cmd_snapshot` (same DB effects). `CycleResult`
  carries per-kind counts, the resolved wallets, and per-source `sources: dict[str, SourceStamp]`
  where `SourceStamp = {ok: bool, ts: str, block: int|None, error: str|None}`.
- `dexpaprika.recorder.service.RecorderService` — a loop that calls `run_cycle` per source
  cadence, with error backoff and a cooperative stop. Injected `clock` + `sleep` (no real
  time in tests). Writes a `recorder_heartbeat` row each cycle and keeps the latest
  in-memory `SourceStamp`s; exposes `status()` (latest stamps + staleness seconds).
- CLI:
  - `dexpaprika recorder cycle [--kind all|lp|hedge|defi|holdings] [--address A] [--json]`
    — one cycle; the external-scheduler correctness fallback. Same effect as one service tick.
  - `dexpaprika recorder run [--kind ...] [--lp-interval S] [--hedge-interval S]
    [--max-cycles N] [--json]` — the service loop (foreground; a Windows service/NSSM or
    scheduled-at-logon wraps it — see python-scheduling--playbook--windows.md). `--max-cycles`
    bounds it for test and smoke runs.
  - `dexpaprika recorder status [--json]` — last cycle per source + staleness + heartbeat age.
- Schema (migration `0003_recorder.sql`): `recorder_heartbeat (id, ts, kind, ok, block,
  detail_json)` — append-only; readers never block the writer (WAL). No change to existing
  tables; the full raw variable set continues to live in `position_events.state_json`,
  `hedge_state`, `orders.raw_json`.

## Full-variable capture (acceptance)

A cycle MUST persist, per the 2026-08-04 decision, into RAW storage (state_json etc.):
- **LP:** both token prices, pool price + tick, in/out-of-range, pool volume + liquidity,
  position token amounts, unclaimed fees. (From `lp.discovery` — already captures ticks,
  liquidity, amounts, pool, in_range; extend the recorded state to include token prices +
  pool volume + unclaimed fees if absent.)
- **Hedge:** mark/entry/liq price, size (USD + ETH), collateral/margin, leverage, uPnL,
  funding + borrowing fees, and the SL/stop order (trigger + size). (From `clients.gmx` +
  `orders`.) 
A probe (Step 2b) dumps one live LP + one live hedge raw state to `probes/out/s12a/` and the
test-fixture asserts every field above is present (or explicitly recorded as null-with-reason).

## Behavioural rules

- **Staleness is honest.** Every source carries its own last-updated stamp; `status()`
  reports staleness seconds per source. A source that failed this cycle keeps its previous
  stamp and is marked not-ok — never silently presented as fresh (ENGINEERING_STANDARDS §2).
- **Backoff, not crash.** A source error is logged to `recorder_heartbeat` (ok=false) and
  retried next cadence with capped exponential backoff; one bad source never stops the loop
  or the other sources.
- **Cooperative stop.** `run` stops cleanly on stop-flag/`--max-cycles`; no partial rows.
- **Correctness without the daemon.** `recorder cycle` == exactly one service tick; a series
  of scheduled `cycle` calls produces the same rows as `run`. (Property-tested.)
- Standards: quota tracker gates every client; Decimal for money; no secrets; zero-network
  tests via mocked clients.

## Tests (offline, written first)

1. `run_cycle` with mocked LP/GMX/aave/holdings clients writes the expected snapshots +
   position_events (observed state_json) + heartbeat; counts match; sources stamped ok.
2. Full-variable fixture: recorded LP + hedge state_json contains every field in the
   acceptance list (fixture from the Step-2b probe dump).
3. Service loop: with an injected clock+sleep and `--max-cycles`, LP runs every lp-interval,
   hedge every hedge-interval; terminates deterministically; heartbeat rows == cycles.
4. Backoff: a client raising once → heartbeat ok=false that cycle, ok=true next, loop
   survives, other sources still recorded.
5. Staleness: `status()` staleness increases with the injected clock; a failed source shows
   its stale prior stamp, flagged not-ok.
6. Equivalence: N sequential `run_cycle` calls == one `run` with `--max-cycles N` (same rows).
7. CLI: `recorder cycle`/`status` JSON contracts; `recorder run --max-cycles 1` exits 0.

## Done-criteria

Full suite + ruff + mypy(strict) + coverage gate green; fresh-agent verdict PASS; existing
S6 `snapshot` behaviour unchanged (its tests still pass after the `run_cycle` extraction);
`ARCHITECTURE.md` + `RUNBOOK.md` updated with the recorder; merge `--no-ff`, tag
`s12a-complete`.

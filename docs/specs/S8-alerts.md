# S8 spec — Reporting & alerts (ntfy)

## Purpose

Close the loop from recorded state to Richard's phone: a rules engine that
evaluates the latest recorded snapshots/hedge state/quota/health, an ntfy
client that delivers, and an `alerts_log` audit trail so no firing is ever
lost — plus report formats a fresh Claude session can consume (RUNBOOK).

Read-only + outbound-notify only. No orders (S9, separately gated).

## Reference gate

- `CONTEXT\reference\ntfy--api-reference.md` (captured 2026-08-01): publish =
  POST to `https://ntfy.sh/{topic}`; headers Title/Priority/Tags/Click;
  JSON publish for action buttons; **topic is a secret by knowledge-of-name**;
  best-effort delivery, ~12h server cache, free-tier rate limits ⇒ alert per
  state-change with cooldown, never per poll tick.
- `CONTEXT\reference\python-scheduling--playbook--windows.md`: architecture is
  CLI + EXTERNAL scheduler (VERIFIED_FINDINGS §7). Task Scheduler drives
  `dexpaprika snapshot` (hourly) and `dexpaprika alerts check` (minutes-scale);
  catch-up on missed start, no-new-instance, 30-min hang guard; CLI must be
  idempotent and gap-tolerant; nonzero exit on failure so scheduler history is
  a health log. APScheduler documented as the later-VPS option only.

## Probe gate

Outbound-only upstream, but the response shape still gets pinned from life:
probe publishes ONE low-priority test message to the real topic and dumps the
JSON receipt (`id`, `time`, `event`, `topic`, `message`) to
`probes/out/s8/publish_receipt.json` (topic REDACTED in the committed
fixture). That receipt is the mock fixture for the gate suite.

## Secrets & config

- Secret `ntfy_topic` (env backend: `DEXPAPRIKA_NTFY_TOPIC`). NEVER logged,
  never in alerts_log, never in error text — client redacts the topic from any
  exception/URL it surfaces (tested).
- Config additions (`Settings`): `ntfy_base_url` (default `https://ntfy.sh`,
  HTTPS-only rule applies), `alert_cooldown_minutes` (default 60),
  `snapshot_staleness_minutes` (default 90 — hourly recorder + slack),
  `quota_alert_used_pct` (default `Decimal("0.80")`).

## Public interface

### `dexpaprika.alerts.ntfy` — `NtfyClient`

- `publish(title, message, *, priority="default", tags=(), click=None) -> PublishReceipt`
  via the shared transport factory (quota-gated like every upstream; provider
  row `ntfy`). Priorities: min/low/default/high/urgent (ntfy 1–5).
- Action buttons (JSON publish, ≤3 actions) supported by the client for S9's
  approval pattern but NOT used by S8 rules.

### `dexpaprika.alerts.rules` — the engine

`evaluate(conn, *, settings, now) -> list[Alert]` over recorded state only
(no network): `Alert(rule, severity, title, message, firing_inputs)`.

| rule | fires when | severity |
|---|---|---|
| `naked-lp` | hedge analysis flags naked-lp (short gone/zero) | urgent |
| `price-near-sl` | analysis flag (≤3% from SL) | urgent |
| `near-band-edge` | analysis flag (≤2% from either bound) | high |
| `rebalance-needed` | analysis rebalance_needed (band `hedge_rebalance_band`) | high |
| `snapshot-stale` | newest snapshot older than `snapshot_staleness_minutes` | high |
| `quota-critical` | any provider ≥ `quota_alert_used_pct` of a window/credit budget | high |
| `healthcheck-degraded` | offline healthcheck (db/migrations/secrets) fails | high |

Hedge rules source their inputs from the SAME path as `hedge status`
(`_latest_hedge_inputs` + `analyze`); no recorded hedge/lp state ⇒ only the
non-hedge rules run (staleness will fire — that's the alert).

- **Cooldown/dedup:** a rule that fired within `alert_cooldown_minutes`
  (per alerts_log, delivered or not) is suppressed — returned as suppressed,
  not re-delivered. State-change alerts, not poll-tick spam (ntfy rate-limit
  note).
- **Recording:** every non-suppressed alert is written to `alerts_log`
  (`payload_json` = firing inputs incl. the numbers that tripped the rule)
  BEFORE delivery is attempted; `delivered`/`ntfy_status` updated after.
  Delivery failure ⇒ recorded-not-lost, CLI exits degraded.

### CLI

```
dexpaprika alerts check [--dry-run] [--json]   # evaluate → record → deliver
dexpaprika alerts test [--json]                # one live test notification
dexpaprika alerts log [--limit N] [--json]     # firing history (audit)
```

Exit codes: 0 = evaluated + all deliveries ok (alerts firing is still 0 —
firing is the system WORKING); 3 = degraded (delivery failed / no topic
configured but rules fired); 1 = failure; 2 = usage. `--dry-run` evaluates
and prints, records nothing, sends nothing.

### Reports for a fresh Claude session (RUNBOOK)

No new report code: `report --json`, `hedge status --json`, `quota --json`,
`alerts log --json` are the machine-readable surface. RUNBOOK S8 section
documents the read-order for a fresh session + the Task Scheduler task
definitions (hourly `snapshot --kind lp` + `--kind hedge`; 5-min
`alerts check`; hardening checklist from the playbook).

## Test focus (gate suite, offline)

- Each rule fires on a synthetic state that violates it and stays silent on a
  healthy state; quadrant/flag inputs reuse the S5/S4 live fixtures.
- Cooldown: same rule twice inside the window ⇒ one delivery, one log row.
- Delivery failure (mock 500/timeout): alert row exists, `delivered=0`,
  `ntfy_status` recorded, exit 3.
- Secret hygiene: topic never appears in log output, alerts_log, or raised
  errors (assert on repr/message).
- `alerts test` end-to-end against the mock receipt fixture.

## Done criteria

Gate green + verifier pass + ONE live smoke (excluded from gate):
`alerts test` lands a REAL notification on Richard's topic, and
`alerts check --dry-run` runs clean against the real recorded DB.

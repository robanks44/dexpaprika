# S13 — External watchdog + daily digest (spec)

**Status:** in_progress (branch `section/s13-watchdog`)
**Depends:** S8 (alerts/ntfy delivery), S12a (recorder + snapshots/heartbeat), S12b (read layer).
**Decision basis:** PROGRESS 2026-08-04 "NEW top risk — silent failure / automation
complacency": once alerts replace the daily check, a DEAD recorder = nobody watching, worse
than baseline. REQUIREMENTS (not options): (1) heartbeat to an EXTERNAL dead-man's switch
that must NOT live on the watched machine; (2) a daily "all is well" position digest to ntfy.
**Library:** checked 2026-08-04 — no coverage for healthchecks.io / dead-man switches. Built
provider-agnostic against the healthchecks.io ping convention (works with any equivalent).

## Purpose

Two guards that survive the failure mode self-hosted alerts can't catch (a dead machine
can't alert itself):

1. **External heartbeat** — the machine pings an OFF-machine dead-man's switch on a short
   cadence while healthy. If pings stop (machine dead, recorder crashed, power/network lost),
   the EXTERNAL service alerts Richard. This is the authoritative liveness guard.
2. **Daily digest** — a once-a-day "all is well" position summary to ntfy, replacing the old
   manual once-daily check (PROGRESS baseline: Richard managed positions once/day).

No new runtime dependency. Outbound HTTP is intentional here (unlike the dashboard) — the
watchdog's whole job is to reach an external service. The digest goes through the NtfyClient
(shared transport). The heartbeat ping response is plain text (`OK`), so it uses a direct,
injectable httpx GET that BYPASSES api_call_log entirely — so the secret token never reaches
the log — and redacts the token from any exception (same hygiene intent as the ntfy topic).

## Public interface

- `dexpaprika.watchdog.heartbeat`:
  - `ping(conn, settings, *, state, client_factory=None) -> PingResult` — ping the configured
    dead-man's switch. `state ∈ {"ok","fail","start"}`. healthchecks.io convention: base URL =
    success, `/fail` = failure, `/start` = run-started. The base ping URL is the secret
    `heartbeat_url` (resolved via `resolve_provider`); UNSET → `PingResult(configured=False,
    …)` (honest no-op, never a fake success). Token hygiene: `endpoint_label="heartbeat"`,
    errors redact the URL.
  - `assess_health(conn, settings, *, now, max_age_s) -> HealthVerdict` — is the recorder
    fresh? Reads the newest `snapshots` ts (reusing S12b staleness); `ok` when the freshest
    source is within `max_age_s`, else `stale` with the offending age. NO network.
  - `run_heartbeat(conn, settings, *, now, client_factory=None) -> HeartbeatResult` — assess,
    then ping `ok` when healthy / `fail` when stale. This is what the scheduler calls: a
    healthy machine pings ok (switch stays green); a stalled-but-alive machine pings fail
    (switch trips loudly); a dead machine sends nothing (switch trips on silence).
- `dexpaprika.watchdog.digest`:
  - `build_digest(conn, settings, *, now) -> Digest` — compose the daily summary from the
    latest recorded state, reusing `dashboard.read.latest_view` + `derived`. Content: per-source
    freshness (honest staleness), LP in-range + range position, hedge coverage + distance-to-SL
    / distance-to-liq, net delta, funding run-rate, rebalance flag. `Digest` carries a title,
    a plain-text message, a priority, tags, and an `all_ok` bool. If data is STALE or missing,
    the digest says so and is NOT "all is well" (no fabricated green).
  - `send_digest(conn, settings, *, now, client_factory=None) -> DigestResult` — build + deliver
    via `NtfyClient` (reused). Tag/priority reflect `all_ok` (green check vs warning).
- CLI (`dexpaprika watchdog …`):
  - `watchdog heartbeat [--state ok|fail|start|auto] [--json]` — `auto` (default) runs
    `run_heartbeat` (assess→ping); explicit state forces a ping. The scheduler calls `auto`.
  - `watchdog digest [--dry-run] [--json]` — build + send (or print) the daily digest.
  - `watchdog status [--json]` — show heartbeat-url configured?, last snapshot age, health
    verdict — offline, no ping.
- Scheduler: add two `JobSpec`s — `watchdog-heartbeat` (interval, e.g. 5 min → `watchdog
  heartbeat`) and `watchdog-digest` (cron-daily → `watchdog digest`). Same
  max_instances=1 / exit-code-honest discipline as existing jobs.

## Behavioural rules

- **Off-machine or it's worthless.** The heartbeat only guards if the switch lives elsewhere;
  the RUNBOOK says so and the URL is operator-supplied. The system cannot verify
  off-machine-ness, so it's a documented operator responsibility.
- **Honest health.** A stale recorder pings `fail`, never `ok`; a dead machine pings nothing.
  The digest never reports "all is well" over stale/missing data (ENGINEERING_STANDARDS §2).
- **No fabrication.** No heartbeat URL configured → `configured=False`, surfaced, never a
  faked send. Digest metrics come from recorded RAW rows via the S12b read layer.
- **Secret hygiene.** The ping-URL token gets the ntfy-topic treatment: never in
  api_call_log endpoint, never in an exception, never in a repr.
- **Silence self-noise.** The digest is a SUMMARY, not a re-fire of S8 per-rule alerts; it
  does not duplicate or replace S8 (S8 still fires actionable alerts; S13 adds the external
  liveness guard + the daily all-clear).

## Tests (offline, written first)

1. `ping`: unset heartbeat_url → `configured=False`, no HTTP attempted; configured → posts to
   the right path per state (mocked transport); `/fail` and `/start` suffixes correct.
2. Token hygiene: a transport error's message has the URL token redacted; `api_call_log`
   endpoint label is `heartbeat`, not the URL.
3. `assess_health`: fresh snapshot → ok; stale (> max_age) → stale with the age; empty DB →
   stale/no-data (never ok).
4. `run_heartbeat`: healthy → pings `ok`; stale → pings `fail`; unconfigured → no ping,
   surfaced.
5. `build_digest`: from a seeded DB, message contains coverage/distance/in-range/staleness;
   `all_ok` true only when fresh AND within-range/covered; stale data → `all_ok` false and the
   message says stale (no green).
6. `send_digest`: delivers via a mocked NtfyClient with the right priority/tag for all_ok vs
   not; `--dry-run` sends nothing.
7. Scheduler: `job_specs` includes `watchdog-heartbeat` (interval) + `watchdog-digest`
   (cron-daily) with max_instances=1; triggers build.
8. CLI: `watchdog heartbeat/digest/status` JSON contracts; `heartbeat` unconfigured →
   non-zero-free honest payload; `digest --dry-run` prints without sending.

## Done-criteria

Full suite + ruff + mypy(strict) + coverage gate green; fresh-agent verdict PASS; no new
runtime dep; `ARCHITECTURE.md` + `RUNBOOK.md` updated (watchdog commands, the off-machine
requirement, the `heartbeat_url` secret + `DEXPAPRIKA_SECRET_HEARTBEAT_URL`); merge `--no-ff`,
tag `s13-complete`.

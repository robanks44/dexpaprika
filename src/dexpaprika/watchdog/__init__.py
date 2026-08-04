"""External watchdog: dead-man's-switch heartbeat + daily digest (S13).

The two guards for the failure mode self-hosted alerts cannot catch — a dead
machine cannot alert itself. The heartbeat pings an OFF-machine switch while
healthy (silence ⇒ external alert); the digest sends a daily all-clear to ntfy.
"""

from __future__ import annotations

from dexpaprika.watchdog.digest import Digest, DigestResult, build_digest, send_digest
from dexpaprika.watchdog.heartbeat import (
    HealthVerdict,
    HeartbeatResult,
    PingResult,
    assess_health,
    ping,
    run_heartbeat,
)

__all__ = [
    "Digest",
    "DigestResult",
    "HealthVerdict",
    "HeartbeatResult",
    "PingResult",
    "assess_health",
    "build_digest",
    "ping",
    "run_heartbeat",
    "send_digest",
]

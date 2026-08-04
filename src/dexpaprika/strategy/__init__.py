"""Delta-band rebalance strategy (S14) — net-capital-optimizing hedge management.

Resize the GMX short to track the LP's live ETH exposure, triggered by delta drift.
Ships DORMANT (shadow → measure → tune → enable); auto-execution reuses the S9
pipeline (no guard bypassed) and is gated behind ``auto_rebalance_enabled``.
"""

from __future__ import annotations

from dexpaprika.strategy.rebalance import (
    GateStates,
    RebalanceDecision,
    RebalanceOutcome,
    evaluate,
    run,
)

__all__ = [
    "GateStates",
    "RebalanceDecision",
    "RebalanceOutcome",
    "evaluate",
    "run",
]

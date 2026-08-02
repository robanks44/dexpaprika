"""Out-of-band approval (S9) — ntfy request + reply polling (OWASP ASI09).

The approval channel is Richard's phone, NOT the CLI/agent that asked.
Approval binds to the instruction id whose full parameters were shown:
a bare "yes" fires nothing. Timeout = rejected (fail-closed).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from dexpaprika.clients.base import Sleeper


class ApprovalDecision(BaseModel, frozen=True):
    approved: bool
    reason: str


# (title, message, priority) -> None
Publisher = Callable[[str, str, str], object]
# since-unix-ts -> list of message texts
Poller = Callable[[int], list[str]]
Clock = Callable[[], datetime]


def request_approval(
    instruction_id: str,
    message: str,
    *,
    publisher: Publisher,
    poller: Poller,
    clock: Clock,
    sleeper: Sleeper,
    timeout_minutes: int,
    poll_interval_seconds: float = 5.0,
) -> ApprovalDecision:
    """Publish the substantive request, then poll for Richard's reply."""
    started = clock()
    deadline = started + timedelta(minutes=timeout_minutes)
    approve_token = f"approve {instruction_id}"
    reject_token = f"reject {instruction_id}"
    publisher(
        "EXECUTE approval required",
        f"{message}\n\nReply '{approve_token}' or '{reject_token}' within {timeout_minutes} min.",
        "urgent",
    )
    since = int(started.timestamp())
    while clock() < deadline:
        for text in poller(since):
            lowered = text.strip().lower()
            if lowered == approve_token.lower():
                return ApprovalDecision(approved=True, reason=f"approved via ntfy: {text!r}")
            if lowered == reject_token.lower():
                return ApprovalDecision(approved=False, reason=f"rejected via ntfy: {text!r}")
        sleeper(poll_interval_seconds)
    return ApprovalDecision(
        approved=False,
        reason=f"approval timeout after {timeout_minutes} min — fail-closed",
    )

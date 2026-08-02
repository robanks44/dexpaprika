"""Reporting & alerts (S8) — rules engine over recorded state + ntfy delivery."""

from dexpaprika.alerts.ntfy import NtfyClient, PublishReceipt
from dexpaprika.alerts.rules import (
    Alert,
    apply_cooldown,
    evaluate,
    mark_delivery,
    record_alert,
)

__all__ = [
    "Alert",
    "NtfyClient",
    "PublishReceipt",
    "apply_cooldown",
    "evaluate",
    "mark_delivery",
    "record_alert",
]

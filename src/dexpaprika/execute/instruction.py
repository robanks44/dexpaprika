"""Typed order instructions (S9) — the ONLY thing the executor accepts.

No free-text ever reaches the execution path (OWASP ASI01/ASI02): three
typed actions, Decimal parameters, and a deterministic idempotency key
derived from the decision identity (action + canonical params + UTC hour
bucket — the Stripe model, bounded expiry when the bucket rolls).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, model_validator

MARKET = "ETH/USD"

Action = Literal["set-sl-trigger", "resize-short", "cancel-order"]


class OrderInstruction(BaseModel, frozen=True):
    """One fully-specified privileged action."""

    action: Action
    market: str = MARKET
    trigger_price: Decimal | None = None  # set-sl-trigger
    target_eth: Decimal | None = None  # resize-short
    order_key: str | None = None  # set-sl-trigger / cancel-order

    @model_validator(mode="after")
    def _required_params(self) -> OrderInstruction:
        if self.action == "set-sl-trigger" and (
            self.trigger_price is None or self.order_key is None
        ):
            msg = "set-sl-trigger requires trigger_price and order_key"
            raise ValueError(msg)
        if self.action == "resize-short" and self.target_eth is None:
            msg = "resize-short requires target_eth"
            raise ValueError(msg)
        if self.action == "cancel-order" and self.order_key is None:
            msg = "cancel-order requires order_key"
            raise ValueError(msg)
        # Hard parameter ranges IN PYTHON (verifier finding #1 — the spec's
        # claim now matches the code; the sidecar's checks are a second net).
        if self.trigger_price is not None and not (
            Decimal(0) < self.trigger_price < Decimal(1_000_000)
        ):
            msg = f"trigger_price {self.trigger_price} outside sane range (0, 1e6)"
            raise ValueError(msg)
        if self.target_eth is not None and not (Decimal(0) <= self.target_eth <= Decimal(100)):
            msg = f"target_eth {self.target_eth} outside sane range [0, 100]"
            raise ValueError(msg)
        if self.order_key is not None and not (
            self.order_key.startswith("0x") and len(self.order_key) == 66
        ):
            msg = f"order_key {self.order_key!r} is not a 32-byte hex key"
            raise ValueError(msg)
        return self

    def canonical(self) -> str:
        parts = [
            self.action,
            self.market,
            str(self.trigger_price) if self.trigger_price is not None else "-",
            str(self.target_eth) if self.target_eth is not None else "-",
            (self.order_key or "-").lower(),
        ]
        return "|".join(parts)

    def idempotency_key(self, now: datetime) -> str:
        bucket = now.strftime("%Y-%m-%dT%H")  # UTC hour bucket
        return hashlib.sha256(f"{self.canonical()}|{bucket}".encode()).hexdigest()

    def summary(self) -> str:
        """Human line for approvals/audits — restates every parameter."""
        bits = [f"action={self.action}", f"market={self.market}"]
        if self.trigger_price is not None:
            bits.append(f"trigger_price=${self.trigger_price}")
        if self.target_eth is not None:
            bits.append(f"target_eth={self.target_eth}")
        if self.order_key:
            bits.append(f"order={self.order_key}")
        return " ".join(bits)

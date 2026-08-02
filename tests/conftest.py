"""Global test isolation (pytest--best-practices.md pattern).

No ambient ``DEXPAPRIKA_*`` environment can leak into any test: every test
starts from a clean slate and sets exactly what it needs. This is what keeps
the gate suite deterministic on any machine (fresh-agent requirement).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_dexpaprika_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("DEXPAPRIKA_"):
            monkeypatch.delenv(key)

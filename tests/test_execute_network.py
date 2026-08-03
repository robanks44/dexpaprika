"""S9.5 — network-configurable executor (Arbitrum Sepolia testnet harness)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dexpaprika.config import Settings

ARBITRUM_ONE = 42161
ARBITRUM_SEPOLIA = 421614
MAINNET_ACCOUNT = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"


class TestConfigDefaults:
    def test_mainnet_defaults(self) -> None:
        s = Settings.load()
        assert s.gmx_chain_id == ARBITRUM_ONE
        assert s.execution_account == MAINNET_ACCOUNT

    def test_testnet_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_GMX_CHAIN_ID", str(ARBITRUM_SEPOLIA))
        monkeypatch.setenv(
            "DEXPAPRIKA_EXECUTION_ACCOUNT", "0x1111111111111111111111111111111111111111"
        )
        s = Settings.load()
        assert s.gmx_chain_id == ARBITRUM_SEPOLIA
        assert s.execution_account == "0x1111111111111111111111111111111111111111"

    def test_only_gmx_chain_ids_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A chain GMX does not run on is rejected (no Base — verified 2026-08)."""
        monkeypatch.setenv("DEXPAPRIKA_GMX_CHAIN_ID", "8453")  # Base
        with pytest.raises(ValueError, match="GMX"):
            Settings.load()


class TestSidecarEnv:
    def _capture_env(self, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> dict[str, str]:
        import dexpaprika.cli as cli

        captured: dict[str, Any] = {}

        class FakeCompleted:
            returncode = 0
            stdout = '{"ok": true, "orders": []}'
            stderr = ""

        def fake_run(argv: list[str], **kwargs: Any) -> FakeCompleted:
            captured["env"] = kwargs["env"]
            captured["input"] = kwargs["input"]
            return FakeCompleted()

        monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/node")
        monkeypatch.setattr(cli.Path, "exists", lambda _self: True)
        monkeypatch.setattr("subprocess.run", fake_run)
        runner = cli._sidecar_runner(settings)
        runner({"mode": "read", "action": "read-orders", "params": {}})
        return captured["env"]

    def test_chain_and_account_passed_to_sidecar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_GMX_CHAIN_ID", str(ARBITRUM_SEPOLIA))
        monkeypatch.setenv("DEXPAPRIKA_EXECUTION_ACCOUNT", MAINNET_ACCOUNT)
        env = self._capture_env(monkeypatch, Settings.load())
        assert env["GMX_CHAIN_ID"] == str(ARBITRUM_SEPOLIA)
        assert env["GMX_ACCOUNT"] == MAINNET_ACCOUNT

    def test_read_mode_never_gets_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
        monkeypatch.setenv("DEXPAPRIKA_SECRET_GMX_SUBACCOUNT_KEY", "0xdeadbeef")
        env = self._capture_env(monkeypatch, Settings.load())
        assert "GMX_SUBACCOUNT_KEY" not in env  # only submit mode gets it


class TestSidecarScript:
    def test_sidecar_reads_chain_and_account_from_env(self) -> None:
        """The Node sidecar must not hardcode chain/account (grep the source)."""
        source = (Path(__file__).parent.parent / "executor" / "gmx_exec.cjs").read_text(
            encoding="utf-8"
        )
        assert "process.env.GMX_CHAIN_ID" in source
        assert "process.env.GMX_ACCOUNT" in source
        # No hardcoded mainnet account literal driving behaviour.
        assert source.count(MAINNET_ACCOUNT) == 0

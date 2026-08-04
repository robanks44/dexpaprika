"""S9.6 — on-chain executor (GmxSdk Classic) plumbing + sidecar-source invariants.

Locks the behavior established by the 2026-08-04 express->on-chain pivot:
- the Python runner defaults to the on-chain sidecar and hands it the wallet key
  ONLY in submit mode;
- RPC/oracle/subsquid overrides pass through;
- the on-chain sidecar reads its target from env (no hardcoded account/key) and
  sequences a nonce-safe create-new -> wait -> cancel-old move.

Offline by design: subprocess is faked, no network, no node execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dexpaprika.config import Settings

MAINNET_ACCOUNT = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
ONCHAIN = Path(__file__).parent.parent / "executor" / "gmx_exec_onchain.cjs"
FAKE_KEY = "0x" + "11" * 32

READ = {"mode": "read", "action": "read-orders", "params": {}}
SUBMIT_SL = {"mode": "submit", "action": "set-sl-trigger", "params": {}}


def _capture(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    import dexpaprika.cli as cli

    captured: dict[str, Any] = {}

    class FakeCompleted:
        returncode = 0
        stdout = '{"ok": true, "orders": []}'
        stderr = ""

    def fake_run(argv: list[str], **kwargs: Any) -> FakeCompleted:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return FakeCompleted()

    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr("pathlib.Path.exists", lambda _self: True)
    monkeypatch.setattr("subprocess.run", fake_run)
    cli._sidecar_runner(settings)(payload)
    return captured


class TestOnchainPlumbing:
    def test_default_sidecar_is_onchain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap = _capture(monkeypatch, Settings.load(), READ)
        assert cap["argv"][1].endswith("gmx_exec_onchain.cjs")

    def test_sidecar_script_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_SIDECAR_SCRIPT", "gmx_exec.cjs")
        cap = _capture(monkeypatch, Settings.load(), READ)
        assert cap["argv"][1].endswith("gmx_exec.cjs")

    def test_submit_gets_wallet_key_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
        monkeypatch.setenv("DEXPAPRIKA_SECRET_GMX_WALLET_KEY", FAKE_KEY)
        cap = _capture(monkeypatch, Settings.load(), SUBMIT_SL)
        assert cap["env"]["GMX_WALLET_KEY"] == FAKE_KEY
        # No dexpaprika secret is ever visible to the sidecar.
        assert not any(k.startswith("DEXPAPRIKA_SECRET_") for k in cap["env"])

    def test_read_and_prepare_never_get_wallet_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
        monkeypatch.setenv("DEXPAPRIKA_SECRET_GMX_WALLET_KEY", FAKE_KEY)
        for mode in ("read", "prepare"):
            payload: dict[str, Any] = {"mode": mode, "action": "set-sl-trigger", "params": {}}
            cap = _capture(monkeypatch, Settings.load(), payload)
            assert "GMX_WALLET_KEY" not in cap["env"]

    def test_submit_without_wallet_key_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dexpaprika.cli as cli

        monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
        monkeypatch.delenv("DEXPAPRIKA_SECRET_GMX_WALLET_KEY", raising=False)
        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/node")
        monkeypatch.setattr("pathlib.Path.exists", lambda _self: True)

        def boom(*_a: Any, **_k: Any) -> Any:  # subprocess must never be reached
            raise AssertionError("sidecar ran without a resolvable wallet key")

        monkeypatch.setattr("subprocess.run", boom)
        res = cli._sidecar_runner(Settings.load())(SUBMIT_SL)
        assert res["ok"] is False
        assert "gmx_wallet_key" in res["error"]

    def test_rpc_oracle_subsquid_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GMX_RPC_URL", "https://rpc.example")
        monkeypatch.setenv("GMX_ORACLE_URL", "https://oracle.example")
        monkeypatch.setenv("GMX_SUBSQUID_URL", "https://sq.example")
        cap = _capture(monkeypatch, Settings.load(), READ)
        assert cap["env"]["GMX_RPC_URL"] == "https://rpc.example"
        assert cap["env"]["GMX_ORACLE_URL"] == "https://oracle.example"
        assert cap["env"]["GMX_SUBSQUID_URL"] == "https://sq.example"


class TestOnchainSidecarSource:
    src = ONCHAIN.read_text(encoding="utf-8")

    def test_target_read_from_env_not_hardcoded(self) -> None:
        for needle in (
            "process.env.GMX_ACCOUNT",
            "process.env.GMX_CHAIN_ID",
            "process.env.GMX_RPC_URL",
            "process.env.GMX_WALLET_KEY",
        ):
            assert needle in self.src
        # No hardcoded mainnet execution account driving behaviour.
        assert MAINNET_ACCOUNT not in self.src

    def test_wallet_address_equals_account_guard(self) -> None:
        assert "!= GMX_ACCOUNT" in self.src or "wallet key address" in self.src

    def test_nonce_safe_create_then_wait_then_cancel(self) -> None:
        i_create = self.src.index("sdk.orders.createDecreaseOrder")
        i_wait = self.src.index("await waitNonceAdvance")
        i_last_cancel = self.src.rindex("sdk.orders.cancelOrders")
        assert i_create < i_wait < i_last_cancel

    def test_cancel_order_action_supported(self) -> None:
        assert "cancel-order" in self.src

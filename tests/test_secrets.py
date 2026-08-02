"""Secret providers — fallback chain, masking, no value leakage."""

from __future__ import annotations

import pytest

from dexpaprika.config import Settings
from dexpaprika.secrets import (
    ChainProvider,
    EnvProvider,
    KeyringProvider,
    resolve_provider,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("DEXPAPRIKA_"):
            monkeypatch.delenv(key)


def test_env_provider_reads_prefixed_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "dummy-topic-value")
    assert EnvProvider().get("ntfy_topic") == "dummy-topic-value"


def test_env_provider_missing_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EnvProvider().get("ntfy_topic") is None


def test_keyring_provider_uses_dexpaprika_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_get(service: str, name: str) -> str | None:
        calls.append((service, name))
        return "from-keyring"

    monkeypatch.setattr("keyring.get_password", fake_get)
    assert KeyringProvider().get("github_pat") == "from-keyring"
    assert calls == [("dexpaprika", "github_pat")]


def test_keyring_unavailable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux VM without a backend: silent None, never a crash (reference doc)."""

    def broken_get(service: str, name: str) -> str | None:
        raise RuntimeError("No recommended backend was available")

    monkeypatch.setattr("keyring.get_password", broken_get)
    assert KeyringProvider().get("github_pat") is None


def test_chain_prefers_keyring_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "env-value")
    monkeypatch.setattr("keyring.get_password", lambda s, n: None)
    chain = ChainProvider()
    assert chain.get("ntfy_topic") == "env-value"

    monkeypatch.setattr("keyring.get_password", lambda s, n: "keyring-value")
    assert chain.get("ntfy_topic") == "keyring-value"


@pytest.mark.parametrize(
    ("backend", "expected_type"),
    [("auto", ChainProvider), ("keyring", KeyringProvider), ("env", EnvProvider)],
)
def test_resolve_provider(
    backend: str, expected_type: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", backend)
    assert isinstance(resolve_provider(Settings.load()), expected_type)


def test_provider_reprs_never_contain_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "super-secret-value")
    provider = EnvProvider()
    provider.get("ntfy_topic")
    assert "super-secret-value" not in repr(provider)
    assert "super-secret-value" not in str(provider)

"""Secret access behind a provider seam (ENGINEERING_STANDARDS §3).

Local: OS keyring (Windows Credential Manager on Richard's machine).
Cloud/CI: environment variables ``DEXPAPRIKA_SECRET_<NAME>``.
``auto`` chains keyring → env (reference: python-keyring--setup--windows.md).

Secret VALUES must never appear in logs, exceptions, or reprs. Providers
hold no secret state; they fetch on demand and return the value to the
caller only.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol

import keyring

from dexpaprika.config import Settings

SERVICE_NAME = "dexpaprika"

KNOWN_SECRETS = (
    "github_pat",
    "ntfy_topic",
    "dexpaprika_api_key",
    "krystal_api_key",
    "coinstats_api_key",
    "heartbeat_url",  # S13: off-machine dead-man's-switch ping URL (token is a secret)
)


class SecretProvider(Protocol):
    """Anything that can resolve a named secret."""

    def get(self, name: str) -> str | None:
        """Return the secret value, or None if unavailable."""
        ...


class KeyringProvider:
    """OS keyring, service ``dexpaprika``.

    On a machine without a configured backend (e.g. a Linux VM), lookups
    return None silently rather than crashing — documented keyring behavior
    the env fallback is designed around.
    """

    def get(self, name: str) -> str | None:
        try:
            value = keyring.get_password(SERVICE_NAME, name)
        except Exception:  # any backend failure means "not available here" → env fallback
            return None
        return value or None

    def __repr__(self) -> str:
        return f"KeyringProvider(service={SERVICE_NAME!r})"


class EnvProvider:
    """Environment variables ``DEXPAPRIKA_SECRET_<NAME_UPPERCASED>``."""

    def get(self, name: str) -> str | None:
        return os.environ.get(f"DEXPAPRIKA_SECRET_{name.upper()}") or None

    def __repr__(self) -> str:
        return "EnvProvider(prefix='DEXPAPRIKA_SECRET_')"


class ChainProvider:
    """Keyring first, then env — the ``auto`` backend."""

    def __init__(self) -> None:
        self._providers: tuple[SecretProvider, ...] = (KeyringProvider(), EnvProvider())

    def get(self, name: str) -> str | None:
        for provider in self._providers:
            value = provider.get(name)
            if value is not None:
                return value
        return None

    def __repr__(self) -> str:
        return f"ChainProvider(providers={self._providers!r})"


_BACKENDS: dict[str, Callable[[], SecretProvider]] = {
    "keyring": KeyringProvider,
    "env": EnvProvider,
    "auto": ChainProvider,
}


def resolve_provider(settings: Settings) -> SecretProvider:
    """Pick the provider for the configured backend."""
    return _BACKENDS[settings.secret_backend]()

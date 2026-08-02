"""Container/compose/release packaging integrity (S11) — offline gate."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
COMPOSE = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
MAKEFILE = (REPO / "Makefile").read_text(encoding="utf-8")


class TestCompose:
    def test_scheduler_service_runs_the_scheduler(self) -> None:
        service = COMPOSE["services"]["scheduler"]
        assert service["command"] == ["scheduler", "run"]
        assert service["restart"] == "unless-stopped"

    def test_data_volume_and_hardening(self) -> None:
        service = COMPOSE["services"]["scheduler"]
        assert any(str(v).endswith(":/data") for v in service["volumes"])
        assert "dexpaprika-data" in COMPOSE["volumes"]
        assert service["read_only"] is True

    def test_secrets_come_from_environment_not_the_file(self) -> None:
        """The §3 provider swap: env backend in containers, values NEVER inline."""
        service = COMPOSE["services"]["scheduler"]
        environment = service["environment"]
        assert environment["DEXPAPRIKA_SECRET_BACKEND"] == "env"  # noqa: S105 — backend name, not a secret
        topic = environment["DEXPAPRIKA_SECRET_NTFY_TOPIC"]
        assert str(topic).startswith("${")  # interpolated from host env, never a value


class TestDockerfile:
    def test_non_root_and_entrypoint(self) -> None:
        assert "USER app" in DOCKERFILE
        assert 'ENTRYPOINT ["dexpaprika"]' in DOCKERFILE

    def test_pinned_base_and_multistage(self) -> None:
        assert DOCKERFILE.count("FROM python:3.13-slim") >= 2


class TestMakefile:
    def test_sbom_and_release_targets(self) -> None:
        assert "sbom:" in MAKEFILE
        assert "release:" in MAKEFILE
        assert "cyclonedx" in MAKEFILE

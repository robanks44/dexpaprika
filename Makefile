# Gate commands (ENGINEERING_STANDARDS §1/§3/§5).
#
# `make test`  — the OFFLINE gate suite: zero network, zero secrets, zero human
#               action. This is what the fresh-agent verifier runs and what must
#               be green before any section completes.
# `make audit` — supply-chain checks that legitimately need the network
#               (vulnerability DBs). Run on every change when online, and on a
#               schedule — new CVEs land after you pin.
# `make gate`  — both.

.PHONY: test lint type unit audit gate smoke

test: lint type unit

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

type:
	uv run mypy

unit:
	uv run pytest

audit:
	uv run bandit -c pyproject.toml -r src -q
	uv run pip-audit

gate: test audit

# Read-only LIVE smoke suite (S10) — real network, throwaway data dir, sends
# nothing. The live leg of the LOOP_PROMPT Step 8 whole-system check.
smoke:
	uv run pytest tests/live -m live --force-enable-socket --no-cov -q

# CycloneDX SBOM for the FROZEN runtime dependency set (ENGINEERING_STANDARDS §3).
sbom:
	mkdir -p dist
	uv export --frozen --no-dev --no-emit-project -o dist/requirements-frozen.txt
	uv run cyclonedx-py requirements dist/requirements-frozen.txt -o dist/sbom.cdx.json
	rm -f dist/requirements-frozen.txt

# Release artifact = wheel + sdist + SBOM, all under dist/.
release: sbom
	uv build

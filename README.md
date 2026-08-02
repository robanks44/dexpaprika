# dexpaprika

Claude-operated DeFi system: portfolio analysis, data recording, and active
management of a GMX short hedge that protects LP downside risk. Named after the
DexPaprika market-data API, a primary data source.

**Operator:** Claude, via the CLI (`dexpaprika <cmd> --json`). Richard never has
to run commands or git. Start with `dexpaprika status --json`.

## Layout

| Path | What |
|---|---|
| `ARCHITECTURE.md` | Components, data flow, schema draft, secrets, safety design |
| `SECTION_PLAN.md` | Ordered build sections (one section = one loop iteration) |
| `PROGRESS.md` | Loop state — single source of truth across sessions |
| `loop/` | The build-loop kit: LOOP_PROMPT, ENGINEERING_STANDARDS, GIT_RULES, REFERENCE_INDEX |
| `probes/` | Throwaway probe scripts + recorded raw payloads (test-fixture sources) |
| `src/dexpaprika/` | Application code (S0: CLI stubs only) |
| `tests/` | Offline gate suite |

## Gates

```
make test    # offline gate suite: ruff + mypy --strict + pytest (coverage)
make audit   # bandit + pip-audit (needs network for vuln DBs)
make gate    # both
```

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13 (`uv sync` sets up the
env from the committed lockfile). Install hooks once per clone: `uv tool run
pre-commit install`.

## Build process

Each build iteration = paste `loop/LOOP_PROMPT.md` in a fresh session. One
section per iteration, tests before code, a fresh agent independently verifies,
git per `loop/GIT_RULES.md`. Reference docs resolve through
`loop/REFERENCE_INDEX.md` against the CONTEXT library
(`C:\Users\NoBloat\COWORK\CONTEXT`).

# dexpaprika-loop — Coding Loop for the DexPaprika Project

A dedicated build loop for the **dexpaprika** project: a secure, reliable, Claude-operated
DeFi system for portfolio analysis, data recording, and active management of a GMX short
hedge that protects LP downside risk.

**Yes — this project gets its own loop.** A coding loop carries project-specific state
(section plan, reference-doc map, test gates, progress log) that doesn't transfer between
projects. Reusing another loop would mix progress state and skip this project's specific
gates (school-material reconciliation, GMX safety rules, offline test requirement).

## What's in this folder

| File | Purpose |
|------|---------|
| `README.md` | This file — how to use the loop |
| `SETUP_PROMPT.txt` | **Start here.** Paste as the first message in the dexpaprika project chat. Sets up the ideal environment: scope confirmation, research, architecture, section plan, scaffold. No app code. |
| `LOOP_PROMPT.md` | The per-iteration prompt. Paste once per build iteration; each iteration completes exactly one section through all gates. |
| `ENGINEERING_STANDARDS.md` | Binding standards: security, reliability, testing, cloud migratability, privileged-action safeguards. |
| `REFERENCE_INDEX.md` | Read-before-code map: which library docs must be read before each kind of section. **§0/§0.1 hold session-verified findings that outrank every other doc — read them first, every section.** |
| `GIT_RULES.md` | Automatic git tracking rules: Claude does all git; every gate leaves a commit, every completed section a tag; secrets can never enter history. |
| `PROGRESS.md` | Loop state file — the single source of truth across sessions (paired with git history). |

## How to use it (Richard's steps)

1. **Copy this whole folder** into the dexpaprika project's working folder (e.g. as
   `dexpaprika-loop\` or `loop\` at the repo root).
2. **Connect the reference library** to the dexpaprika Cowork session:
   `C:\Users\NoBloat\COWORK\CONTEXT` — one folder, containing `reference\` (indexed
   technical docs), `APIDOCS\` and `encylopedia Uig\`. Start at `reference\INDEX.md`.
   Also connect the dexpaprika project folder itself.
   (The old `PROJECTS\UIG\Context Docs` folder is GONE — do not try to connect it.)
3. **Paste the contents of `SETUP_PROMPT.txt`** as the first message in the dexpaprika
   chat. It will confirm scope with you, research, design, and scaffold — then stop for
   your approval. It writes no application code.
4. **Review its handoff report** (architecture + section plan). Approve or adjust.
5. **Each build iteration:** paste the iteration prompt from `LOOP_PROMPT.md`. One
   iteration = one section, built test-first and verified by a fresh agent. You can run
   iterations back-to-back or days apart — `PROGRESS.md` carries the state.
6. **If a section fails 3 attempts**, the loop stops and writes you a blocker report
   instead of thrashing.

## The loop's hard gates (summary)

1. **Reference gate** — mapped docs in `REFERENCE_INDEX.md` are read before a section is
   designed; hedge/LP-risk sections are reconciled against the school material in
   `encylopedia Uig\`.
2. **Tests-first gate** — the section's tests are written and committed (failing) before
   any implementation code.
3. **Fresh-agent gate** — a new agent with no implementation context builds a clean env
   and runs the FULL suite + static/security gates. It alone decides pass/fail.
4. **Restart rule** — any failure restarts the section loop; all tests must pass; the
   gate suite needs zero network and zero action from you.
5. **Git gate** — everything is tracked in git automatically per `GIT_RULES.md`: `main`
   only ever holds verifier-passed code, each section merges with a `s<N>-complete` tag,
   and no iteration may end with uncommitted changes. You never run git yourself.

## Notes

- The DexPaprika API is public (no key required; free key raises the monthly cap) but
  capped at ~30 requests/minute — the standards file requires client-side rate limiting.
- Order execution on GMX (actually adjusting the hedge) is treated as a privileged
  capability: dry-run by default, hard limits, kill-switch, audit log — and it only enters
  the section plan if you explicitly approve it during setup Step 1.
- Loop authored 2026-07-31 in the Claude Loops project; a copy lives in that project's
  docs under `dexpaprika-loop/` for reuse as a template.

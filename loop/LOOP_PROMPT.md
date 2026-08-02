# LOOP_PROMPT.md — Section Build Loop (paste for each iteration)

You are running one iteration of the dexpaprika build loop. Complete exactly ONE section
per iteration, passing every gate. If any gate fails, restart the section loop as
described in Step 7 — never carry a failing section forward.

Paste-able iteration prompt:

---

Run one iteration of the dexpaprika section loop.

**Step 1 — Orient.** Read `PROGRESS.md`, `SECTION_PLAN.md`, `ENGINEERING_STANDARDS.md`,
`REFERENCE_INDEX.md`, and `GIT_RULES.md`. Git preflight (per GIT_RULES.md §6): working
tree clean, `main` tip matches the last completed-section tag recorded in PROGRESS.md —
any mismatch is a blocker to investigate and log before building. Select the next
incomplete section from SECTION_PLAN.md (respect dependencies), create/checkout its
branch `section/s<N>-<slug>` from `main`, and mark it `in_progress` in PROGRESS.md with a
timestamp (committed).

**Step 2 — Reference gate (hard gate).** Look the section's topic up in
`REFERENCE_INDEX.md` and read the mapped reference docs BEFORE designing anything. For
sections touching hedge logic or LP risk, the school material in `encylopedia Uig\` is
ground truth — reconcile the design with it. For live endpoints/SDKs, verify the local
mirror against current online docs. Record in PROGRESS.md which docs were read and any
design decisions they drove.

**Step 2b — Probe gate (hard gate, new 2026-08-01).** If this section reads from any
external API or on-chain source, WRITE AND RUN A THROWAWAY PROBE against the real source
with Richard's real wallet BEFORE designing the client. Dump the raw response to
`probes/out/` and commit it. Confirm from the actual payload: the fields you intend to
parse exist; freshness/cache parameters are set correctly; and the values match an
independent source (on-chain read, block explorer, or a second provider). Do NOT design
against documentation alone — REFERENCE_INDEX §0.1 lists four findings from one hour of
probing that documentation alone would have missed, each worth a wasted section. The
recorded payload becomes the section's test fixture (ENGINEERING_STANDARDS §5).

**Step 3 — Spec.** Write/update the section's spec: purpose, public interface (CLI
commands, functions, schemas), acceptance criteria, error cases, and its standards
obligations (rate limits, validation, logging, privileged-action rules). Keep it short and
testable. Commit: `docs(s<N>): spec for <name>`.

**Step 4 — Tests first (hard gate).** Write the section's tests BEFORE any implementation
code: unit tests, integration tests with recorded/mocked fixtures, property tests where
math is involved, and negative/error-path tests. They must run fully offline (no network,
no secrets, no human action). Run the suite and confirm the new tests FAIL for the right
reason (feature absent — not collection errors). Commit the tests before writing any
implementation: `test(s<N>): tests for <name> (attempt <A>, pre-implementation)`.

**Step 5 — Implement.** Write the implementation to the spec and
`ENGINEERING_STANDARDS.md`. Iterate until the FULL suite (all sections, not just this one)
passes locally, plus: `ruff check` clean, `mypy --strict` clean, `bandit` and `pip-audit`
clean or findings documented, coverage thresholds met. Commit incrementally as atomic
conventional commits (`feat(s<N>): ...`) per GIT_RULES.md §3 — never a blind `git add .`.
Do NOT weaken or delete tests to pass; any test change is its own commit with a
justification in the message and logged in PROGRESS.md.

**Step 6 — Independent verification (hard gate).** Spawn a FRESH agent (Task/Agent tool)
with NO implementation context. Give it only: repo path plus the section branch's commit
hash, and instructions to (a) check out that exact commit in a clean clone/worktree —
verifying committed state, not the working directory, (b) create a clean environment from
the lockfile, (c) run the full test suite + static gates from scratch, (d) report results
verbatim as pass/fail with output. The implementer does not run this step's checks
itself, and the verifier does not fix anything.

**Step 7 — Gate decision.**
- ANY failure → log the verbatim failure in PROGRESS.md under the section's attempt
  history, increment the attempt counter, commit it
  (`chore(s<N>): record attempt <A> failure`), and RESTART the section loop at Step 3
  (Step 4 if the tests themselves were wrong) on the SAME branch — attempt history stays
  in git. Do not proceed. If the same section fails 3 attempts, stop and write a blocker
  report in PROGRESS.md for Richard instead of looping a 4th time.
- ALL pass → mark the section `complete` in PROGRESS.md (docs read, attempts, coverage,
  verifier verdict, branch + merge hash), update `RUNBOOK.md` and `ARCHITECTURE.md` if
  the section changed operations or structure, then per GIT_RULES.md §2: merge the
  section branch into `main` with `--no-ff` (merge message = gate summary + verifier
  verdict), create annotated tag `s<N>-complete`, delete the branch, and push `main` +
  tags if a remote is configured. Confirm the working tree is clean.

**Step 8 — Report.** End with a short summary: section completed, gates passed, verifier
verdict, next section queued. If this was the final section, run the whole-system check:
fresh-agent full-suite run + `healthcheck` + the read-only live smoke suite, and report
the system as complete only if all pass.

---

**Loop invariants (apply every iteration):**
- One section per iteration; never two.
- Tests precede code; a fresh agent is the only judge of pass/fail.
- The gate suite requires zero action from Richard and zero network access.
- PROGRESS.md is the single source of truth for loop state — keep it accurate; assume the
  next iteration may run in a fresh session with no memory of this one.
- PROGRESS.md is SINGLE-WRITER. Never run two loop iterations against this repo at the
  same time, in this session or another — concurrent writes silently drop entries and both
  writers verify their own change and pass. Re-read PROGRESS.md immediately before writing.
- No client is designed from documentation alone; Step 2b's recorded payload comes first.
- All git operations follow GIT_RULES.md; every iteration ends with a clean working tree
  and `main` still green. Richard never has to run git.

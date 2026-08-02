# GIT_RULES.md — Version Control Rules (automatic and binding)

Git is the project's audit trail. Claude performs ALL git operations — Richard never has
to touch git. Every loop gate leaves a commit; every completed section leaves a tag; a
fresh session must be able to reconstruct full project state from `git log` + PROGRESS.md
alone. These rules bind every phase (setup and build loop).

## 1. One-time setup (SETUP_PROMPT Step 4, before any other file is created)

1. `git init` with default branch `main`.
2. Set a repo-local bot identity so loop commits are distinguishable from Richard's:
   `git config user.name "Claude (dexpaprika-loop)"` and
   `git config user.email "rcarrgeo+dexpaprika-loop@gmail.com"`.
3. **.gitignore is the first file committed** — before anything else can be staged. It
   must cover at minimum: `.env*` (except `.env.example`), `*.db* / *.sqlite*` runtime
   databases, `logs/`, `.venv/`, `__pycache__/`, `.coverage*`, `.pytest_cache/`,
   `.mypy_cache/`, `.ruff_cache/`, `dist/`, key/wallet material (`*.pem`, `*.key`,
   `keystore*`), and editor cruft. Recorded API test fixtures ARE committed (they are
   sanitized — see §4).
4. Install pre-commit hooks (committed `.pre-commit-config.yaml`), running on every
   commit: secret scan (`gitleaks` or `detect-secrets`), `ruff` lint + format,
   `check-added-large-files` (limit 1 MB), `end-of-file-fixer`, `check-merge-conflict`.
   Hooks must pass — never commit with `--no-verify` (if a hook is wrong, fix the hook in
   its own commit).
5. Initial commit: `chore(S0): initialize repository` — then build the scaffold in
   further commits. Tag `S0-complete` only after the scaffold passes its fresh-agent gate.
6. **Remote (ask Richard during setup Step 1):** recommended — a PRIVATE GitHub repo as
   off-machine backup. Authenticate via HTTPS with a fine-grained PAT scoped to that one
   repo, stored in the OS credential manager/keyring — never in a file or URL. If a
   remote exists, push `main` and tags automatically after every section merge (§3).

## 2. Branching model

- `main` is always green: every commit on `main` has passed the fresh-agent gate. Direct
  commits to `main` are allowed ONLY for docs/state files (PROGRESS.md, RUNBOOK.md,
  ARCHITECTURE.md, loop files) — never for application code or tests.
- Every section is built on its own branch: `section/s<N>-<slug>` (e.g.
  `section/s3-dexpaprika-client`), created from the tip of `main` at Step 1 of the loop.
- Failed attempts stay on the section branch — attempt history is evidence, not garbage.
  Restarting a section continues on the same branch.
- Merge to `main` ONLY after the Step 6 verifier reports all gates passed:
  `git merge --no-ff section/s<N>-<slug>` with a merge message summarizing gates and the
  verifier verdict, then tag `s<N>-complete` (annotated tag). Delete the section branch
  after merge. Push `main` + tags if a remote is configured.

## 3. Commit rules (per loop step)

Conventional Commits: `type(scope): summary` — types `feat|fix|test|docs|chore|refactor|ci`,
scope = section id. Commits are atomic and staged deliberately: review `git status` and
`git diff --staged` before every commit; never blind `git add .` / `git add -A`.

| Loop step | Required commit |
|---|---|
| Step 3 spec written | `docs(s<N>): spec for <name>` |
| Step 4 tests written (failing) | `test(s<N>): tests for <name> (attempt <A>, pre-implementation)` |
| Step 5 implementation | one or more `feat(s<N>): ...` / `fix(s<N>): ...` commits |
| Step 7 gate FAIL | `chore(s<N>): record attempt <A> failure` (PROGRESS.md update) |
| Step 7 gate PASS | `--no-ff` merge to `main` + annotated tag `s<N>-complete` |
| Any test modification | own commit, `test(s<N>): <change> — justification: <why>` |

- PROGRESS.md is committed with every state change it records.
- Lockfile (`uv.lock`) changes are their own `chore(deps):` commit with the reason.
- Merge commit bodies include: reference docs read, attempts count, coverage, verifier
  verdict (verbatim pass line).

## 4. What never enters history

Secrets, API keys, tokens, seed phrases, private keys, `.env` values, runtime databases,
logs, and any personal/financial data beyond what the sanitized test fixtures need.
Recorded fixtures must be scrubbed of real keys/addresses before committing (replace with
documented dummy values). **If a secret ever lands in a commit: rotate the secret FIRST,
then purge history (`git filter-repo`), then log the incident in PROGRESS.md.** The
pre-commit secret scan exists to make this path never needed.

## 5. History integrity

- Never force-push `main`. Never rewrite pushed history. `git commit --amend` only for
  the immediately previous, unpushed commit.
- Tags are immutable audit points — never move or delete a `s<N>-complete` tag.
- No vendored dependencies or generated artifacts in git; the lockfile is the record.

## 6. Automation guarantees (checked by the loop)

- Every loop iteration ends with a CLEAN working tree (`git status --porcelain` empty).
  A section may not be reported complete with uncommitted changes.
- Every iteration starts by confirming: current branch is correct, working tree clean,
  `main` tip matches the last `s<N>-complete` tag recorded in PROGRESS.md. Any mismatch
  is a blocker — investigate and log before building.
- The fresh-agent verifier (loop Step 6) checks out the section branch by commit hash in
  a clean clone/worktree — it verifies committed state, not the working directory.
- `healthcheck` includes a repo check: clean tree, on a known branch, hooks installed.

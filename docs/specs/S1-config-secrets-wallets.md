# S1 spec — Config, secrets & wallet registry

## Purpose

Typed, env-first configuration; secret access through a provider seam (keyring
locally, env in cloud); a wallet registry (EVM/BTC/Solana) with include/exclude
driving all downstream selection; strict address validation at the boundary;
CLI surface for it all.

## Public interface

### `dexpaprika.config.Settings` (pydantic-settings)

- Env prefix `DEXPAPRIKA_`; optional `.env` for NON-secret local config only
  (secrets never live in `.env` — keyring/env-var only, per reference doc note).
- Fields (defaults in parens): `data_dir` (`./data`), `log_level` (`INFO`),
  `base_rpc_urls`, `arbitrum_rpc_urls`, `gmx_rest_peers`,
  `dexpaprika_base_url`, `ntfy_server`, `secret_backend` (`auto`),
  S9 hard limits: `max_position_usd` (Decimal `0`), `max_delta_per_run_usd`
  (Decimal `0`), `max_daily_adjustments` (`0`), `allowed_markets` (empty) —
  zero/empty = execution disabled.
- Money fields are `Decimal`, parsed from strings; never float.
- `Settings.load()` classmethod; list fields parse comma-separated env values.
- The system reads/writes ONLY inside `data_dir` (ENGINEERING_STANDARDS §3).

### `dexpaprika.secrets`

- `SecretProvider` protocol: `get(name: str) -> str | None`.
- `KeyringProvider` — service `"dexpaprika"`, via `keyring` (Windows Credential
  Manager on Richard's machine; silently `None` on an unconfigured Linux VM —
  documented behavior, reference doc "Fallback for Cowork VM").
- `EnvProvider` — `DEXPAPRIKA_SECRET_<NAME_UPPERCASED>`.
- `ChainProvider` — keyring first, env fallback (reference loading pattern).
- `resolve_provider(settings)` — `auto` → chain; `keyring`/`env` → that one.
- Known secret names: `github_pat`, `ntfy_topic`, `dexpaprika_api_key`,
  `krystal_api_key`, `coinstats_api_key`.
- Secret VALUES never appear in logs, exceptions, or `repr` (masked).

### `dexpaprika.wallets`

- `validation.validate_address(chain_family, address) -> str` — returns the
  NORMALIZED address or raises `AddressValidationError` (message says what's
  wrong; never echoes secrets — addresses are public, echoing them is fine).
  - `evm`: `0x`+40 hex; mixed-case must pass EIP-55 (vendored keccak-256,
    `dexpaprika._keccak`, test-vector pinned); normalized form = EIP-55.
  - `btc`: Base58Check P2PKH (`1…`)/P2SH (`3…`) with sha256d checksum, or
    bech32/bech32m (`bc1q…` v0 len 20/32, `bc1p…` v1 len 32; BIP-173/350
    rules, no mixed case); normalized: bech32 lowercased, base58 as-is.
  - `solana`: base58 decoding to exactly 32 bytes; normalized as-is.
- `registry.WalletRegistry(path)` — persisted at `data_dir/wallets.json`,
  atomic write (tmp + `os.replace`). `Wallet` model: `chain_family`,
  `address` (normalized), `label` (optional, unique if present), `included`
  (default true), `added_at` (UTC ISO). Ops: `add`, `remove`, `set_included`,
  `list_wallets`. Dedup key: `(chain_family, normalized address)` — EVM
  compare case-insensitively. Corrupt file → `RegistryError` telling the
  operator the path and how to recover; never silently reset.
- Storage note: JSON document in S1; S2 may migrate the backend behind the
  same API without CLI change (ADR in PROGRESS.md — avoids building a table
  before the migrations runner exists).

### CLI (extends S0 contract; all subcommands accept `--json`)

```
dexpaprika wallets list
dexpaprika wallets add --chain evm|btc|solana --address <addr> [--label <name>]
dexpaprika wallets remove  (--address <addr> | --label <name>)
dexpaprika wallets include (--address <addr> | --label <name>)
dexpaprika wallets exclude (--address <addr> | --label <name>)
```

- Exit codes: 0 ok; 1 operational failure (invalid address, duplicate, not
  found, corrupt registry) with a JSON error payload `{"error": ...}` on
  stdout in `--json` mode; 2 usage (argparse).
- `status` additionally reports wallet counts (total/included per family) and
  a non-secret config summary.
- `healthcheck` implements two real checks — `secrets_present` (`ntfy_topic`
  resolvable via the provider; alert-only operation depends on it) and
  `data_dir_writable` — remaining checks stay `not-implemented`; exit stays 3
  until ALL checks pass (standards §2).

## Error cases

- Invalid address (each family, each failure mode) → exit 1, reason in message.
- Duplicate wallet / duplicate label → exit 1.
- Remove/include/exclude of unknown wallet → exit 1, lists known selectors.
- Corrupt `wallets.json` → exit 1 `RegistryError` with path + recovery hint.
- Missing secret → healthcheck check `fail`, process does not crash.
- Keyring backend unavailable (Linux VM) → `None`, env fallback engages.

## Standards obligations

- Parse, don't validate-later: all CLI input validated at the boundary;
  registry file parsed through pydantic models.
- Decimal for money config; parse from strings (§1).
- No secret values in logs/output/errors; masked reprs (§3).
- Writes confined to `data_dir`; atomic replace (§3, §2 idempotence).
- New runtime deps: `pydantic`, `pydantic-settings`, `keyring` (lockfile
  commit is its own `chore(deps)` commit with reason).

## Acceptance criteria

- All S1 tests (written first) pass offline with zero human action; full
  suite + static gates green; coverage ≥90% on validation/registry (core
  logic), ≥80% overall.
- Fresh-agent verifier PASS on the section branch commit.

## Probe gate (Step 2b) assessment

S1 reads no external API or on-chain source (config, local files, OS keyring
only) — probe N/A. Address-validation correctness is pinned instead by
published test vectors (EIP-55 examples, BIP-173/350 vectors, Bitcoin genesis
address, Solana system/token program ids) plus Richard's live wallet address
from VERIFIED_FINDINGS (public, already committed in loop docs).

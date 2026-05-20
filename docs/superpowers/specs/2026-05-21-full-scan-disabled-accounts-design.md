# Main full-scan behavior for disabled accounts and scan pacing

## Context

`CPACodexKeeper` currently has three related processing paths:

- main full scan via `run()` / `run_forever()`
- log-driven inspection via `run_fill_once()` / `process_fill_token()`
- tracked disabled-account rechecks via `_run_tracked_recheck()`

Today, the main full scan fetches all codex accounts, but `process_token()` may skip disabled accounts before checking usage when:

- the account is currently disabled, and
- the account exists in `disabled_accounts.json`, and
- `next_check_at` has not arrived yet

This causes full scans to behave differently from the user’s expectation. It can miss quota recovery or fail to backfill `disabled_accounts.json` for already-disabled accounts that are still over quota.

## Goal

Make full scans authoritative and complete in both daemon mode and one-shot mode:

1. Full scans must inspect every codex account, even if the account is already disabled.
2. Full scans must not rely on `disabled_accounts.json` to decide whether a disabled account should be scanned.
3. If an account is disabled and quota is now healthy, full scan should try to enable it.
4. If an account is enabled and quota is over threshold, full scan should disable it and create/update tracked recheck state.
5. If an account is disabled, quota is still over threshold, and it is not in `disabled_accounts.json`, full scan should backfill tracked recheck state.
6. Full scans must run account-by-account with pacing between accounts to avoid busy pressure.
7. The pacing rule applies only to main full scans, not to log inspection or tracked rechecks.

## Non-goals

This change does not redesign the log-driven inspection path or tracked recheck priority model.

This change does not make `disabled_accounts.json` the source of truth for whether an account is disabled. CPA account state remains the source of truth; the JSON file only records keeper-managed future recheck plans.

## Current behavior summary

### Main full scan

`run()` calls `_process_tokens_with_priority()` after fetching all codex tokens. The current implementation uses a thread pool and processes multiple accounts concurrently.

Inside `process_token()`, disabled accounts with a future `tracked_next_check_at` are skipped before any live usage check occurs.

### Quota behavior for disabled accounts

Inside `_apply_quota_policy()`:

- disabled + below threshold + tracked entry present → may enable
- disabled + below threshold + tracked entry absent → stays disabled
- disabled + at/above threshold + tracked entry present → reschedules next check
- disabled + at/above threshold + tracked entry absent → stays disabled without backfilling tracked state

### Other paths

`process_fill_token()` already skips disabled accounts intentionally, because the log-driven path only disables and never enables.

`_run_tracked_recheck()` triggers `process_token(..., trigger_source="tracked_recheck")` and should keep its timer-driven semantics.

## Proposed design

### 1. Make full scan process every account sequentially

Replace the main full-scan worker-pool behavior with sequential processing in `_process_tokens_with_priority()`.

Behavior:

- process one token at a time
- keep existing full-scan priority acquire/release boundaries around each token
- after finishing one token, wait before starting the next token
- pacing applies in both `run()` and `run_forever()` because both use the same full-scan path

This keeps log inspection and tracked rechecks unchanged.

### 2. Add full-scan pacing between accounts

Introduce a small helper for main full-scan pacing, for example `_sleep_between_full_scan_tokens()`.

Behavior:

- sleep only between tokens during full scan
- target normal delay: 30 seconds
- allowed delay range: 10–60 seconds
- use bounded jitter inside that range so scans do not look excessively bursty or perfectly mechanical
- do not sleep after the final token in a round
- do not apply this pacing to `process_fill_token()` or `_run_tracked_recheck()`

If the implementation uses settings, the values should still default to the user-requested behavior. If settings are not introduced now, hardcoded defaults are acceptable for this task.

### 3. Remove disabled-account early skip from full scan

The early-return branch in `process_token()` that skips disabled accounts when `tracked_next_check_at` is still in the future should be removed for the main full-scan path.

Result:

- full scan always performs live status/quota inspection for disabled accounts
- `disabled_accounts.json` no longer suppresses scanning
- tracked recheck timing remains relevant only to the timer-driven recheck path

### 4. Decouple enable/disable decisions from tracked-state presence

Adjust `_apply_quota_policy()` so that decisions are based on current CPA disabled state and live quota, not on whether the account already has a tracked entry.

#### Disabled account, quota below threshold

New behavior:

- attempt enable regardless of whether the account exists in `disabled_accounts.json`
- if enable succeeds and a tracked entry exists, remove it
- if enable succeeds and no tracked entry exists, no tracked entry needs to be created

This allows full scan to recover accounts that were manually disabled earlier or were missing from tracked state.

#### Enabled account, quota at/above threshold

Keep current behavior:

- disable account
- compute `next_check_at`
- create/update tracked entry in `disabled_accounts.json`

#### Disabled account, quota at/above threshold

New behavior:

- keep account disabled
- compute or refresh `next_check_at`
- ensure tracked entry exists in `disabled_accounts.json`
- if entry was missing, backfill it

This ensures disabled over-quota accounts are always enrolled into later timer-based rechecks.

### 5. Preserve tracked recheck semantics

Tracked rechecks remain timer-driven and continue to use `process_token(..., trigger_source="tracked_recheck")`.

The key distinction becomes:

- full scan = authoritative round-based inspection for all codex accounts
- tracked recheck = scheduled follow-up mechanism for accounts keeper intends to retry later

The tracked file no longer acts as a gate for whether full scan may examine a disabled account.

## Error handling

- If usage check fails due to network issues, keep existing skip/network-error behavior.
- If enabling fails, keep the account disabled and leave any existing tracked entry intact.
- If a disabled over-quota account cannot be backfilled into tracked state because state persistence fails, log the failure and continue; do not incorrectly mark the account as enabled or healthy.
- If disabling succeeds but tracked-state persistence fails, keep the current error logging behavior so the operational mismatch is visible.

## Testing plan

Add or update tests in `tests/test_maintainer.py` to cover at least:

1. disabled account with healthy quota is enabled during full scan even when not tracked
2. disabled account with over-threshold quota is kept disabled and backfilled into tracked state when not tracked
3. disabled account is not skipped during full scan just because `next_check_at` is still in the future
4. enabled over-threshold account still disables and records next recheck time
5. main full scan processes tokens sequentially rather than via concurrent worker pool
6. main full scan waits between accounts, but does not wait after the final account
7. log-driven inspection path remains unchanged for disabled accounts
8. tracked recheck path remains unchanged and does not inherit full-scan pacing

## Recommended implementation scope

Primary files:

- `src/maintainer.py`
- `tests/test_maintainer.py`

Possible documentation touch-up after implementation:

- `README.md`
- `README.en.md`

## Acceptance criteria

The change is complete when all of the following are true:

- daemon-mode full scans inspect disabled accounts instead of skipping them based on tracked recheck time
- one-shot full scans behave the same way
- disabled healthy accounts can be re-enabled even if missing from `disabled_accounts.json`
- disabled over-quota accounts missing from `disabled_accounts.json` are backfilled into it
- enabled over-quota accounts are disabled and tracked as before
- full scans run one account at a time with 10–60 second pacing, normally around 30 seconds
- log-driven inspection and tracked rechecks do not inherit the new pacing behavior

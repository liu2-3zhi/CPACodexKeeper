# Force refresh for expiring cookies design

## Summary

Add a new forced-refresh capability for expiring cookies with two separate entry points:

- `--once --force-refresh` for an explicit one-shot forced refresh run
- `CPA_FORCE_REFRESH_ON_EXPIRY=true` for daemon-mode automatic forced refresh

The forced-refresh flow should temporarily disable accounts that are currently enabled, refresh and upload the token, then restore the original disabled/enabled state. If an account was manually disabled before refresh, it must remain disabled after a successful refresh.

A critical rule is that `--once` must ignore the environment variable. In one-shot mode, forced refresh is enabled only when the user explicitly passes `--force-refresh`.

## Goals

- Add an explicit `--force-refresh` flag for `--once`
- Add a new environment variable that enables forced refresh in daemon mode only
- Force-refresh expiring tokens even when they are currently enabled
- Preserve the account's original disabled/enabled state after a successful forced refresh
- Keep the existing non-forced refresh behavior intact for disabled tokens
- Cover the new CLI, settings, and maintainer behavior with focused tests

## Non-goals

- Changing the existing quota-disable or recheck rules
- Forcing refresh during monitor-only mode unless the daemon-mode maintainer path naturally reaches the token
- Changing log-inspection-only behavior to perform refresh work
- Refreshing tokens that do not have a refresh token
- Making `--once` infer forced-refresh behavior from environment configuration

## Current behavior

Today the maintainer only refreshes a token from `_apply_refresh_policy()` when all of these are true:

- the token still has positive remaining lifetime but is below the expiry threshold
- automatic refresh is enabled
- the token is still disabled at the end of quota handling

That means currently enabled accounts are never proactively refreshed by keeper. They are intentionally left to CPA's own automatic handling.

Also, `src/cli.py` currently supports `--once` and `-monitor`, but there is no explicit `--force-refresh` mode.

## Recommended approach

Keep one forced-refresh policy inside `CPACodexKeeper`, but give it two activation paths with different configuration sources:

- in daemon mode, activation comes from a new setting loaded from the environment
- in `--once` mode, activation comes only from the CLI flag

This keeps the actual refresh behavior unified while still respecting the requested policy split between one-shot and daemon execution.

## Configuration model

Add a new boolean setting:

- environment variable: `CPA_FORCE_REFRESH_ON_EXPIRY`
- default: `false`

Meaning:

- when `true`, daemon-mode full inspections may perform forced refresh for tokens that are below the expiry threshold, even if they are currently enabled
- when `false`, daemon mode keeps the current refresh behavior

Important policy split:

- daemon mode reads `CPA_FORCE_REFRESH_ON_EXPIRY`
- `--once` ignores `CPA_FORCE_REFRESH_ON_EXPIRY`
- `--once` enables forced refresh only when `--force-refresh` is passed explicitly

This rule must be reflected both in CLI wiring and in tests.

## CLI behavior

Add a new flag:

- `--force-refresh`
  - action: `store_true`
  - meaning in `--once`: force-refresh expiring tokens during the one-shot run

Argument interaction rules:

- `--once --force-refresh` is valid
- `--force-refresh` without `--once` should be rejected as invalid usage
- `-monitor --force-refresh` should be rejected as invalid usage
- plain `--once` keeps current behavior and ignores `CPA_FORCE_REFRESH_ON_EXPIRY`
- daemon mode without `--once` never reads the CLI flag because the parser should reject that combination

Using parser-level mutual exclusion / validation is preferred so unsupported combinations fail immediately and clearly.

## Runtime activation rules

### Once mode

When `--once` is used:

- do not read or honor `CPA_FORCE_REFRESH_ON_EXPIRY`
- pass a one-shot forced-refresh override into the maintainer only when `--force-refresh` is present
- otherwise run one-shot inspection with current non-forced behavior

### Daemon mode

When running the normal daemon full-inspection loop:

- use the new environment-backed setting to decide whether forced refresh is active
- no new CLI flag is needed in daemon mode

### Monitor-only mode

No change is required in monitor-only mode for this feature because it does not run the full token-processing loop where refresh decisions are made.

## Maintainer behavior

Introduce a forced-refresh path for expiring tokens.

This path should run when all of these are true:

- the token has a refresh token
- the token still has positive remaining lifetime
- the remaining lifetime is below the expiry threshold
- forced refresh is active for the current run

The flow should be:

1. capture the token's original disabled state
2. if originally enabled, temporarily disable it before refresh
3. attempt refresh
4. if refresh succeeds, upload the updated token data
5. restore the original disabled state
   - originally enabled → re-enable after success
   - originally disabled → keep disabled after success
6. log each transition clearly

If refresh fails after the helper has temporarily disabled an originally enabled token, the account should remain disabled and the logs should clearly say that refresh failed and the account was left disabled for safety / manual follow-up.

This is intentionally conservative and avoids silently returning an account to enabled state after a failed forced update attempt.

## Relationship to existing refresh behavior

Keep the current non-forced refresh path intact.

That means there are now two refresh modes:

- normal refresh: existing behavior, only for tokens that remain disabled after quota handling
- forced refresh: new behavior, for expiring tokens when the run configuration explicitly enables it

The forced path should not require quota-based disablement to happen first.

Implementation-wise, the cleanest approach is likely:

- preserve `_apply_refresh_policy()` semantics for the current path
- add a separate forced-refresh helper invoked from `process_token()` when forced refresh is active
- ensure the two paths do not both refresh the same token in one processing pass

## Logging expectations

Add explicit logs for the forced-refresh path so it is distinguishable from the existing refresh behavior.

At minimum log:

- forced refresh activated for this token because remaining lifetime is below threshold
- original account state (`enabled` or `disabled`)
- temporary disable before refresh when applicable
- refresh success or failure
- upload success or failure
- restored final state after success
- failure path leaves token disabled for manual handling when refresh fails after temporary disable

## Tests

### Settings tests

Add tests in `tests/test_settings.py` for:

- reading `CPA_FORCE_REFRESH_ON_EXPIRY=true`
- default value remains `false`

### CLI tests

Add tests in `tests/test_cli.py` for:

- parser accepts `--once --force-refresh`
- parser rejects `--force-refresh` without `--once`
- parser rejects `-monitor --force-refresh`
- once mode passes the forced-refresh override when the flag is present
- once mode ignores the environment-backed setting when the flag is absent

### Maintainer tests

Add tests in `tests/test_maintainer.py` for:

- forced refresh on an originally enabled token temporarily disables, refreshes, uploads, then re-enables
- forced refresh on an originally disabled token refreshes, uploads, and keeps disabled
- forced refresh does not run when there is no refresh token
- forced refresh does not run when remaining lifetime is above threshold
- refresh failure after temporary disable leaves the token disabled
- plain `--once` behavior still does not use daemon env activation
- existing normal disabled-token refresh behavior still works unchanged

## Acceptance criteria

- `--once --force-refresh` performs forced refresh for expiring eligible tokens
- plain `--once` ignores `CPA_FORCE_REFRESH_ON_EXPIRY`
- daemon mode can activate forced refresh via `CPA_FORCE_REFRESH_ON_EXPIRY=true`
- `--force-refresh` is rejected unless paired with `--once`
- originally enabled tokens are temporarily disabled and restored to enabled after successful forced refresh
- originally manually disabled tokens remain disabled after successful forced refresh
- refresh failure after temporary disable leaves the token disabled and logs that state clearly
- existing non-forced refresh behavior remains intact when forced refresh is not active

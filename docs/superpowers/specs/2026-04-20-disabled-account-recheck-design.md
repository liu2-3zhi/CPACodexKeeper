# Disabled account recheck design

## Summary

Add a persistent JSON state file to track the next time an automatically disabled account should be rechecked for possible re-enable. The keeper should only auto-enable accounts that it previously auto-disabled because quota usage reached `CPA_QUOTA_THRESHOLD`.

Manual disables must never be auto-enabled. State must survive process restarts.

## Goals

- Persist auto-disable recheck schedule across restarts
- Only schedule accounts disabled by quota policy
- Compute `next_check_at` from quota windows that actually reached `CPA_QUOTA_THRESHOLD`
- When threshold-reaching windows have no usable `reset_at`, fall back to a new env var
- Once a scheduled account reaches `next_check_at`, check usage first, then decide whether to enable or reschedule
- Keep changes concentrated in existing maintainer flow

## Non-goals

- Changing the meaning of `CPA_QUOTA_THRESHOLD`
- Auto-enabling manually disabled accounts
- Adding a database or extra external dependency
- Introducing worktrees or separate worker processes

## New configuration

Add one new environment variable:

- `CPA_QUOTA_RESET_NONE_RECHECK_SECONDS`
  - default: `18000`
  - used when quota reached the disable threshold but all threshold-reaching windows had `reset_at=None`
  - `next_check_at = now + CPA_QUOTA_RESET_NONE_RECHECK_SECONDS`

Existing `CPA_INTERVAL` keeps its current meaning and is reused when a scheduled account reaches its check time, usage is still above threshold, and the latest parsed usage still does not expose usable `reset_at` for the threshold-reaching windows.

## Persistent state file

Create a JSON file in the project root:

- `disabled_accounts.json`

Shape:

```json
{
  "token-a": {
    "next_check_at": 1777226053
  },
  "token-b": {
    "next_check_at": 1777000096
  }
}
```

Only automatically quota-disabled accounts are stored here.

If the file does not exist, treat it as empty state.
If the file is invalid JSON, log an error and treat it as empty state for that run.

## Scheduling rules

### When a healthy account becomes disabled by quota

After quota policy decides to disable the account and the disable operation succeeds:

1. Inspect only windows whose `used_percent >= CPA_QUOTA_THRESHOLD`
2. Collect their `reset_at` values if present
3. If at least one usable `reset_at` exists, set:
   - `next_check_at = max(reached_window_reset_ats)`
4. Otherwise set:
   - `next_check_at = now + CPA_QUOTA_RESET_NONE_RECHECK_SECONDS`
5. Persist the account entry into `disabled_accounts.json`
6. Log which path was used

### When an account is disabled and exists in JSON

If current time is still before `next_check_at`:

- skip usage checking for this account
- keep it disabled
- do not attempt refresh or re-enable in this round
- log that recheck is deferred until `next_check_at`

If current time is at or after `next_check_at`:

1. check usage
2. if usage is now below threshold for all relevant windows:
   - enable the account
   - remove the JSON entry on success
3. if usage still reaches threshold:
   - recompute from the latest parsed usage
   - if threshold-reaching windows now have usable `reset_at`, overwrite `next_check_at` with the new max reset time
   - otherwise overwrite `next_check_at = now + CPA_INTERVAL`
   - keep disabled and log the reason

### When an account is disabled and not in JSON

Treat it as manually disabled:

- do not auto-enable
- do not create a schedule entry unless this run itself disables it through quota policy
- existing expiry/refresh handling for disabled accounts should still follow current behavior where applicable

### When an account is deleted or successfully enabled

Remove any matching JSON entry.

## Runtime flow changes

## Current problem

The current disabled-account branch immediately re-enables accounts once usage falls below threshold. That does not preserve intent across restarts and does not respect delayed recheck based on reset timestamps.

## New flow

Within `process_token`:

1. load token detail
2. apply non-refreshable expiry deletion as today
3. determine whether the account is disabled and whether it is tracked in `disabled_accounts.json`
4. if disabled and tracked and `now < next_check_at`:
   - skip usage check entirely
   - return without enabling
5. otherwise continue with usage check
6. parse usage and apply quota policy
7. quota policy now also updates persistent schedule state:
   - on new auto-disable, write schedule
   - on scheduled recheck success, enable and remove schedule
   - on scheduled recheck failure, reschedule
   - on manually disabled account below threshold, keep disabled

This keeps the logic centered in `maintainer.py` without changing external API behavior.

## Code structure

Add small helper methods to `CPACodexKeeper`:

- `_load_disabled_accounts_state()`
- `_save_disabled_accounts_state()`
- `_get_tracked_next_check_at(name)`
- `_set_tracked_next_check_at(name, ts)`
- `_remove_tracked_account(name)`
- `_compute_next_check_at_from_usage(body_info, now, fallback_seconds)`
- `_should_skip_usage_for_tracked_disabled(name, now)`

State updates must be guarded by a lock because token processing is concurrent.

## Settings changes

In `src/settings.py`:

- add `DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS = 18000`
- add `quota_reset_none_recheck_seconds: int` to `Settings`
- read `CPA_QUOTA_RESET_NONE_RECHECK_SECONDS` as integer with minimum `1`

In `.env.example` and README files, document the new variable.

## Logging

Add explicit logs for:

- auto-disable scheduled with reset timestamp
- auto-disable scheduled with fallback env var because reset_at is missing
- tracked disabled account skipped until future `next_check_at`
- tracked disabled account reached check time and is being rechecked
- recheck still above threshold and rescheduled with latest reset timestamp
- recheck still above threshold and rescheduled with `CPA_INTERVAL`
- successful enable and state cleanup

## Error handling

- JSON load failure: log error, use empty state
- JSON save failure: raise so the per-token task is surfaced as task exception in current executor handling
- Missing schedule entry for disabled account: treat as manual disable
- Missing threshold-reaching `reset_at`: use configured fallback, not an error

## Tests

Add tests for:

1. quota disable schedules by max reset time among threshold-reaching windows only
2. quota disable falls back to `CPA_QUOTA_RESET_NONE_RECHECK_SECONDS` when needed
3. tracked disabled account before `next_check_at` skips usage check
4. tracked disabled account at `next_check_at` enables when usage is below threshold
5. tracked disabled account at `next_check_at` with threshold still reached uses new reset timestamp if available
6. tracked disabled account at `next_check_at` with threshold still reached and no reset timestamp uses `now + CPA_INTERVAL`
7. manually disabled account not in JSON is not auto-enabled
8. deleting an account removes stale schedule entry
9. state is loaded on startup and survives restart

## Acceptance criteria

- Auto-disabled accounts get persisted recheck times in `disabled_accounts.json`
- Manual disables are never auto-enabled
- Recheck scheduling uses only threshold-reaching windows
- Missing threshold reset times use the new env var on first schedule and `CPA_INTERVAL` on later rechecks
- Restarting the process preserves pending rechecks
- Existing quota disable, delete, and refresh behavior remains intact outside this new scheduling gate

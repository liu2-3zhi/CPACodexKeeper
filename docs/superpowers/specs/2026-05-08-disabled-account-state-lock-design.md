# Disabled account state lock design

## Summary

Add cross-instance file locking around `disabled_accounts.json` updates so multiple `CPACodexKeeper` instances can safely share the same tracked disabled-account schedule.

The keeper must retry lock acquisition for a bounded time, then fail explicitly without mutating disk, in-memory state, or timers. This prevents silent state loss that can cause scheduled rechecks to disappear and accounts to miss re-enable at the expected time.

## Goals

- Prevent `disabled_accounts.json` from being overwritten by concurrent keeper instances
- Preserve tracked recheck entries across concurrent add/remove/update operations
- Ensure scheduled rechecks still happen after their planned time instead of being lost by cross-instance races
- Add bounded retry with timeout when lock acquisition temporarily fails
- Keep current quota enable/disable business rules unchanged
- Keep the persistent state format as JSON in the project root

## Non-goals

- Replacing JSON state with sqlite or another database
- Reworking the timer architecture
- Merging daemon and fill monitor into a single keeper instance
- Changing quota threshold semantics or refresh policy semantics
- Adding best-effort writes after lock timeout

## Current problem

In daemon mode, `src/cli.py` creates two separate `CPACodexKeeper` instances that share the same `disabled_accounts.json` file:

- the main full-scan keeper
- the fill monitor keeper

Each instance has its own `_state_lock`, in-memory `_tracked_disabled_accounts`, and timer registry. The current implementation reloads the JSON file before updating it, but that only protects concurrent threads inside one instance. It does not serialize read-modify-write cycles across instances.

This creates two failure modes:

1. One instance can overwrite another instance's state update, removing a tracked `next_check_at` entry.
2. A scheduled recheck can disappear or be observed through stale in-memory state, so a disabled account is not re-enabled when it should be.

## Chosen approach

Use a lock file next to `disabled_accounts.json` and require every state mutation to run inside a cross-instance critical section.

Recommended dependency:

- `filelock`

State files:

- state file: `disabled_accounts.json`
- lock file: `disabled_accounts.json.lock`

The critical section covers the whole read-modify-write sequence:

1. acquire file lock
2. reload latest JSON from disk
3. apply one logical update
4. write temp file
5. atomically replace target file
6. refresh current instance memory from the committed payload
7. release lock

This gives serialized state transitions across keeper instances while preserving the existing JSON file contract.

## New configuration

Add two environment variables:

- `CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS`
  - default: `10`
  - minimum: greater than `0`
  - maximum wait time for acquiring the disabled-state file lock

- `CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS`
  - default: `0.2`
  - minimum: greater than `0`
  - delay between lock acquisition retries

These values control retry behavior only for tracked disabled-account state operations.

## Persistent state and lock files

Persistent state remains in the project root:

- `disabled_accounts.json`

Add sibling lock file:

- `disabled_accounts.json.lock`

JSON shape does not change:

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

## State update rules

All logical writes must go through one locked update entry point.

Covered operations:

- set or overwrite `next_check_at` for one token
- remove one tracked token
- any future tracked disabled-account state mutation

Locked update behavior:

1. attempt to acquire file lock
2. if acquisition fails, retry after `CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS`
3. stop retrying once total wait exceeds `CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS`
4. after lock acquisition, reload the latest on-disk state
5. apply the requested mutation to the reloaded state
6. persist via temp file + atomic replace
7. update current instance `_tracked_disabled_accounts` from the committed state
8. perform timer side effects only after the state commit succeeds

Timeout behavior:

- log an explicit error
- return failure to the caller
- do not write the JSON file
- do not mutate current instance `_tracked_disabled_accounts`
- do not arm or cancel timers

Write failure behavior:

- treat the operation as failed
- do not partially update memory or timers
- surface the failure to current caller flow

## Read consistency rules

The system does not need to lock every read, but key decisions must not rely indefinitely on stale instance memory.

Rules:

- `_start_tracked_rechecks()` must reload from disk before arming timers
- `_run_tracked_recheck(name)` must confirm the token still exists in the latest persisted state before processing it
- successful locked writes must refresh the current instance cache from committed state
- paths that decide whether a tracked disabled token has a `next_check_at` should use refreshed state after any successful local mutation

This keeps timers instance-local while making persistence authoritative.

## Runtime flow impact

### New disable by quota

When quota policy disables an account and computes `next_check_at`:

1. disable account through CPA as today
2. persist tracked schedule through locked state update
3. only if persistence succeeds:
   - update in-memory tracked state
   - arm or refresh timer
4. if persistence fails:
   - log error
   - leave current process state unchanged

### Successful re-enable at recheck time

When a tracked disabled account is below threshold and enable succeeds:

1. enable account through CPA as today
2. remove tracked schedule through locked state update
3. only if removal persistence succeeds:
   - remove local cache entry
   - cancel local timer

### Delete flow

When an account is deleted or cleanup should remove stale schedule:

- schedule removal must also go through the same locked state update path

### Startup recovery

When daemon mode starts:

- `_start_tracked_rechecks()` reloads the latest JSON state from disk
- timers are restored from persisted `next_check_at` values
- stale in-memory bootstrap state must not win over disk

## Code structure

Concentrate the change in `src/maintainer.py` by introducing a small locked state helper layer.

Recommended helper responsibilities:

- compute lock file path
- acquire lock with retry and timeout
- load current JSON state
- save JSON state atomically
- apply mutation function to latest state
- refresh instance cache after commit

Likely methods to add or reshape:

- `_disabled_accounts_lock_path()`
- `_locked_update_tracked_disabled_accounts(...)`
- `_reload_tracked_disabled_accounts_state()`
- `_save_disabled_accounts_state(payload)` or equivalent committed-save helper
- `_set_tracked_next_check_at(name, ts)` updated to use locked helper
- `_remove_tracked_account(name)` updated to use locked helper
- `_run_tracked_recheck(name)` updated to confirm persisted membership before processing

`threading.Timer` management stays outside the locked file write itself, but must run only after the state mutation is durably committed.

## Logging

Add explicit logs for:

- waiting to acquire disabled-state lock
- lock acquired after retry
- lock acquisition timeout
- tracked state update succeeded
- tracked state update failed without mutating local cache
- recheck skipped because persisted tracked entry no longer exists

These logs should make it obvious whether a missed re-enable was caused by quota policy, CPA action failure, or state-lock timeout.

## Error handling

- invalid JSON on load: log error and treat as empty state, as today
- lock timeout: explicit failure, no state mutation
- lock library exception: explicit failure, no state mutation
- temp write or replace failure: explicit failure, no memory/timer mutation
- manual disabled accounts not in persisted JSON: keep existing behavior and do not auto-enable

## Dependency changes

Add one third-party dependency:

- `filelock`

No other runtime dependency changes are required.

## Tests

Add or update tests for:

1. two keeper instances writing different tracked tokens to the same file preserve both entries
2. removing one tracked token does not remove another instance's newly written token
3. startup timer restoration uses latest disk state, not stale in-memory cache
4. recheck path confirms persisted tracked membership before processing
5. lock acquisition retries before succeeding
6. lock acquisition timeout leaves disk, in-memory state, and timers unchanged
7. successful locked add updates disk and arms timer
8. successful locked remove updates disk and cancels timer
9. scheduled re-enable still removes persisted tracked state after successful enable
10. new settings parse defaults and reject invalid timeout/retry values

## Acceptance criteria

- Concurrent keeper instances no longer overwrite each other's `disabled_accounts.json` updates
- Tracked `next_check_at` entries survive concurrent add/remove activity unless intentionally removed
- A due tracked account is not silently skipped because its schedule entry was lost by a race
- Lock contention retries automatically and stops after bounded timeout
- On lock timeout or write failure, no partial local state change is visible
- Existing quota disable/re-enable behavior remains unchanged apart from the new consistency guarantees

# Delete Guard Design

**Date:** 2026-04-20  
**Status:** Approved for planning

## Goal

Add a new environment variable that can globally disable auth-file deletion in CPACodexKeeper, while keeping existing disable, enable, refresh, log inspection, and timer-based recheck behavior stable.

## Scope

This design only covers one behavior change:

- introduce a new boolean environment variable `CPA_ALLOW_DELETE`
- when deletion is disabled, paths that previously deleted auth-files in the main inspection flow must disable the account instead
- deletion-to-disable fallback must **not** write to `disabled_accounts.json`
- deletion-to-disable fallback must append a readable history event into `delete_blocked_accounts.json`
- `.env.example` must be updated alongside user-facing documentation

Out of scope:

- changing log inspection behavior beyond preserving its current non-delete behavior
- changing timer priority coordination
- changing refresh policy
- introducing per-reason delete policies

## Current behavior

Deletion is currently allowed unconditionally in the main inspection flow.

Observed delete paths in `src/maintainer.py`:

1. usage returns `401` or `402`
2. token is expired and has no `refresh_token`
3. token has no `refresh_token` and quota reaches the disable threshold

Current log inspection (`run_fill_once` / `process_fill_token`) does not delete files now and should stay that way.

Configuration currently lives in `src/settings.py` as typed dataclass fields loaded from `.env` or process environment. Existing boolean flags already use `_read_bool(...)`, so the new delete control should follow that same pattern.

## Proposed behavior

### New configuration

Add:

- env var: `CPA_ALLOW_DELETE`
- type: boolean
- default: `true`

Semantics:

- `true`: keep current behavior
- `false`: do not delete auth-files; convert delete actions into disable actions

Accepted boolean values should match existing config parsing behavior:

- true set: `1`, `true`, `yes`, `on`
- false set: `0`, `false`, `no`, `off`

## Runtime rules

When `CPA_ALLOW_DELETE=false`, every main-flow delete path must be converted as follows:

- instead of calling the CPA delete endpoint, call the existing disable path
- if disable succeeds, count the token as disabled, not deleted
- do not record the token into `disabled_accounts.json`
- do not arm or restore a timer for this fallback disable

This applies uniformly to all current delete reasons in the main inspection flow:

- invalid / deactivated token (`401` / `402`)
- expired token without `refresh_token`
- quota-threshold hit without `refresh_token`

### State persistence

Delete-to-disable fallback is intentionally **not** treated as keeper-managed quota disable state.

At the same time, it should be persisted as a user-readable history log in a separate JSON file:

- file path: `delete_blocked_accounts.json`
- purpose: preserve a durable event history of accounts that were supposed to be deleted but were disabled instead because deletion was blocked by config
- persistence model: append-only history events, so records survive process restarts

That means:

- no `next_check_at` is written to `disabled_accounts.json`
- no timer is scheduled
- no auto re-enable is expected later from this fallback path
- a successful delete-to-disable fallback appends one history event into `delete_blocked_accounts.json`

This keeps the behavior aligned with the approved rule: fallback disables are terminal operational safeguards, not temporary quota windows.

### Event history file

Add a new user-readable history file in the project root:

- `delete_blocked_accounts.json`

Recommended JSON shape:

```json
{
  "events": [
    {
      "name": "token-a",
      "reason": "Token 无效或 workspace 已停用",
      "source_action": "delete",
      "trigger": "401_or_402",
      "updated_at": "2026-04-20 12:34:56"
    }
  ]
}
```

Field meanings:

- `name`: auth-file name
- `reason`: human-readable reason shown to users
- `source_action`: always `delete` for this feature, to make the fallback explicit
- `trigger`: normalized machine-readable trigger, such as `401_or_402`, `expired_without_refresh_token`, or `quota_without_refresh_token`
- `updated_at`: local formatted timestamp for human reading

Write policy:

- append one event only when delete-to-disable fallback succeeds
- do not write an event when disable fails
- keep the file pretty-printed for manual reading

## Logging and summaries

Logs should make the branch explicit.

When deletion is allowed, existing delete-oriented wording may remain.

When deletion is blocked by config, logs should clearly state that:

- deletion was requested by the existing logic
- deletion is disabled by `CPA_ALLOW_DELETE=false`
- the account is being disabled instead

Round summary should reflect the real final action:

- successful fallback actions should contribute to `disabled`
- they should not contribute to `dead`

This keeps the summary honest and avoids reporting “deleted” when nothing was deleted.

## Implementation shape

### Files to modify

- `src/settings.py`
- `src/maintainer.py`
- `.env.example`
- `README.md`
- `README.en.md`
- tests in `tests/test_settings.py`
- tests in `tests/test_maintainer.py`

A new root-level runtime data file will also be created by the application when needed:

- `delete_blocked_accounts.json`

### Settings layer

Add a new settings field:

- `allow_delete: bool = True`

Load it through `_read_bool("CPA_ALLOW_DELETE", True, env_values)`.

### Maintainer layer

Keep the change narrow by reusing existing action methods.

Preferred design:

- preserve `delete_token(...)` as the low-level delete operation
- add one small decision helper in `CPACodexKeeper` that handles “delete if allowed, otherwise disable”
- route all current delete reasons through that helper instead of open-coding the branch in multiple places

This avoids scattered conditionals and keeps the behavior consistent across all current delete triggers.

### Documentation layer

`.env.example` must include:

- the new variable
- a short bilingual explanation that it controls whether auth-files may be deleted
- the default value

`README.md` and `README.en.md` must describe:

- the new configuration field
- that `false` converts main-flow deletions into disable actions
- that this fallback disable does not enter `disabled_accounts.json`
- that successful fallback actions are recorded in `delete_blocked_accounts.json`

## Testing requirements

### Settings tests

Add coverage for:

- explicit `CPA_ALLOW_DELETE=false`
- default value is `true`

### Maintainer tests

Add coverage for each important behavior:

1. when delete is allowed, existing delete path still deletes
2. when delete is disabled, `401/402` path disables instead of deleting
3. when delete is disabled, expired-without-refresh path disables instead of deleting
4. when delete is disabled, quota-hit-without-refresh path disables instead of deleting
5. fallback disable does not write tracked recheck state
6. successful fallback disable appends an event to `delete_blocked_accounts.json`
7. failed fallback disable does not append an event to `delete_blocked_accounts.json`
8. round summary counts fallback as disabled, not dead

Tests should prefer the existing maintainer test style and keep the change tightly focused on behavior.

## Tradeoffs

### Why this design

This is the smallest coherent change because it:

- uses one global switch instead of multiple overlapping controls
- preserves the existing disable path instead of inventing new state transitions
- keeps `disabled_accounts.json` limited to true keeper-managed quota rechecks
- gives users a durable, readable history in `delete_blocked_accounts.json`
- minimizes surprises in daemon mode

### What it deliberately does not do

It does not try to determine whether a fallback-disabled token is “recoverable” later. That would require a larger policy design and would blur the current meaning of `disabled_accounts.json`.

## Acceptance criteria

The design is complete when all of the following are true:

- `CPA_ALLOW_DELETE` exists and defaults to `true`
- `.env.example` documents the variable
- main-flow deletes are converted to disables when the flag is `false`
- converted disables do not write `disabled_accounts.json`
- successful converted disables append readable events into `delete_blocked_accounts.json`
- summaries report converted actions as disabled, not deleted
- README Chinese and English docs describe the behavior
- targeted tests cover config parsing and delete-to-disable fallback behavior

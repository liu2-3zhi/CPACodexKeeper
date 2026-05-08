# Monitor-only mode design

## Summary

Add a new startup argument `-monitor` that starts only the usage-log polling loop and the tracked disabled-account recheck timers.

In this mode, the process must not start the full inspection loop.

`-monitor` is always a daemon-style runtime mode. If the user combines `-monitor` with `--once`, the CLI should reject the combination as invalid arguments.

## Goals

- Add a dedicated monitor-only startup mode without changing existing default startup behavior
- Keep usage-log inspection running in daemon mode
- Keep tracked disabled-account recheck timers active in monitor-only mode
- Ensure full inspection does not start when monitor-only mode is selected
- Keep the change concentrated in CLI wiring with minimal maintainer logic changes
- Cover the new behavior with focused CLI tests

## Non-goals

- Changing the behavior of the existing default daemon startup path
- Changing how usage-log inspection itself works
- Changing tracked recheck scheduling or priority rules
- Merging full inspection and usage-log inspection into one new runtime abstraction
- Renaming existing CLI flags such as `--once` or `--daemon`

## Current behavior

Today `src/cli.py` builds one main keeper and always follows one of two branches:

- daemon mode
  - start tracked recheck timers
  - optionally create a second keeper for usage-log polling when `CPA_USAGE_QUERY_INTERVAL > 0`
  - start full inspection forever on the main keeper
- once mode
  - run one full inspection and exit

That means there is currently no CLI mode that runs only the usage-log inspection loop while still preserving tracked recheck timers.

## Recommended approach

Add a new boolean CLI flag `-monitor` and handle it in `main()` before the existing daemon/once branch.

This keeps the new behavior explicit at the CLI boundary instead of pushing startup-mode branching deeper into `CPACodexKeeper`.

## CLI behavior

### New argument

Add a new argument:

- `-monitor`
  - action: `store_true`
  - meaning: start monitor-only daemon mode

The option name should stay exactly `-monitor` to match the requested startup parameter.

### Argument interaction rules

Rules:

- default invocation with no extra flag keeps existing daemon behavior
- `--once` still means one full inspection and exit
- `-monitor` means monitor-only daemon mode
- `-monitor` and `--once` are mutually exclusive

If both `-monitor` and `--once` are provided, parsing should fail with a clear argument error instead of silently picking one behavior.

## Runtime behavior

### Default daemon mode

No behavior change:

1. create shared `PriorityCoordinator`
2. create main keeper
3. start tracked recheck timers on the main keeper
4. when `CPA_USAGE_QUERY_INTERVAL > 0`, create a second keeper that shares the same coordinator and logger, then start `run_fill_forever(...)` in a background thread
5. run `run_forever(...)` on the main keeper

### Once mode

No behavior change:

1. create shared `PriorityCoordinator`
2. create main keeper
3. run one full inspection with `run()`
4. exit

### Monitor-only mode

New behavior:

1. create shared `PriorityCoordinator`
2. create one keeper instance
3. start tracked recheck timers with `_start_tracked_rechecks()`
4. start usage-log polling with `run_fill_forever(interval_seconds=settings.usage_query_interval_seconds)`
5. do not call `run_forever(...)`
6. do not create the extra full-inspection keeper/thread arrangement used by default daemon mode

Using a single keeper here is the simplest option because monitor-only mode does not run the full inspection loop concurrently.

## Disabled usage-log interval behavior

Monitor-only mode should not add special-case behavior for `CPA_USAGE_QUERY_INTERVAL=0`.

It should reuse the current `run_fill_forever()` and `run_fill_once()` behavior:

- the loop still starts
- each round logs that usage-log inspection is disabled
- tracked recheck timers still remain active in the same process

This keeps monitor-only mode consistent with the current maintainer behavior and avoids adding a second policy branch in CLI startup.

## Priority coordination

`-monitor` mode still needs the normal `PriorityCoordinator`.

Reason:

- usage-log inspection uses `log` priority
- tracked rechecks use `timer` priority
- tracked rechecks must continue to outrank usage-log work even when full inspection is absent

No coordinator behavior needs to change. Only the set of started runtime paths changes.

## Logging

No new logging subsystem is needed.

Expected observable behavior in monitor-only mode:

- tracked recheck timers can log their normal startup and execution messages
- usage-log inspection logs its daemon startup banner and polling rounds
- full inspection startup logs and full-inspection round logs do not appear, because that loop never starts

## Error handling

- configuration loading keeps existing behavior
- invalid `-monitor --once` combination should exit as a CLI argument error
- runtime exceptions inside `run_fill_forever()` or tracked rechecks keep their existing handling
- no fallback should start full inspection if monitor-only mode was explicitly selected

## Tests

Add CLI-focused tests in `tests/test_cli.py` for:

1. parser accepts `-monitor`
2. parser rejects `-monitor --once`
3. monitor-only mode starts tracked rechecks
4. monitor-only mode starts usage-log polling forever with `settings.usage_query_interval_seconds`
5. monitor-only mode does not start full inspection forever
6. monitor-only mode does not run one-shot full inspection
7. monitor-only mode does not create the extra background thread used by default daemon mode

The tests can stay at the CLI boundary by mocking `CPACodexKeeper`, `load_settings`, and `threading.Thread` as needed.

## Acceptance criteria

- Starting the program with `-monitor` launches usage-log polling only
- Starting the program with `-monitor` also starts tracked disabled-account recheck timers
- Starting the program with `-monitor` does not start the full inspection loop
- `-monitor` behaves as a daemon mode, not a one-shot mode
- `-monitor --once` is rejected as invalid arguments
- Existing default daemon mode and once mode behavior remain unchanged

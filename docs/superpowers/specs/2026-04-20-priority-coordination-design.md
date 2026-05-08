# Priority coordination design

## Summary

Add a shared in-process priority coordinator so the three runtime paths that can update `disabled_accounts.json` stop stepping on each other:

- timer-driven tracked recheck
- usage-log inspection
- full inspection

Priority must be:

1. timer recheck
2. usage-log inspection
3. full inspection

Preemption is cooperative and happens only at token boundaries. A path that is already processing one token is allowed to finish that token, but it must not start the next token if a higher-priority path is waiting.

## Goals

- Prevent `disabled_accounts.json` write conflicts without changing its format
- Enforce runtime priority: timer recheck > usage-log inspection > full inspection
- Keep current token-processing logic intact as much as possible
- Preserve token-level concurrency for full inspection
- Make the behavior easy to test with focused TDD cases

## Non-goals

- Replacing `disabled_accounts.json` with a database or external queue
- Hard-stopping a token in the middle of processing
- Rewriting the business rules for quota disable, re-enable, delete, or refresh
- Merging all runtime paths into one central event loop

## Current problem

Today the project has three independent execution paths that may eventually call `_set_tracked_next_check_at()` or `_remove_tracked_account()` and therefore write `disabled_accounts.json`:

- full inspection via `run_forever()` / `run()` / `process_token()`
- usage-log inspection via `run_fill_forever()` / `run_fill_once()` / `process_fill_token()`
- timer-based tracked recheck via `_run_tracked_recheck()`

There is already a state lock around state mutation, so single writes are protected. The problem is that the three paths run independently and can continue advancing work while a higher-priority path is trying to update the same account state. This creates unnecessary contention and makes the intended precedence unclear.

## Recommended approach

Use a shared `PriorityCoordinator` object that all keeper instances in the process share.

The coordinator does not know quota logic, token details, or CPA APIs. It only controls **who may start processing the next token**.

This preserves the current business logic while making path ordering explicit.

## Priority model

Define three priority levels:

- `timer` = 3
- `log` = 2
- `full` = 1

Rules:

- a higher-priority waiter blocks lower-priority paths from starting their next token
- a path that already started processing one token is allowed to finish that token
- multiple due timer rechecks are drained before log inspection or full inspection resumes
- log inspection blocks full inspection from starting further tokens while any timer work is pending

This exactly matches the agreed behavior:

- full inspection yields after the current token when log inspection is waiting
- log inspection yields after the current token when timer recheck is waiting
- multiple due timer rechecks execute first as a batch

## Coordinator API

Add a small helper class in `src/maintainer.py`:

- `request(priority)`
  - register that a path wants to run work at this priority
- `acquire_next(priority)`
  - block until this priority is allowed to start one more token-sized unit of work
- `release(priority)`
  - mark the current token-sized unit as finished and wake other waiters
- `drain_done(priority)`
  - helper for paths that register a batch of work and need to decrement pending counters cleanly

Internal state should stay small:

- currently active worker count by priority
- waiting or pending work count by priority
- a `threading.Condition`

The admission rule is simple:

- allow priority `P` to start only if there is no higher priority with pending work

This is enough; no fairness policy beyond priority is needed for this change.

## Runtime integration

### CLI wiring

`src/cli.py` should create one shared coordinator and pass it into both keeper instances:

- main keeper for full inspection and tracked timers
- fill keeper for usage-log inspection

This is required because today daemon mode uses two separate `CPACodexKeeper` instances, so priority state must live outside either individual keeper.

### Timer recheck path

When `_run_tracked_recheck(name)` fires:

1. register timer work with the coordinator
2. wait until timer priority is admitted
3. run the existing single-token recheck logic
4. release the coordinator slot

If multiple timer callbacks fire, each becomes pending timer work. Lower priorities remain blocked until the timer queue is drained.

### Usage-log inspection path

`run_fill_once()` already processes matched tokens one by one. Integrate the coordinator at the token boundary:

1. determine matched tokens as today
2. before each `process_fill_token(...)`, request/acquire log priority
3. after that token finishes, release log priority

This lets timer rechecks interrupt log inspection only between tokens.

### Full inspection path

The current full inspection submits the entire round into a thread pool immediately. That is too eager for cooperative priority control, because high-priority work may arrive after many low-priority tasks are already in flight.

So full inspection must switch to **worker-pulls-next-token** instead of **main-thread-submits-all-tokens**.

Recommended structure:

1. keep the existing thread pool
2. create a shared iterator or index over shuffled tokens
3. each worker repeatedly:
   - asks the coordinator to request/acquire full priority for one token
   - pulls exactly one next token
   - processes it with existing `process_token(...)`
   - releases full priority
4. stop when no tokens remain

This preserves full-inspection concurrency while ensuring high-priority work can stop further low-priority expansion at token boundaries.

## State file behavior

`disabled_accounts.json` format does not change.

Existing `_state_lock` remains in place and still guards in-memory state mutation plus file writes. The new coordinator does not replace `_state_lock`; it reduces contention by controlling admission order before writes are reached.

That means the locking layers are:

- coordinator: path-level priority and admission
- `_state_lock`: actual state mutation and file write serialization

## Logging

Add path-level logs for coordination so behavior is observable when debugging:

- full inspection paused because log inspection is waiting
- full inspection paused because timer recheck is waiting
- log inspection paused because timer recheck is waiting
- timer recheck acquired highest priority
- timer queue drained, lower priorities resumed

Keep these logs brief and only emit on actual waits or transitions, not on every token when there is no contention.

## Error handling

- if a token-processing path raises, it must still release its coordinator slot in `finally`
- if a waiting path gets cancelled or exits early, pending counters must be decremented correctly
- coordinator bugs must not bypass `_state_lock`; file writes still serialize even if admission is imperfect

## Tests

Add tests in `tests/test_maintainer.py` and `tests/test_cli.py` for:

1. full inspection yields after the current token when log inspection becomes pending
2. log inspection yields after the current token when timer recheck becomes pending
3. multiple timer rechecks drain before lower-priority work resumes
4. CLI wires one shared coordinator into both keeper instances in daemon mode
5. coordinator release happens even when token processing raises
6. existing tracked recheck, log inspection, and quota scheduling behavior still passes

Tests should avoid real sleeps by mocking the coordinator or using condition-controlled test doubles.

## Acceptance criteria

- Timer rechecks always outrank log inspection and full inspection
- Log inspection always outranks full inspection
- Preemption happens only after the current token finishes
- Multiple due timer rechecks are processed before lower-priority paths continue
- `disabled_accounts.json` format and semantics remain unchanged
- Existing quota disable / re-enable / refresh logic remains intact
- The daemon still runs full inspection, log inspection, and timer rechecks concurrently, but now under explicit priority control

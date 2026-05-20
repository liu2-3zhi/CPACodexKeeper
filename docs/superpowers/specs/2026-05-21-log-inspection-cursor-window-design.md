# Log inspection cursor-based windowing without CPA_USAGE_QUERY_INTERVAL

## Context

`CPACodexKeeper` currently runs a log-driven inspection path alongside the main full scan in daemon and monitor modes.

Today, that path uses two different settings:

- `CPA_FILL_INTERVAL` controls how often the log-inspection loop wakes up
- `CPA_USAGE_QUERY_INTERVAL` controls the fixed `lookback_seconds` sent to `/v0/management/usage`, and also acts as the enable/disable switch for log inspection

The current model mixes two separate concerns:

1. whether log inspection is enabled
2. how often the loop runs
3. how wide each usage-log query window is

It also relies on a fixed lookback window plus local filtering against `last_usage_query_time` and `_last_seen_usage_by_email`.

The desired behavior is different:

- remove `CPA_USAGE_QUERY_INTERVAL`
- use `CPA_FILL_INTERVAL <= 0` to disable the log-inspection loop
- on the first loop, only record a starting time
- on the second loop, query logs from that recorded start time until now
- on later loops, query starting from the last successfully seen log timestamp
- include the last seen timestamp in the next query window, then de-duplicate locally so same-second logs are not missed

## Goal

Replace fixed-window log inspection with in-memory cursor-based progression.

The new design should make the log-inspection query window follow actual observed usage-log progress instead of a static configuration value.

## Non-goals

This change does not redesign the main full scan or tracked disabled-account recheck behavior.

This change does not add cross-process persistence for the log-inspection cursor.

This change does not change the CPA management API contract; the keeper may still compute and send `lookback_seconds` if that remains the available request shape.

## Confirmed behavior decisions

The following behavior has already been chosen for this change:

1. `CPA_USAGE_QUERY_INTERVAL` is removed entirely from settings, docs, tests, and `.env.example`.
2. `CPA_FILL_INTERVAL <= 0` disables log inspection.
3. `CPA_FILL_INTERVAL > 0` enables log inspection and controls its polling frequency.
4. First log-inspection round: record the starting cursor time only; do not query CPA usage logs yet.
5. Second round: query logs covering the first recorded time through the current time.
6. Later rounds: query logs starting from the last successfully seen log timestamp.
7. Query boundaries are inclusive on the start timestamp, so `timestamp >= cursor_time` is eligible locally.
8. Local de-duplication must remain so same-second logs are not missed.
9. If CPA usage-log fetch fails, the cursor does not advance.
10. If CPA usage-log fetch succeeds but returns no eligible logs, the cursor advances to the current query-start time.
11. Cursor state is in-memory only; process restart returns to the first-round priming behavior.
12. `.env.example` must be updated to remove `CPA_USAGE_QUERY_INTERVAL`.

## Current behavior summary

### Settings and startup

- `src/settings.py` defines `usage_query_interval_seconds`
- `src/cli.py` starts the log-inspection thread only when `settings.usage_query_interval_seconds > 0`
- `src/maintainer.py` shows startup text based on whether `usage_query_interval_seconds == 0`

### Usage-log query path

- `CPAClient.get_usage_log()` accepts `lookback_seconds`
- `CPACodexKeeper.get_usage_log()` currently always passes `self.settings.usage_query_interval_seconds`
- `run_fill_once()` primes `last_usage_query_time` on first run, then fetches a fixed lookback window on later runs
- local filtering uses `last_usage_query_time` with overlap and `_last_seen_usage_by_email`

### Why this needs to change

The current model keeps querying a configuration-sized historical window even when the keeper already knows the last successfully observed log time. That adds unnecessary coupling between polling frequency and query span, and it makes `.env.example` and README explain a concept the user no longer wants to expose.

## Proposed design

### 1. Remove CPA_USAGE_QUERY_INTERVAL and make CPA_FILL_INTERVAL the only log-inspection control

Configuration changes:

- remove `DEFAULT_USAGE_QUERY_INTERVAL_SECONDS`
- remove `usage_query_interval_seconds` from `Settings`
- remove all parsing and validation for `CPA_USAGE_QUERY_INTERVAL`
- remove `CPA_USAGE_QUERY_INTERVAL` from `.env.example`
- update README and README.en so log inspection is described as enabled when `CPA_FILL_INTERVAL > 0` and disabled when `CPA_FILL_INTERVAL <= 0`

Startup / CLI changes:

- `src/cli.py` should start the log-inspection thread only when `settings.fill_interval_seconds > 0`
- monitor mode should still call `run_fill_forever(interval_seconds=settings.fill_interval_seconds)`; inside the loop, non-positive values must be handled as disabled behavior
- startup logging should no longer reference `CPA_USAGE_QUERY_INTERVAL`

### 2. Replace fixed-window querying with in-memory cursor progression

Introduce clearer in-memory state for the log-inspection path. The exact field names may vary, but the model should separate:

- whether the loop has been primed yet
- the current inclusive start cursor time
- the per-email last-seen timestamps used for local de-duplication

Recommended semantics:

- before first success, `log_cursor_time` is `None`
- first `run_fill_once()` call records `log_cursor_time = now`, logs priming, and returns without calling CPA
- second and later calls compute a dynamic query window based on `now - log_cursor_time`
- the local acceptance filter keeps only usage records whose timestamp is `>= log_cursor_time`
- if eligible records exist, advance `log_cursor_time` to the maximum timestamp actually accepted in this round
- if fetch succeeds but yields no eligible records, advance `log_cursor_time` to `query_started_at`
- if fetch fails, keep `log_cursor_time` unchanged

This preserves the user’s desired “first record, then query from record time, then query from last log time” model.

### 3. Keep inclusive cursoring and local de-duplication together

Because later rounds must start from the previous last log timestamp inclusively, the implementation must prevent duplicate reprocessing.

Recommended behavior:

- keep `_last_seen_usage_by_email` or an equivalent structure
- when filtering usage records, accept a record only if:
  - its timestamp is `>= log_cursor_time`, and
  - it is newer than the last seen timestamp already recorded for that email
- after processing, update the per-email last-seen map with the newest accepted timestamp for each email

This ensures the inclusive boundary does not reprocess already-handled records while still protecting against missing multiple records that share the same second.

### 4. Failure and empty-result behavior

The cursor-advance rules should be explicit:

#### CPA usage-log request fails

- return a skipped/error outcome consistent with current behavior
- do not update the cursor
- do not update `_last_seen_usage_by_email`

This ensures the keeper retries the same logical window next time instead of silently skipping data.

#### CPA usage-log request succeeds but there are no eligible records

- treat the round as successful but empty
- advance the cursor to `query_started_at`
- do not update `_last_seen_usage_by_email`

This prevents repeatedly re-querying an empty historical interval forever.

#### CPA usage-log request succeeds with eligible records

- process matched tokens as today
- update per-email last-seen timestamps
- advance the cursor to the maximum accepted record timestamp

### 5. Preserve the existing CPA API shape if possible

`CPAClient.get_usage_log()` already accepts `lookback_seconds`, and the current management API appears to be based on that parameter.

To minimize change surface:

- keep the CPA client request shape unchanged if the server API still expects `lookback_seconds`
- compute `lookback_seconds` dynamically inside the maintainer from `query_started_at - log_cursor_time`
- clamp negative values to zero if needed

This avoids a broader API-layer redesign while still delivering cursor-based behavior.

### 6. Update docs and config template to match the new model

The following documentation must be aligned:

- `.env.example`
- `README.md`
- `README.en.md`
- `CLAUDE.md` only if it references the removed setting in a way that becomes incorrect

Documentation should clearly describe:

- `CPA_FILL_INTERVAL` now controls both log-inspection enable/disable and polling frequency
- first round primes the start time only
- later rounds use cursor-based progression through usage logs
- cursor state is not persisted across process restarts

## Error handling

- If `CPA_FILL_INTERVAL <= 0`, `run_fill_once()` should log that log inspection is disabled and skip work cleanly.
- If dynamic `lookback_seconds` computes to zero or negative on a later round, the request may still be made with zero or be short-circuited locally depending on implementation preference; either way behavior should remain correct and non-crashing.
- If usage data is malformed or missing timestamps, keep existing defensive filtering and simply avoid advancing cursor from invalid records.
- If no matching codex accounts remain after log filtering, the round should still update cursor state according to the success/empty rules above.

## Testing plan

Update or add tests to cover at least the following:

1. settings no longer expose `CPA_USAGE_QUERY_INTERVAL`
2. `.env.example` no longer includes `CPA_USAGE_QUERY_INTERVAL`
3. log inspection is disabled when `CPA_FILL_INTERVAL <= 0`
4. first `run_fill_once()` call only primes the cursor and does not call CPA usage logs
5. second `run_fill_once()` call queries from primed time to now
6. later `run_fill_once()` calls query from the last accepted log timestamp
7. inclusive cursoring plus local de-duplication does not reprocess same-second logs but also does not miss them
8. fetch failure does not advance cursor
9. successful empty response advances cursor to current query-start time
10. CLI daemon-mode startup only spawns the log-inspection thread when `CPA_FILL_INTERVAL > 0`
11. README and `.env.example` stay aligned with the new setting model

Primary files likely to change:

- `src/settings.py`
- `src/cli.py`
- `src/maintainer.py`
- `src/cpa_client.py` (possibly only tests or call sites)
- `.env.example`
- `README.md`
- `README.en.md`
- `tests/test_settings.py`
- `tests/test_maintainer.py`
- `tests/test_cpa_client.py`
- `tests/test_project_files.py` if config-template assertions are added

## Acceptance criteria

The change is complete when all of the following are true:

- `CPA_USAGE_QUERY_INTERVAL` is fully removed from runtime settings, config template, and docs
- `CPA_FILL_INTERVAL <= 0` disables the log-inspection path
- first log-inspection round only records a starting cursor time
- second round queries from that start time to the current time
- later rounds query from the last successfully accepted log timestamp inclusively
- local de-duplication prevents same-second duplicate processing while avoiding missed records
- failed usage-log fetches do not advance cursor
- successful empty usage-log fetches advance cursor to the round’s query-start time
- cursor state is in-memory only and resets on process restart

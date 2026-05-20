# Log Inspection Cursor Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed-window log inspection based on `CPA_USAGE_QUERY_INTERVAL` with in-memory cursor-based progression controlled only by `CPA_FILL_INTERVAL`, while keeping CPA API usage compatible and updating docs, config template, and tests.

**Architecture:** Keep the existing `CPAClient.get_usage_log(lookback_seconds=...)` API shape, but move query-window calculation into `CPACodexKeeper.run_fill_once()` using an in-memory inclusive cursor and per-email last-seen de-duplication. Remove `CPA_USAGE_QUERY_INTERVAL` from settings and docs, treat `CPA_FILL_INTERVAL <= 0` as the log-inspection disable switch, and keep cursor state process-local so restart returns to the priming-first behavior.

**Tech Stack:** Python 3.11, unittest, unittest.mock, existing `src/maintainer.py` / `src/settings.py` / `src/cli.py` architecture

---

## File map

- `src/settings.py`
  - Remove the `CPA_USAGE_QUERY_INTERVAL` setting and its validation path.
  - Keep `CPA_FILL_INTERVAL` as the only log-inspection interval control.
- `src/cli.py`
  - Change daemon-mode thread startup so log inspection is enabled only when `settings.fill_interval_seconds > 0`.
- `src/maintainer.py`
  - Main behavior changes live here.
  - Key functions and state: `__init__`, `get_usage_log()`, `_new_usage_timestamp_by_email()`, `log_startup()`, `run_fill_once()`, `run_fill_forever()`.
- `src/cpa_client.py`
  - Likely no production change beyond keeping `lookback_seconds`; test expectations remain important.
- `.env.example`
  - Remove `CPA_USAGE_QUERY_INTERVAL` and update `CPA_FILL_INTERVAL` comments to describe both enable/disable and polling behavior.
- `README.md`
  - Update Chinese docs for log-inspection enable rules, query behavior, and removed config.
- `README.en.md`
  - Update English docs to match the new cursor-based model.
- `tests/test_settings.py`
  - Remove tests for `CPA_USAGE_QUERY_INTERVAL`; add tests around non-positive `CPA_FILL_INTERVAL` if needed.
- `tests/test_maintainer.py`
  - Add focused tests for priming, cursor advancement, empty success, failure retry, inclusive cursoring, and daemon startup semantics.
- `tests/test_cpa_client.py`
  - Keep the existing request-shape test passing with dynamic `lookback_seconds`; update only if the exact calling path changes.
- `tests/test_project_files.py`
  - Add assertions that `.env.example` no longer contains the removed config if desired.

## Implementation order

1. Lock in the new runtime contract with failing maintainer and settings tests.
2. Remove `CPA_USAGE_QUERY_INTERVAL` from settings and daemon thread-start conditions.
3. Implement cursor-based log inspection in `run_fill_once()` with explicit success/empty/failure advancement rules.
4. Update `.env.example`, README, and README.en to match the new model.
5. Run targeted tests, then broader regression tests.
6. Commit `new` and sync only business/doc files needed for `main`.

## Regression risks to watch

- Accidentally breaking main full scan or tracked recheck behavior while editing shared `CPACodexKeeper` state.
- Advancing the cursor on CPA request failure and silently skipping logs.
- Treating the cursor as exclusive and missing same-second records.
- Losing local de-duplication, causing repeated processing of the same account on each loop.
- Making `CPA_FILL_INTERVAL <= 0` break monitor/daemon loops instead of cleanly disabling the log-inspection work.
- Leaving stale references to `CPA_USAGE_QUERY_INTERVAL` in `.env.example`, README, README.en, or `CLAUDE.md`.
- Accidentally syncing `CLAUDE.md` to `main` when only business/doc changes should propagate there.

### Task 1: Replace the old settings contract with failing tests for the new log-inspection model

**Files:**
- Modify: `tests/test_settings.py:66-79`
- Modify: `tests/test_settings.py:207-233`
- Modify: `tests/test_maintainer.py:560-620`
- Modify: `tests/test_maintainer.py` near existing `run_fill_once` coverage

- [ ] **Step 1: Remove the old settings tests and add a failing test that `CPA_FILL_INTERVAL` may disable log inspection**

Replace the old `CPA_USAGE_QUERY_INTERVAL`-specific settings assertions with tests that preserve `fill_interval_seconds` and allow non-positive values.

```python
def test_load_settings_reads_fill_interval_zero(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
            "CPA_FILL_INTERVAL": "0",
        },
        clear=True,
    ):
        settings = load_settings()

    self.assertEqual(settings.fill_interval_seconds, 0)
```

```python
def test_load_settings_reads_fill_interval_negative_one(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
            "CPA_FILL_INTERVAL": "-1",
        },
        clear=True,
    ):
        settings = load_settings()

    self.assertEqual(settings.fill_interval_seconds, -1)
```

- [ ] **Step 2: Run the two new settings tests to verify they fail under current code**

Run: `pytest tests/test_settings.py::SettingsTests::test_load_settings_reads_fill_interval_zero tests/test_settings.py::SettingsTests::test_load_settings_reads_fill_interval_negative_one -v`

Expected: FAIL because `src/settings.py` currently validates `CPA_FILL_INTERVAL` with `minimum=1`.

- [ ] **Step 3: Add a failing maintainer test for priming-only first run**

Add a focused test near existing log-inspection tests.

```python
def test_run_fill_once_primes_cursor_without_requesting_usage_logs(self):
    self.maintainer.settings.fill_interval_seconds = 10
    self.maintainer.get_usage_log = Mock()

    with patch("src.maintainer.time.time", return_value=1000):
        result = self.maintainer.run_fill_once()

    self.assertEqual(result, "primed")
    self.assertEqual(self.maintainer.last_usage_query_time, 1000)
    self.maintainer.get_usage_log.assert_not_called()
```

- [ ] **Step 4: Run the priming test to confirm current behavior baseline**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_run_fill_once_primes_cursor_without_requesting_usage_logs -v`

Expected: PASS or near-pass baseline; if it already passes, keep it as an anchor before adding later failing tests.

- [ ] **Step 5: Add a failing maintainer test for disabling log inspection when `CPA_FILL_INTERVAL <= 0`**

```python
def test_run_fill_once_skips_when_fill_interval_is_zero(self):
    self.maintainer.settings.fill_interval_seconds = 0
    self.maintainer.get_usage_log = Mock()

    result = self.maintainer.run_fill_once()

    self.assertEqual(result, "disabled")
    self.maintainer.get_usage_log.assert_not_called()
```

- [ ] **Step 6: Run the disable test to verify it fails under current code**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_run_fill_once_skips_when_fill_interval_is_zero -v`

Expected: FAIL because `run_fill_once()` currently gates on `usage_query_interval_seconds`, not `fill_interval_seconds`.

- [ ] **Step 7: Add a failing maintainer test for “success but no logs advances cursor to current query-start time”**

```python
def test_run_fill_once_advances_cursor_on_successful_empty_usage_logs(self):
    self.maintainer.settings.fill_interval_seconds = 10
    self.maintainer.last_usage_query_time = 1000
    self.maintainer.get_usage_log = Mock(return_value={"usage": {"apis": {}}})
    self.maintainer.get_fill_token_map = Mock(return_value={})

    with patch("src.maintainer.time.time", return_value=1050):
        result = self.maintainer.run_fill_once()

    self.assertEqual(result, "processed")
    self.assertEqual(self.maintainer.last_usage_query_time, 1050)
```

- [ ] **Step 8: Run the empty-success test and verify it fails**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_run_fill_once_advances_cursor_on_successful_empty_usage_logs -v`

Expected: FAIL because current logic still depends on fixed-window querying and existing timestamp semantics.

- [ ] **Step 9: Add a failing maintainer test for fetch failure preserving cursor**

```python
def test_run_fill_once_does_not_advance_cursor_on_usage_log_failure(self):
    self.maintainer.settings.fill_interval_seconds = 10
    self.maintainer.last_usage_query_time = 1000
    self.maintainer.get_usage_log = Mock(return_value=None)

    with patch("src.maintainer.time.time", return_value=1050):
        result = self.maintainer.run_fill_once()

    self.assertEqual(result, "skipped")
    self.assertEqual(self.maintainer.last_usage_query_time, 1000)
```

- [ ] **Step 10: Run the failure-preserves-cursor test and verify it fails if current behavior advances the timestamp**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_run_fill_once_does_not_advance_cursor_on_usage_log_failure -v`

Expected: FAIL if current code updates the timestamp on this path; otherwise keep as a passing safety anchor.

- [ ] **Step 11: Commit the red/anchor tests**

```bash
git add tests/test_settings.py tests/test_maintainer.py
git commit -m "test: capture cursor-based log inspection behavior"
```

### Task 2: Remove CPA_USAGE_QUERY_INTERVAL from settings and daemon startup wiring

**Files:**
- Modify: `src/settings.py:6-25`
- Modify: `src/settings.py:33-57`
- Modify: `src/settings.py:157-218`
- Modify: `src/cli.py:38-53`
- Test: `tests/test_settings.py:66-79, 207-233`

- [ ] **Step 1: Remove the old default and dataclass field from `src/settings.py`**

Delete:

```python
DEFAULT_USAGE_QUERY_INTERVAL_SECONDS = 7200
```

and remove this dataclass field:

```python
usage_query_interval_seconds: int = DEFAULT_USAGE_QUERY_INTERVAL_SECONDS
```

- [ ] **Step 2: Relax `CPA_FILL_INTERVAL` validation so it accepts zero and negative values**

Change:

```python
fill_interval_seconds=_read_int("CPA_FILL_INTERVAL", DEFAULT_FILL_INTERVAL_SECONDS, env_values, minimum=1),
```

to:

```python
fill_interval_seconds=_read_int("CPA_FILL_INTERVAL", DEFAULT_FILL_INTERVAL_SECONDS, env_values),
```

This keeps integer parsing but removes the positive-only restriction.

- [ ] **Step 3: Remove `CPA_USAGE_QUERY_INTERVAL` parsing from `load_settings()`**

Delete the block:

```python
usage_query_interval_seconds=_read_int(
    "CPA_USAGE_QUERY_INTERVAL",
    DEFAULT_USAGE_QUERY_INTERVAL_SECONDS,
    env_values,
    minimum=0,
),
```

- [ ] **Step 4: Run the targeted settings tests and verify they pass**

Run: `pytest tests/test_settings.py::SettingsTests::test_load_settings_reads_fill_interval_zero tests/test_settings.py::SettingsTests::test_load_settings_reads_fill_interval_negative_one -v`

Expected: PASS.

- [ ] **Step 5: Change daemon-mode log thread startup to key off `fill_interval_seconds > 0`**

In `src/cli.py`, replace:

```python
if settings.usage_query_interval_seconds > 0:
```

with:

```python
if settings.fill_interval_seconds > 0:
```

Keep the rest of the spawned thread logic unchanged.

- [ ] **Step 6: Add a CLI-facing regression test if an appropriate test location already exists; otherwise cover the startup rule through maintainer logging/tests**

If there is no existing CLI test file, do not create a new broad framework. Instead, cover the behavior through maintainer-level disable tests plus README/config updates.

- [ ] **Step 7: Commit the settings/startup contract change**

```bash
git add src/settings.py src/cli.py tests/test_settings.py
git commit -m "refactor: remove usage query interval setting"
```

### Task 3: Implement cursor-based log inspection in `src/maintainer.py`

**Files:**
- Modify: `src/maintainer.py:120-140`
- Modify: `src/maintainer.py:234-245`
- Modify: `src/maintainer.py:614-646`
- Modify: `src/maintainer.py:1158-1177`
- Modify: `src/maintainer.py:1279-1321`
- Test: `tests/test_maintainer.py` around existing log-inspection coverage

- [ ] **Step 1: Keep the in-memory cursor state but rename it if needed for clarity**

You may keep `last_usage_query_time` as the cursor field if you want a smaller diff, but update its semantics in code comments / logs so it now means the inclusive log-inspection cursor, not a fixed-window query checkpoint.

If renaming, keep it small and local:

```python
self.log_cursor_time: int | None = None
self._last_seen_usage_by_email: dict[str, int] = {}
```

- [ ] **Step 2: Change `get_usage_log()` to accept a dynamic lookback value**

Replace:

```python
def get_usage_log(self):
    return self.cpa_client.get_usage_log(lookback_seconds=self.settings.usage_query_interval_seconds)
```

with:

```python
def get_usage_log(self, *, lookback_seconds: int):
    return self.cpa_client.get_usage_log(lookback_seconds=lookback_seconds)
```

- [ ] **Step 3: Update `_new_usage_timestamp_by_email()` to accept an inclusive cursor time instead of overlap semantics**

Replace the current signature and filter logic:

```python
def _new_usage_timestamp_by_email(self, usage_data, *, overlap_seconds=0):
```

with a cursor-based version:

```python
def _new_usage_timestamp_by_email(self, usage_data, *, cursor_time: int | None):
```

Inside it, accept a record only if:

```python
if cursor_time is not None and ts < cursor_time:
    continue
last_seen = self._last_seen_usage_by_email.get(email)
if last_seen is not None and ts <= last_seen:
    continue
```

This preserves inclusive cursoring while still locally de-duplicating already-seen per-email entries.

- [ ] **Step 4: Rewrite `run_fill_once()` around priming + dynamic lookback + explicit cursor advancement rules**

Use this structure:

```python
def run_fill_once(self):
    if self.settings.fill_interval_seconds <= 0:
        self.log("INFO", "日志巡检已禁用：CPA_FILL_INTERVAL<=0，跳过本轮CPA使用日志扫描")
        return "disabled"

    now = int(time.time())
    if self.last_usage_query_time is None:
        self.last_usage_query_time = now
        self.log("INFO", "日志巡检首次启动：已记录起始查询时间，下一轮开始比对新增日志")
        return "primed"

    query_started_at = now
    cursor_time = self.last_usage_query_time
    lookback_seconds = max(0, query_started_at - cursor_time)
    usage_data = self.get_usage_log(lookback_seconds=lookback_seconds)
    if not usage_data:
        self.log("WARN", "日志巡检未获取到CPA日志数据：本轮无法筛选新增调用账号")
        return "skipped"

    latest_by_email = self._new_usage_timestamp_by_email(
        usage_data,
        cursor_time=cursor_time,
    )
    token_map = self.get_fill_token_map()
    matched_tokens = []
    for email in latest_by_email:
        matched_tokens.extend(token_map.get(email, []))

    total = len(matched_tokens)
    if total:
        self.reset_stats()
        self._set_total(total)
        self.log("INFO", f"日志巡检命中 {total} 个账号：开始逐个校验额度并按需禁用")
        for idx, token_info in enumerate(matched_tokens, 1):
            self._acquire_priority("log")
            try:
                self.process_fill_token(token_info, idx, total)
            finally:
                self._release_priority("log")
    else:
        self.log("INFO", "日志巡检未命中新账号：本轮没有需要进一步检查的CPA使用记录")

    if latest_by_email:
        self._last_seen_usage_by_email.update(latest_by_email)
        self.last_usage_query_time = max(latest_by_email.values())
    else:
        self.last_usage_query_time = query_started_at
    return "processed"
```

- [ ] **Step 5: Run targeted maintainer tests for priming, disabled mode, empty success, and failure-preserves-cursor**

Run:

```bash
pytest tests/test_maintainer.py::MaintainerTests::test_run_fill_once_primes_cursor_without_requesting_usage_logs tests/test_maintainer.py::MaintainerTests::test_run_fill_once_skips_when_fill_interval_is_zero tests/test_maintainer.py::MaintainerTests::test_run_fill_once_advances_cursor_on_successful_empty_usage_logs tests/test_maintainer.py::MaintainerTests::test_run_fill_once_does_not_advance_cursor_on_usage_log_failure -v
```

Expected: PASS.

- [ ] **Step 6: Add a focused test for inclusive cursoring plus same-second local de-duplication**

Add a test like:

```python
def test_new_usage_timestamp_by_email_keeps_same_second_new_emails_without_reprocessing_seen_email(self):
    self.maintainer._last_seen_usage_by_email = {"seen@example.com": 1000}
    usage_data = {
        "usage": {
            "apis": {
                "api-1": {
                    "models": {
                        "gpt-5.3-codex": {
                            "details": [
                                {"source": "seen@example.com", "timestamp": 1000},
                                {"source": "new@example.com", "timestamp": 1000},
                            ]
                        }
                    }
                }
            }
        }
    }

    latest = self.maintainer._new_usage_timestamp_by_email(usage_data, cursor_time=1000)

    self.assertEqual(latest, {"new@example.com": 1000})
```

If the helper expects ISO strings instead of ints, use the repository’s actual timestamp shape when writing the real test.

- [ ] **Step 7: Run the new same-second de-dup test and verify it passes**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_new_usage_timestamp_by_email_keeps_same_second_new_emails_without_reprocessing_seen_email -v`

Expected: PASS.

- [ ] **Step 8: Update startup logging to remove stale `CPA_USAGE_QUERY_INTERVAL` wording**

In `log_startup()`, replace the old display block:

```python
usage_query_interval_display = (
    "已禁用（CPA_USAGE_QUERY_INTERVAL=0）"
    if self.settings.usage_query_interval_seconds == 0
    else f"{self.settings.usage_query_interval_seconds} 秒"
)
```

with a `CPA_FILL_INTERVAL`-based message such as:

```python
log_inspection_display = (
    "已禁用（CPA_FILL_INTERVAL<=0）"
    if self.settings.fill_interval_seconds <= 0
    else f"每 {self.settings.fill_interval_seconds} 秒轮询，按内存游标推进日志窗口"
)
```

Then update the emitted line label accordingly.

- [ ] **Step 9: Commit the maintainer cursor implementation**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: use cursor-based log inspection windows"
```

### Task 4: Keep the CPA client contract stable and update focused request-shape tests

**Files:**
- Modify: `tests/test_cpa_client.py:34-52`
- Optional Modify: `src/cpa_client.py:79-83` only if signature or return handling changes

- [ ] **Step 1: Keep the request-shape test explicit about `lookback_seconds`**

The production API should still be:

```python
result = client.get_usage_log(lookback_seconds=7200)
```

and should still assert:

```python
self.assertEqual(kwargs["params"], {"lookback_seconds": 7200})
```

Do not broaden this test unless production behavior actually changes.

- [ ] **Step 2: Run the CPA client request-shape test**

Run: `pytest tests/test_cpa_client.py::CPAClientTests::test_get_usage_log_uses_management_usage_endpoint_and_auth_headers -v`

Expected: PASS.

- [ ] **Step 3: Commit only if a test or production adjustment was necessary**

```bash
git add tests/test_cpa_client.py src/cpa_client.py
git commit -m "test: preserve usage log request contract"
```

Skip this commit if no file changed.

### Task 5: Update config template and README docs to match the new behavior

**Files:**
- Modify: `.env.example`
- Modify: `README.md:143-165, 199-203`
- Modify: `README.en.md:143-164, 198-202`
- Optional Modify: `tests/test_project_files.py`

- [ ] **Step 1: Remove `CPA_USAGE_QUERY_INTERVAL` from `.env.example` and rewrite `CPA_FILL_INTERVAL` comments**

Update the config template so it reflects the new role of `CPA_FILL_INTERVAL`.

Use content in this shape:

```dotenv
# Log inspection loop interval in seconds / 日志巡检轮询间隔（秒）
# <= 0 disables log inspection; > 0 enables it and controls how often the loop runs
# 小于等于 0 表示禁用日志巡检；大于 0 表示启用，并控制日志巡检循环频率
CPA_FILL_INTERVAL=10
```

Delete the entire `CPA_USAGE_QUERY_INTERVAL` block.

- [ ] **Step 2: Update README.md configuration and behavior sections**

Replace the old configuration bullet:

```markdown
- `CPA_USAGE_QUERY_INTERVAL`：日志巡检间隔秒数，同时作为查询 `/v0/management/usage` 时的回溯窗口，默认 `7200`，设为 `0` 表示禁用日志巡检
```

with wording that describes:

```markdown
- `CPA_FILL_INTERVAL`：日志巡检轮询间隔秒数，默认 `10`；设为 `0` 或负数表示禁用日志巡检
```

Also update the runtime behavior text around the log-driven path to explain:

- 首轮只记录起始时间
- 第二轮开始查询起始时间到当前时间的日志
- 后续按内存中的最后日志时间推进
- 进程重启后重新从首轮记录时间开始

- [ ] **Step 3: Update README.en.md with the same semantics**

Replace the old `CPA_USAGE_QUERY_INTERVAL` bullet and matching behavior text with English wording mirroring the Chinese README.

- [ ] **Step 4: Add or update a project-file test if useful for guarding `.env.example`**

If you add coverage, use something small and explicit:

```python
def test_env_example_no_longer_mentions_cpa_usage_query_interval(self):
    content = (ROOT / ".env.example").read_text(encoding="utf-8")

    self.assertNotIn("CPA_USAGE_QUERY_INTERVAL", content)
    self.assertIn("CPA_FILL_INTERVAL", content)
```

- [ ] **Step 5: Run documentation/config regression tests**

Run: `pytest tests/test_project_files.py -q`

Expected: PASS.

- [ ] **Step 6: Commit docs/config updates**

```bash
git add .env.example README.md README.en.md tests/test_project_files.py
git commit -m "docs: update log inspection configuration"
```

### Task 6: Run targeted and broad verification, then sync branches correctly

**Files:**
- No new production files; this task verifies and closes the work.

- [ ] **Step 1: Run the targeted suite for this feature**

Run:

```bash
pytest tests/test_settings.py tests/test_maintainer.py tests/test_cpa_client.py tests/test_project_files.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the broader repository regression suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 3: Review the branch diff split before syncing `main`**

On `new`, confirm that changes intended only for the AI-trace branch stay there:

```bash
git diff --name-status origin/main..new
```

Before syncing `main`, keep `CLAUDE.md` and `docs/superpowers/**` off `main` unless the user explicitly changes that branch policy.

- [ ] **Step 4: Commit any final fixups from verification**

```bash
git add <specific-files>
git commit -m "fix: address verification feedback"
```

Only do this if verification requires additional edits.

- [ ] **Step 5: Sync the clean `main` branch with business/doc changes only**

Follow the repository branch rule:

- `new` may keep AI traces and planning/spec files
- `main` must not contain AI traces
- sync runtime code, tests, `.env.example`, and README content across both branches
- do not force `CLAUDE.md` or `docs/superpowers/**` into `main`

- [ ] **Step 6: Push both branches after verification**

```bash
git push origin main
git push origin new
```

## Self-review checklist

- The plan covers every confirmed requirement from `docs/superpowers/specs/2026-05-21-log-inspection-cursor-window-design.md`.
- No step relies on `CPA_USAGE_QUERY_INTERVAL` remaining in runtime settings.
- The plan keeps CPA API usage compatible by preserving `lookback_seconds`.
- The plan explicitly covers `.env.example` removal of `CPA_USAGE_QUERY_INTERVAL`.
- The plan treats `main` and `new` according to repository policy rather than forcing file equality.

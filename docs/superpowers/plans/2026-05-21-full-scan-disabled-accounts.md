# Full Scan Disabled-Account Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make main full scans inspect every codex account regardless of current disabled/tracked state, reconcile enable/disable decisions from live quota, backfill `disabled_accounts.json` for disabled over-quota accounts, and pace full scans sequentially between accounts.

**Architecture:** Keep `CPACodexKeeper.process_token()` as the shared inspection entry point, but remove the full-scan-only early skip for tracked disabled accounts and adjust `_apply_quota_policy()` so tracked-state presence no longer gates enable or recheck enrollment. Convert the main full-scan scheduler in `run()` / `_process_tokens_with_priority()` from thread-pool concurrency to sequential processing with bounded inter-token sleep, while leaving log inspection and tracked timer rechecks unchanged.

**Tech Stack:** Python 3.11, unittest, unittest.mock, existing `src/maintainer.py` / `src/settings.py` architecture

---

## File map

- `src/maintainer.py`
  - Main behavior changes live here.
  - Key functions: `process_token()`, `_apply_quota_policy()`, `_process_tokens_with_priority()`, `run()`, helper methods for tracked-state scheduling and new full-scan pacing.
- `src/settings.py`
  - Add explicit full-scan pacing settings if we choose config-backed values instead of hardcoded constants.
- `tests/test_maintainer.py`
  - Update/remove tests that assert old skip/concurrency behavior.
  - Add focused tests for full-scan disabled-account reconciliation and pacing boundaries.
- `tests/test_settings.py`
  - Only needed if new env-backed pacing settings are added.
- `README.md`
  - Update Chinese docs that currently describe intra-round concurrency and tracked-state gating semantics.
- `README.en.md`
  - Update English docs to match the new full-scan behavior.

## Implementation order

1. Lock in desired behavior with failing maintainer tests.
2. Implement quota-state reconciliation changes for disabled accounts.
3. Replace threaded full-scan execution with sequential paced execution.
4. Add settings support if needed for the 10–60s / ~30s pacing defaults.
5. Update README docs that currently describe concurrency.
6. Run targeted tests, then broader regression tests.

## Regression risks to watch

- Accidentally changing `process_fill_token()` semantics; it must still skip disabled accounts and only disable.
- Accidentally applying main full-scan pacing to `_run_tracked_recheck()` or `run_fill_once()`.
- Breaking tracked-state cleanup after successful enable verification.
- Leaving logs or summary text saying “并发” / “worker threads” after execution becomes sequential.
- Keeping tests that depend on `ThreadPoolExecutor` / `as_completed` after removing that design.

### Task 1: Replace the old disabled-account skip contract with explicit full-scan expectations

**Files:**
- Modify: `tests/test_maintainer.py:605-626`
- Modify: `tests/test_maintainer.py:681-851`
- Modify: `tests/test_maintainer.py:1786-1909`

- [ ] **Step 1: Rewrite the old skip test into a failing full-scan inspection test**

Replace the old `test_process_token_skips_usage_before_tracked_next_check` with a test that proves full scan still checks usage even when the account is disabled and `next_check_at` is in the future.

```python
def test_process_token_checks_usage_even_before_tracked_next_check(self):
    self.maintainer.get_token_detail = Mock(return_value={
        "email": "a@example.com",
        "disabled": True,
        "access_token": "token",
        "refresh_token": "rt",
        "account_id": "acc",
        "expired": "2099-01-01T00:00:00Z",
    })
    self.maintainer._tracked_disabled_accounts = {"t-skip": {"next_check_at": 2000}}
    self.maintainer.check_token_live = Mock(return_value=(200, {
        "json": {
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {"used_percent": 0, "limit_window_seconds": 604800},
                "secondary_window": None,
            },
            "credits": {"has_credits": False},
        }
    }))

    with patch("src.maintainer.time.time", return_value=1000):
        result = self.maintainer.process_token({"name": "t-skip"}, 1, 1)

    self.assertEqual(result, "alive")
    self.maintainer.check_token_live.assert_called_once_with("token", "acc")
```

- [ ] **Step 2: Run the rewritten single test to verify it fails under current code**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_process_token_checks_usage_even_before_tracked_next_check -v`

Expected: FAIL because `check_token_live` is not called under the current early-return logic.

- [ ] **Step 3: Add a failing test for enabling a disabled but untracked healthy account**

Insert a new test near the existing enable tests.

```python
def test_process_token_enables_untracked_disabled_account_when_quota_recovers(self):
    self.maintainer.get_token_detail = Mock(side_effect=[
        {
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        },
        {"name": "t-enable-untracked", "disabled": False},
    ])
    self.maintainer.check_token_live = Mock(return_value=(200, {
        "json": {
            "plan_type": "team",
            "rate_limit": {
                "primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1776634820},
                "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800, "reset_at": 1777000096},
            },
            "credits": {"has_credits": False},
        }
    }))
    self.maintainer.set_disabled_status = Mock(return_value=True)

    with patch("src.maintainer.time.time", return_value=1000), patch("src.maintainer.time.sleep"):
        result = self.maintainer.process_token({"name": "t-enable-untracked"}, 1, 1)

    self.assertEqual(result, "alive")
    self.maintainer.set_disabled_status.assert_called_once_with("t-enable-untracked", disabled=False, logger=ANY)
    self.assertEqual(self.maintainer.stats.enabled, 1)
    self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-enable-untracked"))
```

- [ ] **Step 4: Run the new enable test and verify it fails**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_process_token_enables_untracked_disabled_account_when_quota_recovers -v`

Expected: FAIL because current `_apply_quota_policy()` keeps untracked disabled accounts disabled even when quota is healthy.

- [ ] **Step 5: Add a failing test for backfilling tracked state for disabled over-quota accounts**

Add a new test near `test_process_token_schedules_next_check_when_auto_disabling`.

```python
def test_process_token_backfills_tracked_state_for_disabled_over_quota_account(self):
    self.maintainer.get_token_detail = Mock(return_value={
        "email": "a@example.com",
        "disabled": True,
        "access_token": "token",
        "refresh_token": "rt",
        "account_id": "acc",
        "expired": "2099-01-01T00:00:00Z",
    })
    self.maintainer.check_token_live = Mock(return_value=(200, {
        "json": {
            "plan_type": "team",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "limit_window_seconds": 18000, "reset_at": 1776634820},
                "secondary_window": {"used_percent": 100, "limit_window_seconds": 604800, "reset_at": 1777000096},
            },
            "credits": {"has_credits": False},
        }
    }))

    with patch("src.maintainer.time.time", return_value=1000):
        result = self.maintainer.process_token({"name": "t-disabled-backfill"}, 1, 1)

    self.assertEqual(result, "alive")
    self.assertEqual(self.maintainer._get_tracked_next_check_at("t-disabled-backfill"), 1777000096)
```

- [ ] **Step 6: Run the backfill test and verify it fails**

Run: `pytest tests/test_maintainer.py::MaintainerTests::test_process_token_backfills_tracked_state_for_disabled_over_quota_account -v`

Expected: FAIL because current code leaves disabled untracked over-quota accounts disabled without adding tracked state.

- [ ] **Step 7: Commit the red tests**

```bash
git add tests/test_maintainer.py
git commit -m "test: capture full-scan disabled account behavior"
```

### Task 2: Implement full-scan disabled-account reconciliation in `process_token()` and `_apply_quota_policy()`

**Files:**
- Modify: `src/maintainer.py:872-993`
- Modify: `src/maintainer.py:1085-1165`
- Test: `tests/test_maintainer.py:605-851`

- [ ] **Step 1: Remove the early skip branch from `process_token()`**

Delete this branch so full scan always reaches `check_token_live(...)`:

```python
if disabled and tracked_next_check_at is not None and now < tracked_next_check_at:
    logger.log(
        "INFO",
        f"当前账号已禁用，计划于 {self._format_tracked_next_check_at(tracked_next_check_at)} 后复查额度，当前轮次跳过",
        indent=1,
    )
    self._inc_stat("alive")
    logger.blank_line()
    return "alive"
```

Then remove the now-unused local if it is no longer needed:

```python
tracked_next_check_at = self._get_tracked_next_check_at(name)
```

Keep the rest of `process_token()` unchanged.

- [ ] **Step 2: Implement “disabled + healthy quota => enable even if untracked”**

Update the `if disabled:` branch inside `_apply_quota_policy()`.

Replace:

```python
if below_threshold:
    if tracked_next_check_at is None:
        logger.log("INFO", "已禁用且未被 keeper 纳入自动复查，保持禁用", indent=1)
        return None, effective_disabled
```

with logic that always attempts enable on healthy quota:

```python
if below_threshold:
    if secondary_present:
        logger.log(
            "WARN",
            f"已禁用且 {primary_label}/{secondary_label} 额度均已低于 {self.settings.quota_threshold}%，准备启用",
            indent=1,
        )
    else:
        logger.log(
            "WARN",
            f"已禁用但{primary_label}额度已降至 {primary_pct}% < {self.settings.quota_threshold}%，准备启用",
            indent=1,
        )
    if self._enable_with_verification(name, logger):
        if tracked_next_check_at is not None:
            if self._remove_tracked_account(name):
                logger.log("ENABLE", "账号已重新启用", indent=1)
            else:
                logger.log("ERROR", "账号已确认启用，但移除复查计划失败", indent=1)
        else:
            logger.log("ENABLE", "账号已重新启用", indent=1)
        self._inc_stat("enabled")
        effective_disabled = False
    else:
        logger.log("ERROR", "账号启用未确认成功，保留待下次处理状态", indent=1)
    return None, effective_disabled
```

- [ ] **Step 3: Implement “disabled + over quota => ensure tracked state exists”**

Replace the `tracked_next_check_at is not None and body_info is not None and now is not None` guard with logic that schedules tracked state whenever quota is still over threshold and `body_info`/`now` are available.

Use this structure:

```python
if body_info is not None and now is not None:
    next_check_at = self._compute_next_check_at_from_usage(
        body_info,
        now,
        self.settings.interval_seconds,
        token_detail=token_detail,
    )
    if self._set_tracked_next_check_at(name, next_check_at):
        logger.log(
            "INFO",
            f"已禁用，{reached_summary} >= {self.settings.quota_threshold}%，保持禁用并重排到 {self._format_tracked_next_check_at(next_check_at)}",
            indent=1,
        )
    else:
        logger.log("ERROR", "已禁用，但重排复查计划失败", indent=1)
    return None, effective_disabled
```

This intentionally backfills untracked disabled over-quota accounts.

- [ ] **Step 4: Run the three focused quota tests**

Run:

```bash
pytest tests/test_maintainer.py::MaintainerTests::test_process_token_checks_usage_even_before_tracked_next_check -v
pytest tests/test_maintainer.py::MaintainerTests::test_process_token_enables_untracked_disabled_account_when_quota_recovers -v
pytest tests/test_maintainer.py::MaintainerTests::test_process_token_backfills_tracked_state_for_disabled_over_quota_account -v
```

Expected: PASS.

- [ ] **Step 5: Run the nearby enable/disable regression tests**

Run:

```bash
pytest tests/test_maintainer.py::MaintainerTests::test_process_token_schedules_next_check_when_auto_disabling -v
pytest tests/test_maintainer.py::MaintainerTests::test_process_token_enables_tracked_disabled_token_when_due_and_below_threshold -v
pytest tests/test_maintainer.py::MaintainerTests::test_process_fill_token_schedules_next_check_when_disabling -v
```

Expected: PASS, proving tracked rechecks and log-driven disable behavior still work.

- [ ] **Step 6: Commit the quota-policy implementation**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "fix: reconcile disabled accounts during full scans"
```

### Task 3: Convert main full scan from threaded concurrency to sequential paced execution

**Files:**
- Modify: `src/maintainer.py:1-7`
- Modify: `src/maintainer.py:1194-1267`
- Modify: `tests/test_maintainer.py:1786-1909`

- [ ] **Step 1: Remove unused concurrency imports from `src/maintainer.py`**

Delete this import because full scan will no longer use thread-pool fan-out:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

Keep `threading` because timers and coordinator state still need it.

- [ ] **Step 2: Add a helper that sleeps between full-scan tokens**

Insert a helper method near the other internal helpers:

```python
def _sleep_between_full_scan_tokens(self):
    delay_seconds = random.randint(
        self.settings.full_scan_min_interval_seconds,
        self.settings.full_scan_max_interval_seconds,
    )
    self.log("INFO", f"主巡检节流等待：{delay_seconds} 秒后继续下一个账号")
    time.sleep(delay_seconds)
```

This helper is intentionally only for main full scan.

- [ ] **Step 3: Replace `_process_tokens_with_priority()` with sequential logic**

Replace the current thread-pool implementation with a simple loop:

```python
def _process_tokens_with_priority(self, tokens, *, force_refresh_on_expiry=None):
    total = len(tokens)
    for idx, token_info in enumerate(tokens, 1):
        self._acquire_priority("full")
        try:
            try:
                if force_refresh_on_expiry is None:
                    self.process_token(token_info, idx, total)
                else:
                    self.process_token(
                        token_info,
                        idx,
                        total,
                        force_refresh_on_expiry=force_refresh_on_expiry,
                    )
            except Exception as exc:
                token_name = token_info.get("name", "unknown")
                self.log("ERROR", f"Token 任务异常 ({token_name}): {exc}", indent=1)
                self.blank_line()
        finally:
            self._release_priority("full")

        if idx < total:
            self._sleep_between_full_scan_tokens()
```

- [ ] **Step 4: Update `run()` log text to describe sequential pacing instead of concurrency**

Change:

```python
self.log("INFO", f"主巡检并发设置：{self.settings.worker_threads} 个工作线程")
```

to:

```python
self.log(
    "INFO",
    f"主巡检执行策略：逐账号串行扫描，账号间等待 {self.settings.full_scan_min_interval_seconds}-{self.settings.full_scan_max_interval_seconds} 秒",
)
```

And in the summary section, replace:

```python
self.logger.format_log_record("INFO", f"工作线程: {self.settings.worker_threads}", indent=1),
```

with:

```python
self.logger.format_log_record(
    "INFO",
    f"扫描节流: {self.settings.full_scan_min_interval_seconds}-{self.settings.full_scan_max_interval_seconds} 秒",
    indent=1,
),
```

- [ ] **Step 5: Replace the old threaded run tests with sequential pacing tests**

Delete the tests that patch `ThreadPoolExecutor` / `as_completed` and replace them with sequential assertions.

Use this test for order + per-token execution:

```python
@patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
def test_run_processes_tokens_sequentially(self, _shuffle_mock):
    tokens = [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}]
    self.maintainer.get_token_list = Mock(return_value=tokens)
    self.maintainer.log_startup = Mock()
    self.maintainer._sleep_between_full_scan_tokens = Mock()
    call_order = []

    def process_side_effect(token_info, idx, total, **kwargs):
        call_order.append((token_info["name"], idx, total))
        return "alive"

    self.maintainer.process_token = Mock(side_effect=process_side_effect)

    self.maintainer.run()

    self.assertEqual(call_order, [("t1", 1, 3), ("t2", 2, 3), ("t3", 3, 3)])
    self.assertEqual(self.maintainer._sleep_between_full_scan_tokens.call_count, 2)
```

Use this test for “no sleep after last token”:

```python
@patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
def test_run_does_not_sleep_after_last_token(self, _shuffle_mock):
    tokens = [{"name": "t1"}]
    self.maintainer.get_token_list = Mock(return_value=tokens)
    self.maintainer.log_startup = Mock()
    self.maintainer._sleep_between_full_scan_tokens = Mock()
    self.maintainer.process_token = Mock(return_value="alive")

    self.maintainer.run()

    self.maintainer._sleep_between_full_scan_tokens.assert_not_called()
```

Use this test for task exception continuity under sequential execution:

```python
@patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
def test_run_logs_task_exception_and_continues(self, _shuffle_mock):
    tokens = [{"name": "ok-1"}, {"name": "boom"}, {"name": "ok-2"}]
    self.maintainer.get_token_list = Mock(return_value=tokens)
    self.maintainer.log_startup = Mock()
    self.maintainer.log = Mock()
    self.maintainer._sleep_between_full_scan_tokens = Mock()

    def process_side_effect(token_info, idx, total):
        if token_info["name"] == "boom":
            raise RuntimeError("unexpected boom")
        self.maintainer.stats.alive += 1
        return "alive"

    self.maintainer.process_token = Mock(side_effect=process_side_effect)

    self.maintainer.run()

    self.assertEqual(self.maintainer.process_token.call_count, 3)
    self.assertEqual(self.maintainer.stats.alive, 2)
    self.maintainer.log.assert_any_call("ERROR", "Token 任务异常 (boom): unexpected boom", indent=1)
    self.assertEqual(self.maintainer._sleep_between_full_scan_tokens.call_count, 2)
```

- [ ] **Step 6: Add a direct helper test proving pacing uses the configured bounds**

Add a new test:

```python
def test_sleep_between_full_scan_tokens_uses_configured_bounds(self):
    self.maintainer.settings.full_scan_min_interval_seconds = 10
    self.maintainer.settings.full_scan_max_interval_seconds = 60
    self.maintainer.log = Mock()

    with patch("src.maintainer.random.randint", return_value=30) as randint_mock, patch("src.maintainer.time.sleep") as sleep_mock:
        self.maintainer._sleep_between_full_scan_tokens()

    randint_mock.assert_called_once_with(10, 60)
    sleep_mock.assert_called_once_with(30)
    self.maintainer.log.assert_any_call("INFO", "主巡检节流等待：30 秒后继续下一个账号")
```

- [ ] **Step 7: Run the sequential full-scan tests**

Run:

```bash
pytest tests/test_maintainer.py::MaintainerTests::test_run_processes_tokens_sequentially -v
pytest tests/test_maintainer.py::MaintainerTests::test_run_does_not_sleep_after_last_token -v
pytest tests/test_maintainer.py::MaintainerTests::test_run_logs_task_exception_and_continues -v
pytest tests/test_maintainer.py::MaintainerTests::test_sleep_between_full_scan_tokens_uses_configured_bounds -v
```

Expected: PASS.

- [ ] **Step 8: Commit the sequential scheduler change**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "refactor: pace full scans sequentially"
```

### Task 4: Add explicit settings for full-scan pacing defaults and validation

**Files:**
- Modify: `src/settings.py:6-24`
- Modify: `src/settings.py:31-54`
- Modify: `src/settings.py:137-196`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Add default constants for full-scan pacing**

Insert new defaults near the other settings constants:

```python
DEFAULT_FULL_SCAN_MIN_INTERVAL_SECONDS = 10
DEFAULT_FULL_SCAN_MAX_INTERVAL_SECONDS = 60
```

- [ ] **Step 2: Extend the `Settings` dataclass**

Add two fields after `worker_threads`:

```python
full_scan_min_interval_seconds: int = DEFAULT_FULL_SCAN_MIN_INTERVAL_SECONDS
full_scan_max_interval_seconds: int = DEFAULT_FULL_SCAN_MAX_INTERVAL_SECONDS
```

- [ ] **Step 3: Parse and validate the new env values in `load_settings()`**

Read the values before constructing `Settings` so cross-field validation can happen once:

```python
full_scan_min_interval_seconds = _read_int(
    "CPA_FULL_SCAN_MIN_INTERVAL_SECONDS",
    DEFAULT_FULL_SCAN_MIN_INTERVAL_SECONDS,
    env_values,
    minimum=10,
    maximum=60,
)
full_scan_max_interval_seconds = _read_int(
    "CPA_FULL_SCAN_MAX_INTERVAL_SECONDS",
    DEFAULT_FULL_SCAN_MAX_INTERVAL_SECONDS,
    env_values,
    minimum=10,
    maximum=60,
)
if full_scan_min_interval_seconds > full_scan_max_interval_seconds:
    raise SettingsError("CPA_FULL_SCAN_MIN_INTERVAL_SECONDS must be <= CPA_FULL_SCAN_MAX_INTERVAL_SECONDS")
```

Then pass them into the dataclass:

```python
full_scan_min_interval_seconds=full_scan_min_interval_seconds,
full_scan_max_interval_seconds=full_scan_max_interval_seconds,
```

- [ ] **Step 4: Add settings tests for defaults and validation**

Add these tests to `tests/test_settings.py`:

```python
def test_load_settings_uses_default_full_scan_interval_bounds(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
        },
        clear=True,
    ):
        settings = load_settings()

    self.assertEqual(settings.full_scan_min_interval_seconds, 10)
    self.assertEqual(settings.full_scan_max_interval_seconds, 60)


def test_load_settings_reads_full_scan_interval_bounds(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
            "CPA_FULL_SCAN_MIN_INTERVAL_SECONDS": "15",
            "CPA_FULL_SCAN_MAX_INTERVAL_SECONDS": "45",
        },
        clear=True,
    ):
        settings = load_settings()

    self.assertEqual(settings.full_scan_min_interval_seconds, 15)
    self.assertEqual(settings.full_scan_max_interval_seconds, 45)


def test_load_settings_rejects_inverted_full_scan_interval_bounds(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
            "CPA_FULL_SCAN_MIN_INTERVAL_SECONDS": "50",
            "CPA_FULL_SCAN_MAX_INTERVAL_SECONDS": "20",
        },
        clear=True,
    ):
        with self.assertRaises(SettingsError):
            load_settings()
```

- [ ] **Step 5: Run the settings tests**

Run:

```bash
pytest tests/test_settings.py::SettingsTests::test_load_settings_uses_default_full_scan_interval_bounds -v
pytest tests/test_settings.py::SettingsTests::test_load_settings_reads_full_scan_interval_bounds -v
pytest tests/test_settings.py::SettingsTests::test_load_settings_rejects_inverted_full_scan_interval_bounds -v
```

Expected: PASS.

- [ ] **Step 6: Commit the settings support**

```bash
git add src/settings.py tests/test_settings.py
git commit -m "feat: add full scan pacing settings"
```

### Task 5: Update documentation to match sequential full scans and disabled-account reconciliation

**Files:**
- Modify: `README.md:73-78`
- Modify: `README.md:150-152`
- Modify: `README.md:199-201`
- Modify: `README.md:254`
- Modify: `README.en.md:73-78`
- Modify: `README.en.md:141-145`
- Modify: `README.en.md:199-200`
- Modify: `README.en.md:253`

- [ ] **Step 1: Update the Chinese README behavior summary**

Revise the “按轮次运行” paragraph and the daemon-mode bullets so they no longer say intra-round concurrency and so they explain disabled-account handling during full scan.

Use text along these lines:

```markdown
这是一个**按轮次运行、逐账号串行**的流程：一轮结束后才会进入下一轮；同一轮中的 codex 账号会逐个巡检，并在账号之间按配置等待一段时间，以避免过于繁忙。

在 daemon 模式下，主巡检之外还会并行存在两条附加路径：

- 当 `CPA_USAGE_QUERY_INTERVAL > 0` 时，会额外启动基于 CPA 的日志巡检；它会对比上次查询时间，只挑出这段时间内新增调用过、且仍存在于过滤后 auth-files 中的 codex 账号，再对这些账号执行额度检查。日志巡检只负责禁用，不负责启用、刷新或删除。
- 对于 keeper 自动禁用并写入 `disabled_accounts.json` 的账号，会记录 `next_check_at` 并建立独立定时器；到点后立即复查额度，若恢复则启用，若未恢复则重排下一次复查时间。`disabled_accounts.json` 仅用于记录后续自动复查计划，不会阻止主巡检继续扫描已禁用账号。
```

- [ ] **Step 2: Update the Chinese config section for pacing**

Replace the worker-thread item with the new pacing settings:

```markdown
- `CPA_FULL_SCAN_MIN_INTERVAL_SECONDS`：主巡检两个账号之间的最小等待秒数，默认 `10`
- `CPA_FULL_SCAN_MAX_INTERVAL_SECONDS`：主巡检两个账号之间的最大等待秒数，默认 `60`
```

If `CPA_WORKER_THREADS` remains in code for compatibility but is no longer used by full scan, document that clearly instead of pretending it still controls concurrency.

- [ ] **Step 3: Update the Chinese run-mode summary near the daemon section**

Change the bullets that currently describe “并发巡检多个 token” so they describe:

```markdown
- 原有的全量巡检（逐账号串行，账号间按配置节流）
- `disabled_accounts.json` 中已记录账号的定时复查
- 当 `CPA_USAGE_QUERY_INTERVAL > 0` 时，再额外启动日志巡检线程
```

- [ ] **Step 4: Update the English README in parallel**

Mirror the same meaning in `README.en.md`:

```markdown
This process is **round-based and sequential per account**. One full round still completes before the next round starts. Within a round, codex accounts are inspected one by one, with a configured delay between accounts to avoid excessive pressure.
```

And update the daemon-mode bullet to say `disabled_accounts.json` records planned rechecks but does not stop full scans from inspecting disabled accounts.

- [ ] **Step 5: Run the documentation smoke test if one exists, otherwise run a targeted grep check**

Run:

```bash
pytest tests/test_project_files.py -v
```

If no doc assertions exist for this content, also run:

```bash
python -m pytest tests/test_project_files.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit the documentation update**

```bash
git add README.md README.en.md
git commit -m "docs: describe sequential full-scan behavior"
```

### Task 6: Run the final regression slice and verify unchanged side paths

**Files:**
- Test: `tests/test_maintainer.py`
- Test: `tests/test_settings.py`
- Test: `tests/test_project_files.py`

- [ ] **Step 1: Run a targeted maintainer regression slice covering all three paths**

Run:

```bash
pytest tests/test_maintainer.py::MaintainerTests::test_process_fill_token_schedules_next_check_when_disabling -v
pytest tests/test_maintainer.py::MaintainerTests::test_run_tracked_recheck_requests_and_releases_timer_priority -v
pytest tests/test_maintainer.py::MaintainerTests::test_run_forever_scans_due_tracked_rechecks_each_round -v
pytest tests/test_maintainer.py::MaintainerTests::test_run_fill_forever_scans_due_tracked_rechecks_each_round -v
```

Expected: PASS. This confirms log-driven inspection and tracked rechecks did not inherit full-scan pacing or lose their priority flow.

- [ ] **Step 2: Run the full maintainer and settings suites**

Run:

```bash
pytest tests/test_maintainer.py tests/test_settings.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the lightweight project-file suite**

Run: `pytest tests/test_project_files.py -v`

Expected: PASS.

- [ ] **Step 4: Review git diff before handoff**

Run:

```bash
git diff -- src/maintainer.py src/settings.py tests/test_maintainer.py tests/test_settings.py README.md README.en.md
```

Expected: Diff shows only the planned scheduler, quota-policy, settings, tests, and doc updates.

- [ ] **Step 5: Commit the final verification pass if any last fixes were needed**

```bash
git add src/maintainer.py src/settings.py tests/test_maintainer.py tests/test_settings.py README.md README.en.md
git commit -m "test: verify full-scan pacing and tracked-state behavior"
```

Only make this commit if verification required additional fixes after the earlier task commits.

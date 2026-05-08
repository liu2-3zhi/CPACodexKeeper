# Disabled account state lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent keeper instances from overwriting `disabled_accounts.json` and make scheduled rechecks reliable enough that due disabled accounts are still re-enabled when quota allows.

**Architecture:** Keep `disabled_accounts.json` as the source of persisted tracked-disable state, but wrap every read-modify-write mutation in a cross-instance file lock using `filelock`. Extend `Settings` with lock timeout and retry interval, centralize locked state mutation in `src/maintainer.py`, and verify the new behavior with focused unit tests around settings parsing, state mutation, retry/timeout, startup recovery, and due re-enable cleanup.

**Tech Stack:** Python 3.11, `unittest`, `pytest`, JSON file persistence, `filelock`

---

### Task 1: Add the file-lock dependency and lock settings

**Files:**
- Modify: `requirements.txt`
- Modify: `src/settings.py`
- Modify: `.env.example`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Write the failing settings tests**

```python
    def test_load_settings_uses_default_disabled_state_lock_values(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.disabled_state_lock_timeout_seconds, 10.0)
        self.assertEqual(settings.disabled_state_lock_retry_interval_seconds, 0.2)

    def test_load_settings_reads_disabled_state_lock_values(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS": "3.5",
                "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS": "0.05",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.disabled_state_lock_timeout_seconds, 3.5)
        self.assertEqual(settings.disabled_state_lock_retry_interval_seconds, 0.05)

    def test_load_settings_rejects_non_positive_disabled_state_lock_timeout(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()

    def test_load_settings_rejects_non_positive_disabled_state_lock_retry_interval(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()
```

- [ ] **Step 2: Run the settings test target and verify it fails**

Run: `python -m pytest tests/test_settings.py -k disabled_state_lock -v`
Expected: FAIL because `Settings` has no disabled-state lock fields and `load_settings()` does not parse the new env vars.

- [ ] **Step 3: Add the dependency to `requirements.txt`**

```txt
curl-cffi>=0.7.0
filelock>=3.16.1
```

- [ ] **Step 4: Add float parsing and new settings fields in `src/settings.py`**

```python
DEFAULT_DISABLED_STATE_LOCK_TIMEOUT_SECONDS = 10.0
DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS = 0.2
```

```python
@dataclass(slots=True)
class Settings:
    cpa_endpoint: str
    cpa_token: str
    proxy: str | None = None
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    fill_interval_seconds: int = DEFAULT_FILL_INTERVAL_SECONDS
    quota_threshold: int = DEFAULT_QUOTA_THRESHOLD
    quota_reset_none_recheck_seconds: int = DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS
    expiry_threshold_days: int = DEFAULT_EXPIRY_THRESHOLD_DAYS
    usage_timeout_seconds: int = DEFAULT_USAGE_TIMEOUT_SECONDS
    usage_query_interval_seconds: int = DEFAULT_USAGE_QUERY_INTERVAL_SECONDS
    cpa_timeout_seconds: int = DEFAULT_CPA_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    worker_threads: int = DEFAULT_WORKER_THREADS
    enable_refresh: bool = DEFAULT_ENABLE_REFRESH
    allow_delete: bool = DEFAULT_ALLOW_DELETE
    force_refresh_on_expiry: bool = DEFAULT_FORCE_REFRESH_ON_EXPIRY
    log_archive_max_size_mb: int = DEFAULT_LOG_ARCHIVE_MAX_SIZE_MB
    disabled_state_lock_timeout_seconds: float = DEFAULT_DISABLED_STATE_LOCK_TIMEOUT_SECONDS
    disabled_state_lock_retry_interval_seconds: float = DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS
```

```python
def _read_float(name: str, default: float, env_values: dict[str, str], *, minimum: float = 0.0) -> float:
    raw = _get_config_value(name, env_values)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if value <= minimum:
        raise SettingsError(f"{name} must be > {minimum}")
    return value
```

- [ ] **Step 5: Read the two new env vars in `load_settings()`**

```python
        disabled_state_lock_timeout_seconds=_read_float(
            "CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS",
            DEFAULT_DISABLED_STATE_LOCK_TIMEOUT_SECONDS,
            env_values,
        ),
        disabled_state_lock_retry_interval_seconds=_read_float(
            "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS",
            DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS,
            env_values,
        ),
```

- [ ] **Step 6: Document the new env vars in `.env.example`**

```env
# Timeout when waiting for disabled_accounts.json lock / 等待 disabled_accounts.json 锁的超时时间（秒）
CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS=10

# Retry interval when waiting for disabled_accounts.json lock / 等待 disabled_accounts.json 锁时的重试间隔（秒）
CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS=0.2
```

- [ ] **Step 7: Run the focused settings tests and verify they pass**

Run: `python -m pytest tests/test_settings.py -k disabled_state_lock -v`
Expected: PASS

- [ ] **Step 8: Commit the settings and dependency change**

```bash
git add requirements.txt src/settings.py .env.example tests/test_settings.py
git commit -m "feat: add disabled account state lock settings"
```

### Task 2: Build the locked disabled-account state helper layer

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing helper tests for lock path, retry success, and timeout**

```python
    def test_disabled_accounts_lock_path_uses_sibling_lock_file(self):
        self.assertEqual(
            self.maintainer._disabled_accounts_lock_path(),
            pathlib.Path(f"{self.maintainer.disabled_accounts_path}.lock"),
        )
```

```python
    def test_locked_update_tracked_disabled_accounts_waits_for_lock_then_succeeds(self):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
            disabled_state_lock_timeout_seconds=0.3,
            disabled_state_lock_retry_interval_seconds=0.01,
        )
        maintainer = CPACodexKeeper(settings=settings, dry_run=True)
        maintainer.disabled_accounts_path = shared_path
        lock = FileLock(str(shared_path) + ".lock")

        with lock.acquire(timeout=0):
            release_thread = threading.Thread(target=lambda: (time.sleep(0.05), lock.release()))
            release_thread.start()
            success = maintainer._locked_update_tracked_disabled_accounts(
                "测试写入",
                lambda state: state.__setitem__("token-a", {"next_check_at": 1234}),
            )
            release_thread.join()

        self.assertTrue(success)
        payload = json.loads(shared_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"token-a": {"next_check_at": 1234}})
```

```python
    def test_locked_update_tracked_disabled_accounts_times_out_without_mutating_cache_or_disk(self):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        shared_path.write_text('{"token-old": {"next_check_at": 900}}', encoding="utf-8")
        settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
            disabled_state_lock_timeout_seconds=0.05,
            disabled_state_lock_retry_interval_seconds=0.01,
        )
        maintainer = CPACodexKeeper(settings=settings, dry_run=True)
        maintainer.disabled_accounts_path = shared_path
        maintainer._tracked_disabled_accounts = {"token-old": {"next_check_at": 900}}
        lock = FileLock(str(shared_path) + ".lock")

        with lock.acquire(timeout=0):
            success = maintainer._locked_update_tracked_disabled_accounts(
                "测试超时",
                lambda state: state.__setitem__("token-new", {"next_check_at": 1000}),
            )

        self.assertFalse(success)
        self.assertEqual(
            json.loads(shared_path.read_text(encoding="utf-8")),
            {"token-old": {"next_check_at": 900}},
        )
        self.assertEqual(maintainer._tracked_disabled_accounts, {"token-old": {"next_check_at": 900}})
```

- [ ] **Step 2: Run the helper-focused tests and verify they fail**

Run: `python -m pytest tests/test_maintainer.py -k "disabled_accounts_lock_path or locked_update_tracked_disabled_accounts" -v`
Expected: FAIL because the lock helper methods do not exist and `filelock` is not wired into `CPACodexKeeper`.

- [ ] **Step 3: Import `filelock` and add the lock-path helper in `src/maintainer.py`**

```python
from filelock import FileLock, Timeout as FileLockTimeout
```

```python
def _disabled_accounts_lock_path(self):
    return Path(f"{self.disabled_accounts_path}.lock")
```

- [ ] **Step 4: Change `_save_disabled_accounts_state` to accept an explicit payload**

```python
def _save_disabled_accounts_state(self, payload):
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    self.disabled_accounts_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = self.disabled_accounts_path.with_suffix(f"{self.disabled_accounts_path.suffix}.tmp")
    tmp_path.write_text(serialized + "\n", encoding="utf-8")
    tmp_path.replace(self.disabled_accounts_path)
```

- [ ] **Step 5: Add the locked update helper in `src/maintainer.py`**

```python
def _locked_update_tracked_disabled_accounts(self, action_label, mutator):
    lock = FileLock(str(self._disabled_accounts_lock_path()))
    started_at = time.monotonic()
    try:
        with lock.acquire(
            timeout=self.settings.disabled_state_lock_timeout_seconds,
            poll_interval=self.settings.disabled_state_lock_retry_interval_seconds,
        ):
            waited_seconds = time.monotonic() - started_at
            state = self._load_disabled_accounts_state()
            mutator(state)
            self._save_disabled_accounts_state(state)
            with self._state_lock:
                self._tracked_disabled_accounts = state
            if waited_seconds >= self.settings.disabled_state_lock_retry_interval_seconds:
                self.log("INFO", f"禁用账号计划锁已获取：{action_label}（等待 {waited_seconds:.2f} 秒）")
            return True
    except FileLockTimeout:
        self.log(
            "ERROR",
            f"获取禁用账号计划锁超时：{action_label}（{self.settings.disabled_state_lock_timeout_seconds} 秒）",
        )
        return False
```

- [ ] **Step 6: Run the helper-focused tests and verify they pass**

Run: `python -m pytest tests/test_maintainer.py -k "disabled_accounts_lock_path or locked_update_tracked_disabled_accounts" -v`
Expected: PASS

- [ ] **Step 7: Commit the helper-layer change**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: lock disabled account state updates"
```

### Task 3: Route tracked-state mutations and startup recovery through the lock helper

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing mutation and startup tests**

```python
    @patch("src.maintainer.threading.Timer")
    def test_set_tracked_next_check_at_preserves_entries_loaded_from_disk(self, timer_cls):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        shared_path.write_text('{"token-old": {"next_check_at": 900}}', encoding="utf-8")

        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)
        maintainer.disabled_accounts_path = shared_path
        maintainer._tracked_disabled_accounts = {}

        with patch("src.maintainer.time.time", return_value=1000):
            success = maintainer._set_tracked_next_check_at("token-new", 1100)

        self.assertTrue(success)
        self.assertEqual(
            json.loads(shared_path.read_text(encoding="utf-8")),
            {
                "token-old": {"next_check_at": 900},
                "token-new": {"next_check_at": 1100},
            },
        )
        timer_cls.assert_called_once_with(100, maintainer._run_tracked_recheck, args=("token-new",))
```

```python
    @patch("src.maintainer.threading.Timer")
    def test_remove_tracked_account_preserves_other_entries_from_disk(self, _timer_cls):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        shared_path.write_text(
            '{"token-remove": {"next_check_at": 900}, "token-keep": {"next_check_at": 1200}}',
            encoding="utf-8",
        )
        self.maintainer.disabled_accounts_path = shared_path
        self.maintainer._tracked_disabled_accounts = {"token-remove": {"next_check_at": 900}}

        success = self.maintainer._remove_tracked_account("token-remove")

        self.assertTrue(success)
        self.assertEqual(
            json.loads(shared_path.read_text(encoding="utf-8")),
            {"token-keep": {"next_check_at": 1200}},
        )
```

```python
    @patch("src.maintainer.threading.Timer")
    def test_start_tracked_rechecks_loads_latest_disk_state_even_when_cache_is_stale(self, timer_cls):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        shared_path.write_text('{"token-fresh": {"next_check_at": 1100}}', encoding="utf-8")
        self.maintainer.disabled_accounts_path = shared_path
        self.maintainer._tracked_disabled_accounts = {"token-stale": {"next_check_at": 900}}

        with patch("src.maintainer.time.time", return_value=1000):
            self.maintainer._start_tracked_rechecks()

        self.assertEqual(self.maintainer._tracked_disabled_accounts, {"token-fresh": {"next_check_at": 1100}})
        timer_cls.assert_called_once_with(100, self.maintainer._run_tracked_recheck, args=("token-fresh",))
```

- [ ] **Step 2: Run the state-mutation tests and verify they fail**

Run: `python -m pytest tests/test_maintainer.py -k "preserves_entries_loaded_from_disk or preserves_other_entries_from_disk or loads_latest_disk_state_even_when_cache_is_stale" -v`
Expected: FAIL because `_set_tracked_next_check_at()` and `_remove_tracked_account()` do not return success flags and still mutate via per-instance locking only.

- [ ] **Step 3: Make `_reload_tracked_disabled_accounts_state()` return the latest disk state**

```python
def _reload_tracked_disabled_accounts_state(self):
    state = self._load_disabled_accounts_state()
    with self._state_lock:
        self._tracked_disabled_accounts = state
    return state
```

- [ ] **Step 4: Update `_set_tracked_next_check_at()` and `_remove_tracked_account()` to use the locked helper and only change timers after success**

```python
def _set_tracked_next_check_at(self, name, ts):
    ts_int = int(ts)
    success = self._locked_update_tracked_disabled_accounts(
        f"记录 {name} 的下次复查时间",
        lambda state: state.__setitem__(name, {"next_check_at": ts_int}),
    )
    if success:
        self._schedule_tracked_recheck(name, ts_int)
    return success
```

```python
def _remove_tracked_account(self, name):
    success = self._locked_update_tracked_disabled_accounts(
        f"移除 {name} 的复查计划",
        lambda state: state.pop(name, None),
    )
    if success:
        self._cancel_tracked_recheck_timer(name)
    return success
```

- [ ] **Step 5: Update `_start_tracked_rechecks()` to prefer disk over stale cache**

```python
def _start_tracked_rechecks(self):
    with self._state_lock:
        if self._tracked_rechecks_started:
            return
        self._tracked_rechecks_started = True
    tracked_entries = list(self._reload_tracked_disabled_accounts_state().items())
    for name, entry in tracked_entries:
        if not isinstance(entry, dict):
            continue
        next_check_at = entry.get("next_check_at")
        if isinstance(next_check_at, int):
            self._schedule_tracked_recheck(name, next_check_at)
```

- [ ] **Step 6: Run the state-mutation tests and verify they pass**

Run: `python -m pytest tests/test_maintainer.py -k "preserves_entries_loaded_from_disk or preserves_other_entries_from_disk or loads_latest_disk_state_even_when_cache_is_stale" -v`
Expected: PASS

- [ ] **Step 7: Commit the mutation-routing change**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: route tracked state changes through file lock"
```

### Task 4: Harden due recheck processing and quota-flow cleanup

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing recheck tests for missing persisted membership and successful cleanup**

```python
    def test_run_tracked_recheck_skips_when_persisted_entry_is_gone(self):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        shared_path.write_text("{}", encoding="utf-8")
        self.maintainer.disabled_accounts_path = shared_path
        self.maintainer._tracked_disabled_accounts = {"t-missing": {"next_check_at": 1000}}
        self.maintainer.process_token = Mock(return_value="alive")
        self.maintainer.log = Mock()

        self.maintainer._run_tracked_recheck("t-missing")

        self.maintainer.process_token.assert_not_called()
        self.maintainer.log.assert_any_call("INFO", "账号 t-missing 的复查计划已不存在，跳过本次定时复查")
```

```python
    def test_run_tracked_recheck_enables_due_token_and_removes_persisted_plan(self):
        shared_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        shared_path.write_text('{"t-enable": {"next_check_at": 1000}}', encoding="utf-8")
        self.maintainer.disabled_accounts_path = shared_path
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
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
                    "primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1776634820},
                    "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800, "reset_at": 1777000096},
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.set_disabled_status = Mock(return_value=True)

        with patch("src.maintainer.time.time", return_value=1000):
            self.maintainer._run_tracked_recheck("t-enable")

        self.assertEqual(json.loads(shared_path.read_text(encoding="utf-8")), {})
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-enable"))
```

- [ ] **Step 2: Run the recheck-flow tests and verify they fail**

Run: `python -m pytest tests/test_maintainer.py -k "persisted_entry_is_gone or removes_persisted_plan" -v`
Expected: FAIL because `_run_tracked_recheck()` only trusts in-memory membership and does not verify the latest persisted state before processing.

- [ ] **Step 3: Refresh state at the start of `_run_tracked_recheck()` and skip missing persisted entries**

```python
def _run_tracked_recheck(self, name):
    self._tracked_recheck_timers.pop(name, None)
    tracked_state = self._reload_tracked_disabled_accounts_state()
    if name not in tracked_state:
        self.log("INFO", f"账号 {name} 的复查计划已不存在，跳过本次定时复查")
        return
    self._acquire_priority("timer")
    try:
        self.logger.emit_lines([
            self.logger.format_log_record("INFO", f"账号 {name} 到达计划复查时间，开始复查使用额度")
        ])
        self.process_token({"name": name}, 1, 1)
    except Exception as exc:
        self.log("ERROR", f"账号 {name} 定时复查异常: {exc}")
    finally:
        self._release_priority("timer")
```

- [ ] **Step 4: Update quota-policy call sites to handle locked state helper failures explicitly**

```python
                if self.set_disabled_status(name, disabled=False, logger=logger):
                    if self._remove_tracked_account(name):
                        logger.log("ENABLE", "账号已重新启用", indent=1)
                    else:
                        logger.log("ERROR", "账号已启用，但移除复查计划失败", indent=1)
                    self._inc_stat("enabled")
                    effective_disabled = False
```

```python
            if self.set_disabled_status(name, disabled=True, logger=logger):
                effective_disabled = True
                next_check_at = self._compute_next_check_at_from_usage(
                    body_info or {},
                    now if now is not None else int(time.time()),
                    self.settings.quota_reset_none_recheck_seconds,
                    token_detail=token_detail,
                )
                if self._set_tracked_next_check_at(name, next_check_at):
                    logger.log("INFO", f"已记录下次检查额度时间: {self._format_tracked_next_check_at(next_check_at)}", indent=1)
                else:
                    logger.log("ERROR", "账号已禁用，但记录复查计划失败", indent=1)
                logger.log("DISABLE", "账号已禁用", indent=1)
                self._inc_stat("disabled")
```

- [ ] **Step 5: Run the focused recheck-flow tests and verify they pass**

Run: `python -m pytest tests/test_maintainer.py -k "persisted_entry_is_gone or removes_persisted_plan" -v`
Expected: PASS

- [ ] **Step 6: Run the broader maintainer and settings regression targets**

Run: `python -m pytest tests/test_settings.py tests/test_maintainer.py -v`
Expected: PASS

- [ ] **Step 7: Commit the recheck hardening change**

```bash
git add src/maintainer.py tests/test_maintainer.py tests/test_settings.py requirements.txt .env.example
git commit -m "fix: serialize disabled account state updates"
```

## Self-review checklist

- Spec coverage:
  - lock dependency and config: Task 1
  - cross-instance locked mutation path: Task 2
  - persisted JSON preservation across add/remove/startup: Task 3
  - due recheck reliability and persisted cleanup: Task 4
- Placeholder scan:
  - no `TODO`, `TBD`, or “write tests later” placeholders remain
- Type consistency:
  - `disabled_state_lock_timeout_seconds` and `disabled_state_lock_retry_interval_seconds` are `float`
  - `_save_disabled_accounts_state(payload)` consistently accepts explicit payload
  - `_locked_update_tracked_disabled_accounts(action_label, mutator)` returns `bool`
  - `_set_tracked_next_check_at(name, ts)` and `_remove_tracked_account(name)` return `bool`

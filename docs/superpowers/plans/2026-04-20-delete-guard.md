# Delete Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global delete guard so `CPA_ALLOW_DELETE=false` converts auth-file deletions into disable actions and records readable history events in `delete_blocked_accounts.json`.

**Architecture:** Extend `src/settings.py` with one new boolean flag, then keep delete-policy branching centralized inside `src/maintainer.py` by upgrading the existing delete-with-reason helper to either delete or disable. Keep `disabled_accounts.json` semantics unchanged by not enrolling delete-blocked fallback disables into tracked rechecks, and persist those fallback events in a separate append-only JSON history file guarded by the existing state lock.

**Tech Stack:** Python 3.11+, standard library `json`, `datetime`, `pathlib`, `unittest`, `unittest.mock`

---

## File map

- **Modify:** `src/settings.py`
  - add `DEFAULT_ALLOW_DELETE`
  - add `allow_delete` to `Settings`
  - load `CPA_ALLOW_DELETE` via `_read_bool(...)`
- **Modify:** `src/maintainer.py`
  - add `delete_blocked_accounts_path`
  - add minimal helpers to load/save/append `delete_blocked_accounts.json`
  - upgrade `_delete_token_with_reason(...)` so it deletes when allowed and disables when blocked
  - route invalid-token, expired-without-refresh, and quota-without-refresh delete paths through the same helper
  - add startup logging for the delete guard setting
- **Modify:** `tests/test_settings.py`
  - verify explicit `CPA_ALLOW_DELETE=false`
  - verify default remains `true`
- **Modify:** `tests/test_maintainer.py`
  - add delete-blocked history helper tests
  - add delete-to-disable fallback tests
  - verify fallback does not write `disabled_accounts.json`
  - verify failed fallback does not append history
  - verify summaries still report fallback as disabled instead of dead
- **Modify:** `.env.example`
  - document `CPA_ALLOW_DELETE`
- **Modify:** `README.md`
  - document `CPA_ALLOW_DELETE`
  - document `delete_blocked_accounts.json`
- **Modify:** `README.en.md`
  - document `CPA_ALLOW_DELETE`
  - document `delete_blocked_accounts.json`
- **Create at runtime:** `delete_blocked_accounts.json`
  - append-only history of successful delete-to-disable fallback events

---

### Task 1: Add `CPA_ALLOW_DELETE` to settings

**Files:**
- Modify: `src/settings.py:6-18`
- Modify: `src/settings.py:25-42`
- Modify: `src/settings.py:112-143`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_settings.py` near the existing config parsing tests:

```python
def test_load_settings_reads_allow_delete_false(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
            "CPA_ALLOW_DELETE": "false",
        },
        clear=True,
    ):
        settings = load_settings()

    self.assertFalse(settings.allow_delete)


def test_load_settings_uses_default_allow_delete(self):
    with patch.dict(
        os.environ,
        {
            "CPA_ENDPOINT": "https://example.com",
            "CPA_TOKEN": "secret",
        },
        clear=True,
    ):
        settings = load_settings()

    self.assertTrue(settings.allow_delete)
```

Expected first failure: `AttributeError: 'Settings' object has no attribute 'allow_delete'`

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_settings.SettingsTests.test_load_settings_reads_allow_delete_false \
  tests.test_settings.SettingsTests.test_load_settings_uses_default_allow_delete
```

Expected: FAIL because `allow_delete` does not exist yet.

- [ ] **Step 3: Write the minimal implementation**

In `src/settings.py`, add the new default constant near the other defaults:

```python
DEFAULT_ALLOW_DELETE = True
```

Extend the settings dataclass:

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
    log_archive_max_size_mb: int = DEFAULT_LOG_ARCHIVE_MAX_SIZE_MB
```

Load the field in `load_settings(...)`:

```python
enable_refresh=_read_bool("CPA_ENABLE_REFRESH", DEFAULT_ENABLE_REFRESH, env_values),
allow_delete=_read_bool("CPA_ALLOW_DELETE", DEFAULT_ALLOW_DELETE, env_values),
log_archive_max_size_mb=_read_int(
    "CPA_LOG_ARCHIVE_MAX_SIZE_MB",
    DEFAULT_LOG_ARCHIVE_MAX_SIZE_MB,
    env_values,
    minimum=1,
),
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/settings.py tests/test_settings.py
git commit -m "feat: add delete guard setting"
```

---

### Task 2: Add `delete_blocked_accounts.json` history helpers

**Files:**
- Modify: `src/maintainer.py:88-120`
- Modify: `src/maintainer.py:198-212`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add `import json` at the top of `tests/test_maintainer.py`, then add these tests near the existing state-file tests:

```python
def test_append_delete_blocked_event_creates_history_file(self):
    self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete_blocked_accounts.json"

    self.maintainer._append_delete_blocked_event(
        name="token-a",
        reason="Token 无效或 workspace 已停用，准备删除",
        trigger="401_or_402",
    )

    payload = json.loads(self.maintainer.delete_blocked_accounts_path.read_text(encoding="utf-8"))
    self.assertEqual(len(payload["events"]), 1)
    self.assertEqual(payload["events"][0]["name"], "token-a")
    self.assertEqual(payload["events"][0]["reason"], "Token 无效或 workspace 已停用，准备删除")
    self.assertEqual(payload["events"][0]["source_action"], "delete")
    self.assertEqual(payload["events"][0]["trigger"], "401_or_402")
    self.assertRegex(payload["events"][0]["updated_at"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def test_append_delete_blocked_event_appends_to_existing_history(self):
    self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete_blocked_accounts.json"
    self.maintainer.delete_blocked_accounts_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "name": "token-old",
                        "reason": "old reason",
                        "source_action": "delete",
                        "trigger": "quota_without_refresh_token",
                        "updated_at": "2026-04-20 12:00:00",
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    self.maintainer._append_delete_blocked_event(
        name="token-new",
        reason="Token 已过期且无 Refresh Token，准备删除",
        trigger="expired_without_refresh_token",
    )

    payload = json.loads(self.maintainer.delete_blocked_accounts_path.read_text(encoding="utf-8"))
    self.assertEqual([event["name"] for event in payload["events"]], ["token-old", "token-new"])
```

Expected first failure: `AttributeError: 'CPACodexKeeper' object has no attribute '_append_delete_blocked_event'`

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_append_delete_blocked_event_creates_history_file \
  tests.test_maintainer.MaintainerTests.test_append_delete_blocked_event_appends_to_existing_history
```

Expected: FAIL because the helper and history path do not exist yet.

- [ ] **Step 3: Write the minimal implementation**

Add the new path in `CPACodexKeeper.__init__` near `disabled_accounts_path`:

```python
self.delete_blocked_accounts_path = Path(__file__).resolve().parents[1] / "delete_blocked_accounts.json"
```

Add these helpers near the tracked-state helpers:

```python
def _load_delete_blocked_history(self):
    if not self.delete_blocked_accounts_path.exists():
        return {"events": []}
    try:
        data = json.loads(self.delete_blocked_accounts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": []}
    if not isinstance(data, dict):
        return {"events": []}
    events = data.get("events")
    if not isinstance(events, list):
        return {"events": []}
    return {"events": events}


def _save_delete_blocked_history(self, payload):
    self.delete_blocked_accounts_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _delete_blocked_updated_at(self):
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_delete_blocked_event(self, *, name, reason, trigger):
    with self._state_lock:
        payload = self._load_delete_blocked_history()
        payload["events"].append(
            {
                "name": name,
                "reason": reason,
                "source_action": "delete",
                "trigger": trigger,
                "updated_at": self._delete_blocked_updated_at(),
            }
        )
        self._save_delete_blocked_history(payload)
```

Use `_state_lock` here so concurrent worker threads cannot lose events when they append to the same history file.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: persist delete-blocked history events"
```

---

### Task 3: Upgrade invalid-token and expired-token delete paths to use delete-or-disable behavior

**Files:**
- Modify: `src/maintainer.py:550-567`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing delete-path tests in `tests/test_maintainer.py`:

```python
def test_process_token_invalid_token_disables_when_delete_not_allowed(self):
    self.maintainer.settings.allow_delete = False
    self.maintainer.get_token_detail = Mock(return_value={
        "email": "a@example.com",
        "disabled": False,
        "access_token": "token",
        "account_id": "acc",
        "expired": "2099-01-01T00:00:00Z",
    })
    self.maintainer.check_token_live = Mock(return_value=(401, {"brief": "unauthorized"}))
    self.maintainer.delete_token = Mock(return_value=True)
    self.maintainer.set_disabled_status = Mock(return_value=True)
    self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete-blocked-invalid.json"

    result = self.maintainer.process_token({"name": "t-invalid"}, 1, 1)

    self.assertEqual(result, "alive")
    self.maintainer.delete_token.assert_not_called()
    self.maintainer.set_disabled_status.assert_called_once_with("t-invalid", disabled=True, logger=ANY)
    payload = json.loads(self.maintainer.delete_blocked_accounts_path.read_text(encoding="utf-8"))
    self.assertEqual(payload["events"][0]["trigger"], "401_or_402")
    self.assertEqual(self.maintainer.stats.dead, 0)
    self.assertEqual(self.maintainer.stats.disabled, 1)


def test_process_token_expired_without_refresh_disables_when_delete_not_allowed(self):
    self.maintainer.settings.allow_delete = False
    self.maintainer.get_token_detail = Mock(return_value={
        "email": "a@example.com",
        "disabled": False,
        "access_token": "token",
        "refresh_token": "",
        "account_id": "acc",
        "expired": "1970-01-01T00:00:00Z",
    })
    self.maintainer.delete_token = Mock(return_value=True)
    self.maintainer.set_disabled_status = Mock(return_value=True)
    self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete-blocked-expired.json"

    with patch("src.maintainer.time.time", return_value=1000):
        result = self.maintainer.process_token({"name": "t-expired"}, 1, 1)

    self.assertEqual(result, "alive")
    self.maintainer.delete_token.assert_not_called()
    self.maintainer.set_disabled_status.assert_called_once_with("t-expired", disabled=True, logger=ANY)
    payload = json.loads(self.maintainer.delete_blocked_accounts_path.read_text(encoding="utf-8"))
    self.assertEqual(payload["events"][0]["trigger"], "expired_without_refresh_token")
```

Expected first failure: current code still deletes and returns `"dead"`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_process_token_invalid_token_disables_when_delete_not_allowed \
  tests.test_maintainer.MaintainerTests.test_process_token_expired_without_refresh_disables_when_delete_not_allowed
```

Expected: FAIL because `_delete_token_with_reason(...)` still deletes unconditionally.

- [ ] **Step 3: Write the minimal implementation**

Upgrade `_delete_token_with_reason(...)` in `src/maintainer.py` so it becomes the single decision point:

```python
def _delete_token_with_reason(self, name, reason, trigger, logger):
    logger.log("WARN", reason, indent=1)
    if self.settings.allow_delete:
        if self.delete_token(name, logger=logger):
            self._remove_tracked_account(name)
            logger.log("DELETE", "账号文件已删除", indent=1)
            self._inc_stat("dead")
            logger.blank_line()
            return "dead"
        return self._skip_token("删除失败", logger)

    logger.log("INFO", "检测到 CPA_ALLOW_DELETE=false，删除已禁止，改为禁用账号", indent=1)
    if self.set_disabled_status(name, disabled=True, logger=logger):
        self._remove_tracked_account(name)
        self._append_delete_blocked_event(name=name, reason=reason, trigger=trigger)
        logger.log("DISABLE", "账号已禁用（原动作为删除）", indent=1)
        self._inc_stat("disabled")
        logger.blank_line()
        return "alive"
    return self._skip_token("禁用失败", logger)
```

Update callers:

```python
def _handle_invalid_token(self, name, logger):
    return self._delete_token_with_reason(name, "Token 无效或 workspace 已停用，准备删除", "401_or_402", logger)


def _apply_non_refreshable_expiry_policy(self, name, token_detail, remaining_seconds, expiry_known, logger):
    if self._has_refresh_token(token_detail) or not expiry_known or remaining_seconds > 0:
        return None
    return self._delete_token_with_reason(name, "Token 已过期且无 Refresh Token，准备删除", "expired_without_refresh_token", logger)
```

Do not change `delete_token(...)`; keep it as the low-level delete operation.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: add delete-blocked disable fallback"
```

---

### Task 4: Apply the same fallback to quota-without-refresh delete branches

**Files:**
- Modify: `src/maintainer.py:651-656`
- Modify: `src/maintainer.py:679-684`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing no-refresh quota tests in `tests/test_maintainer.py`:

```python
def test_process_token_quota_hit_without_refresh_disables_when_delete_not_allowed(self):
    self.maintainer.settings.allow_delete = False
    self.maintainer.get_token_detail = Mock(return_value={
        "email": "a@example.com",
        "disabled": False,
        "access_token": "token",
        "refresh_token": "",
        "account_id": "acc",
        "expired": "2099-01-01T00:00:00Z",
    })
    self.maintainer.check_token_live = Mock(return_value=(200, {
        "json": {
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {"used_percent": 100, "limit_window_seconds": 604800, "reset_at": None},
                "secondary_window": None,
            },
            "credits": {"has_credits": False},
        }
    }))
    self.maintainer.delete_token = Mock(return_value=True)
    self.maintainer.set_disabled_status = Mock(return_value=True)
    self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete-blocked-quota.json"

    result = self.maintainer.process_token({"name": "t-no-refresh-quota"}, 1, 1)

    self.assertEqual(result, "alive")
    self.maintainer.delete_token.assert_not_called()
    self.maintainer.set_disabled_status.assert_called_once_with("t-no-refresh-quota", disabled=True, logger=ANY)
    self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-no-refresh-quota"))
    payload = json.loads(self.maintainer.delete_blocked_accounts_path.read_text(encoding="utf-8"))
    self.assertEqual(payload["events"][0]["trigger"], "quota_without_refresh_token")


def test_process_token_delete_blocked_history_is_not_written_when_disable_fails(self):
    self.maintainer.settings.allow_delete = False
    self.maintainer.get_token_detail = Mock(return_value={
        "email": "a@example.com",
        "disabled": False,
        "access_token": "token",
        "account_id": "acc",
        "expired": "2099-01-01T00:00:00Z",
    })
    self.maintainer.check_token_live = Mock(return_value=(401, {"brief": "unauthorized"}))
    self.maintainer.set_disabled_status = Mock(return_value=False)
    self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete-blocked-failed.json"

    result = self.maintainer.process_token({"name": "t-fail-disable"}, 1, 1)

    self.assertEqual(result, "skipped")
    self.assertFalse(self.maintainer.delete_blocked_accounts_path.exists())
```

Expected first failure: quota branches still call the old delete-only path.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_process_token_quota_hit_without_refresh_disables_when_delete_not_allowed \
  tests.test_maintainer.MaintainerTests.test_process_token_delete_blocked_history_is_not_written_when_disable_fails
```

Expected: FAIL because quota delete branches are not routed through fallback yet.

- [ ] **Step 3: Write the minimal implementation**

Replace the two no-refresh delete branches in `_apply_quota_policy(...)` with:

```python
return self._delete_token_with_reason(
    name,
    f"无 Refresh Token，且{reached_summary} >= {self.settings.quota_threshold}%，准备删除",
    "quota_without_refresh_token",
    logger,
), effective_disabled
```

Use that replacement for both:

- the already-disabled branch at `src/maintainer.py:651-656`
- the newly-threshold-hit branch at `src/maintainer.py:679-684`

Do not add any `_set_tracked_next_check_at(...)` call for delete-to-disable fallback. These tokens must stay out of `disabled_accounts.json`.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: apply delete guard to quota delete paths"
```

---

### Task 5: Document and surface the delete guard in startup output

**Files:**
- Modify: `src/maintainer.py:816-835`
- Modify: `.env.example`
- Modify: `README.md:137-160`
- Modify: `README.en.md:136-159`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing test**

Add this test near the existing startup-log tests in `tests/test_maintainer.py`:

```python
def test_log_startup_includes_delete_guard_setting(self):
    self.maintainer.settings.allow_delete = False
    captured = []
    self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured.extend(lines))

    self.maintainer.log_startup()

    joined = "\n".join(captured)
    self.assertIn("允许删除账号文件: 关闭", joined)
```

Expected first failure: startup logs do not mention the new setting.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.test_maintainer.MaintainerTests.test_log_startup_includes_delete_guard_setting
```

Expected: FAIL because `log_startup()` does not include `allow_delete` yet.

- [ ] **Step 3: Write the minimal implementation**

Add this line to the `lines` list in `log_startup()`:

```python
self.logger.format_log_record("INFO", f"允许删除账号文件: {'开启' if self.settings.allow_delete else '关闭'}", indent=1),
```

Update `.env.example` near `CPA_ENABLE_REFRESH`:

```dotenv
# Whether auth-files may be deleted / 是否允许删除账号文件
# When false, main-flow delete actions are converted into disable actions instead
# 关闭后，主巡检中的删除动作会改为禁用动作
CPA_ALLOW_DELETE=true
```

Update `README.md` in the configuration list:

```markdown
- `CPA_ALLOW_DELETE`：是否允许删除账号文件，默认 `true`；设为 `false` 时，主巡检中原本的删除动作会改为禁用
```

Add this sentence after the `disabled_accounts.json` paragraph:

```markdown
当 `CPA_ALLOW_DELETE=false` 时，原本会删除的账号会改为禁用，并把事件追加记录到项目根目录的 `delete_blocked_accounts.json`；这类记录不会进入 `disabled_accounts.json`，也不会参与自动复查。
```

Update `README.en.md` similarly:

```markdown
- `CPA_ALLOW_DELETE`: whether auth-files may be deleted, default `true`; when set to `false`, main-flow delete actions are converted into disable actions
```

```markdown
When `CPA_ALLOW_DELETE=false`, accounts that would otherwise be deleted are disabled instead, and a history event is appended to `delete_blocked_accounts.json` in the project root; these fallback records do not enter `disabled_accounts.json` and do not participate in automatic rechecks.
```

- [ ] **Step 4: Run test to verify it passes**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py .env.example README.md README.en.md tests/test_maintainer.py
git commit -m "docs: describe delete guard behavior"
```

---

### Task 6: Run focused and broad delete-guard regressions

**Files:**
- Modify: `tests/test_maintainer.py`
- Verify: `tests/test_settings.py`
- Verify: `.env.example`, `README.md`, `README.en.md`

- [ ] **Step 1: Add the summary regression test if still missing**

Add this test near the existing summary test `test_run_emits_detailed_summary` in `tests/test_maintainer.py`:

```python
@patch("src.maintainer.time.time", side_effect=[1000, 1001.2])
def test_run_summary_counts_delete_blocked_fallback_as_disabled_not_dead(self, _time_mock):
    emitted_batches = []
    self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: emitted_batches.append(list(lines)))
    self.maintainer.settings.allow_delete = False
    self.maintainer.get_token_list = Mock(return_value=[{"name": "token-a", "type": "codex"}])

    def process_side_effect(*_args, **_kwargs):
        self.maintainer.stats.disabled += 1
        return "alive"

    self.maintainer.process_token = Mock(side_effect=process_side_effect)

    self.maintainer.run()

    summary = emitted_batches[-1]
    self.assertTrue(any("已禁用: 1" in line and "][DISABLED]:" in line for line in summary))
    self.assertTrue(any("死号(已删除): 0" in line and "][DELETE]:" in line for line in summary))
```

Expected first failure only if a previous task accidentally increments `dead` or leaves the summary inconsistent.

- [ ] **Step 2: Run the focused regression suite**

Run:

```bash
python -m unittest \
  tests.test_settings \
  tests.test_maintainer.MaintainerTests.test_append_delete_blocked_event_creates_history_file \
  tests.test_maintainer.MaintainerTests.test_append_delete_blocked_event_appends_to_existing_history \
  tests.test_maintainer.MaintainerTests.test_process_token_invalid_token_disables_when_delete_not_allowed \
  tests.test_maintainer.MaintainerTests.test_process_token_expired_without_refresh_disables_when_delete_not_allowed \
  tests.test_maintainer.MaintainerTests.test_process_token_quota_hit_without_refresh_disables_when_delete_not_allowed \
  tests.test_maintainer.MaintainerTests.test_process_token_delete_blocked_history_is_not_written_when_disable_fails \
  tests.test_maintainer.MaintainerTests.test_log_startup_includes_delete_guard_setting \
  tests.test_maintainer.MaintainerTests.test_run_summary_counts_delete_blocked_fallback_as_disabled_not_dead
```

Expected: PASS.

- [ ] **Step 3: Run the broader regression suite**

Run:

```bash
python -m unittest tests.test_cli tests.test_maintainer tests.test_settings
```

Expected: PASS with no regressions in tracked rechecks, refresh behavior, or summaries.

- [ ] **Step 4: Confirm existing delete-allowed coverage still passes**

Run these existing tests explicitly to prove the default `allow_delete=True` path still deletes:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_process_token_deletes_invalid_token_on_401 \
  tests.test_maintainer.MaintainerTests.test_process_token_deletes_invalid_token_on_402 \
  tests.test_maintainer.MaintainerTests.test_process_token_removes_schedule_entry_when_token_deleted
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_settings.py tests/test_maintainer.py .env.example README.md README.en.md src/settings.py src/maintainer.py
git commit -m "test: verify delete guard regressions"
```

---

## Self-review checklist

- Spec coverage: the plan covers the new config flag, delete-to-disable fallback, separate history persistence, `.env.example`, Chinese/English docs, and regression verification.
- Placeholder scan: no `TBD`, `TODO`, or unspecified code changes remain.
- Type consistency: the plan consistently uses `allow_delete`, `delete_blocked_accounts.json`, and trigger values `401_or_402`, `expired_without_refresh_token`, and `quota_without_refresh_token`.
- Scope check: the plan keeps `disabled_accounts.json` semantics unchanged and does not introduce extra per-reason configuration.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-20-delete-guard.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
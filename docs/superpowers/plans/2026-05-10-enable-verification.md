# Enable Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add post-enable verification with 5-second delayed rechecks and up to 3 attempts for both the maintainer auto-enable path and the batch enable script, while keeping maintainer failures in the tracked retry flow.

**Architecture:** Extend `Settings` with maintainer-only enable verification knobs, add a focused helper in `CPACodexKeeper` that performs `enable -> sleep -> refetch -> confirm`, and add a separate lightweight helper in `enable_all_codex.py` with hard-coded `5s/3 attempts`. Keep tracked-state removal and stats updates at the maintainer call site so success semantics stay explicit.

**Tech Stack:** Python 3.12, unittest, unittest.mock, existing CPAClient/CPACodexKeeper code, `.env`-driven settings for maintainer only.

---

## File map

- Modify: `src/settings.py:5-21,29-49,120-180`
  - Add maintainer enable verification defaults, dataclass fields, and env parsing.
- Modify: `.env.example:1-75`
  - Document maintainer-only enable verification env vars.
- Modify: `tests/test_settings.py:65-242`
  - Add tests for default values, explicit values, and rejection of non-positive values.
- Modify: `src/maintainer.py:160-175,752-778`
  - Add a helper that verifies enablement after each attempt and route auto-enable through it.
- Modify: `tests/test_maintainer.py:653-684,764-803`
  - Add tests for retry-success, final failure, and detail-fetch failure during verification.
- Modify: `enable_all_codex.py:16-19,94-133`
  - Add script-local verification constants/helper and wire `enable_accounts()` through it.
- Modify: `tests/test_enable_all_codex.py:85-181`
  - Add tests for first-try success, retry-success, final failure with summary prompt, and continuing after failure.

## Task 1: Add maintainer verification settings

**Files:**
- Modify: `src/settings.py:5-21,29-49,133-180`
- Modify: `.env.example:23-75`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Write the failing settings tests**

Add these tests near the other env parsing tests in `tests/test_settings.py`:

```python
    def test_load_settings_uses_default_enable_verify_values(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.enable_verify_delay_seconds, 5)
        self.assertEqual(settings.enable_verify_max_attempts, 3)

    def test_load_settings_reads_enable_verify_values(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_ENABLE_VERIFY_DELAY_SECONDS": "7",
                "CPA_ENABLE_VERIFY_MAX_ATTEMPTS": "4",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.enable_verify_delay_seconds, 7)
        self.assertEqual(settings.enable_verify_max_attempts, 4)

    def test_load_settings_rejects_non_positive_enable_verify_delay(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_ENABLE_VERIFY_DELAY_SECONDS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()

    def test_load_settings_rejects_non_positive_enable_verify_max_attempts(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_ENABLE_VERIFY_MAX_ATTEMPTS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()
```

- [ ] **Step 2: Run the settings tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_settings.py -k enable_verify -v
```

Expected: `AttributeError` or assertion failures because `Settings` does not yet define `enable_verify_delay_seconds` / `enable_verify_max_attempts`.

- [ ] **Step 3: Add the settings fields and parsing**

Update `src/settings.py` constants and dataclass:

```python
DEFAULT_ENABLE_VERIFY_DELAY_SECONDS = 5
DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS = 3
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
    enable_verify_delay_seconds: int = DEFAULT_ENABLE_VERIFY_DELAY_SECONDS
    enable_verify_max_attempts: int = DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS
```

Update `load_settings()`:

```python
        disabled_state_lock_retry_interval_seconds=_read_float(
            "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS",
            DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS,
            env_values,
        ),
        enable_verify_delay_seconds=_read_int(
            "CPA_ENABLE_VERIFY_DELAY_SECONDS",
            DEFAULT_ENABLE_VERIFY_DELAY_SECONDS,
            env_values,
            minimum=1,
        ),
        enable_verify_max_attempts=_read_int(
            "CPA_ENABLE_VERIFY_MAX_ATTEMPTS",
            DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS,
            env_values,
            minimum=1,
        ),
```

Document the new env vars in `.env.example` near the other maintainer timing settings:

```dotenv
# Delay before re-checking whether a just-enabled account is really enabled / 启用后再次确认账号状态前的等待时间（秒）
CPA_ENABLE_VERIFY_DELAY_SECONDS=5

# Maximum attempts for enable -> wait -> verify in maintainer auto-enable flow / 自动重新启用时“启用 -> 等待 -> 复查”的最大尝试次数
CPA_ENABLE_VERIFY_MAX_ATTEMPTS=3
```

- [ ] **Step 4: Run the settings tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_settings.py -k enable_verify -v
```

Expected: all four new tests PASS.

- [ ] **Step 5: Commit the settings work**

```powershell
git add -- "src/settings.py" ".env.example" "tests/test_settings.py"
git commit -m @'
增加启用状态复查配置项

为自动重新启用流程补充启用确认延迟与重试次数配置，并补齐设置测试。
'@
```

## Task 2: Implement maintainer-side enable verification

**Files:**
- Modify: `src/maintainer.py:160-175,752-778`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing maintainer tests**

Add these tests near the existing enable-path tests in `tests/test_maintainer.py`:

```python
    def test_process_token_retries_enable_until_verification_succeeds(self):
        self.maintainer.settings.enable_verify_delay_seconds = 5
        self.maintainer.settings.enable_verify_max_attempts = 3
        self.maintainer.get_token_detail = Mock(side_effect=[
            {
                "email": "a@example.com",
                "disabled": True,
                "access_token": "token",
                "refresh_token": "rt",
                "account_id": "acc",
                "expired": "2099-01-01T00:00:00Z",
            },
            {"name": "t-enable", "disabled": True},
            {"name": "t-enable", "disabled": False},
        ])
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
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

        with patch("src.maintainer.time.time", return_value=1000), patch("src.maintainer.time.sleep") as sleep_mock:
            result = self.maintainer.process_token({"name": "t-enable"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertEqual(self.maintainer.set_disabled_status.call_count, 2)
        sleep_mock.assert_has_calls([call(5), call(5)])
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-enable"))
        self.assertEqual(self.maintainer.stats.enabled, 1)

    def test_process_token_keeps_tracked_state_when_enable_verification_never_succeeds(self):
        self.maintainer.settings.enable_verify_delay_seconds = 5
        self.maintainer.settings.enable_verify_max_attempts = 3
        self.maintainer.get_token_detail = Mock(side_effect=[
            {
                "email": "a@example.com",
                "disabled": True,
                "access_token": "token",
                "refresh_token": "rt",
                "account_id": "acc",
                "expired": "2099-01-01T00:00:00Z",
            },
            {"name": "t-enable", "disabled": True},
            {"name": "t-enable", "disabled": True},
            {"name": "t-enable", "disabled": True},
        ])
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
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
            result = self.maintainer.process_token({"name": "t-enable"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertEqual(self.maintainer.set_disabled_status.call_count, 3)
        self.assertEqual(self.maintainer._get_tracked_next_check_at("t-enable"), 1000)
        self.assertEqual(self.maintainer.stats.enabled, 0)

    def test_process_token_retries_when_enable_verification_detail_fetch_fails(self):
        self.maintainer.settings.enable_verify_delay_seconds = 5
        self.maintainer.settings.enable_verify_max_attempts = 3
        self.maintainer.get_token_detail = Mock(side_effect=[
            {
                "email": "a@example.com",
                "disabled": True,
                "access_token": "token",
                "refresh_token": "rt",
                "account_id": "acc",
                "expired": "2099-01-01T00:00:00Z",
            },
            None,
            {"name": "t-enable", "disabled": False},
        ])
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
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
            result = self.maintainer.process_token({"name": "t-enable"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertEqual(self.maintainer.set_disabled_status.call_count, 2)
        self.assertEqual(self.maintainer.stats.enabled, 1)
```

- [ ] **Step 2: Run the maintainer tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "enable_verification or retries_enable" -v
```

Expected: failures because the current code removes tracked state after the first `set_disabled_status(..., False)` success.

- [ ] **Step 3: Add a focused maintainer helper and wire it into the enable path**

Add this helper to `src/maintainer.py` near `set_disabled_status()`:

```python
    def _enable_with_verification(self, name, logger):
        delay_seconds = self.settings.enable_verify_delay_seconds
        max_attempts = self.settings.enable_verify_max_attempts
        for attempt in range(1, max_attempts + 1):
            logger.log("INFO", f"第 {attempt}/{max_attempts} 次尝试启用账号", indent=1)
            if not self.set_disabled_status(name, disabled=False, logger=logger):
                logger.log("ERROR", "启用请求失败", indent=1)
                if attempt < max_attempts:
                    logger.log("WARN", "准备重试启用", indent=1)
                continue
            logger.log("INFO", f"等待 {delay_seconds} 秒后复查启用状态", indent=1)
            time.sleep(delay_seconds)
            token_detail = self.get_token_detail(name)
            if not token_detail:
                logger.log("ERROR", "复查账号详情失败", indent=1)
                if attempt < max_attempts:
                    logger.log("WARN", "无法确认启用状态，准备重试", indent=1)
                continue
            disabled = token_detail.get("disabled")
            if disabled is False:
                logger.log("ENABLE", "复查确认账号已启用", indent=1)
                return True
            logger.log("WARN", f"复查发现账号仍为禁用状态: disabled={disabled}", indent=1)
            if attempt < max_attempts:
                logger.log("WARN", "准备再次发送启用请求", indent=1)
        logger.log("ERROR", f"账号启用复查失败，已达到最大尝试次数 {max_attempts}", indent=1)
        return False
```

Replace the auto-enable branch inside `_apply_quota_policy(...)`:

```python
                if self._enable_with_verification(name, logger):
                    if self._remove_tracked_account(name):
                        logger.log("ENABLE", "账号已重新启用", indent=1)
                    else:
                        logger.log("ERROR", "账号已确认启用，但移除复查计划失败", indent=1)
                    self._inc_stat("enabled")
                    effective_disabled = False
                else:
                    logger.log("ERROR", "账号启用未确认成功，保留待下次处理状态", indent=1)
                return None, effective_disabled
```

Do not move tracked removal or stats increment into `_enable_with_verification()`.

- [ ] **Step 4: Run the maintainer tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "enable_verification or retries_enable or run_tracked_recheck_enables_due_token" -v
```

Expected: the new tests PASS, and the existing enable-path test still passes with the stronger semantics.

- [ ] **Step 5: Commit the maintainer work**

```powershell
git add -- "src/maintainer.py" "tests/test_maintainer.py"
git commit -m @'
为自动启用增加复查确认机制

启用后延迟复查账号状态，失败时保留待下次处理语义并补齐测试。
'@
```

## Task 3: Add batch-script enable verification

**Files:**
- Modify: `enable_all_codex.py:16-19,94-133`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Write the failing batch-script tests**

Add these tests to `tests/test_enable_all_codex.py`:

```python
    def test_enable_accounts_retries_until_verification_succeeds(self):
        client = Mock()
        client.set_disabled.side_effect = [True, True]
        client.get_auth_file.side_effect = [
            {"name": "token-a", "disabled": True},
            {"name": "token-a", "disabled": False},
        ]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep") as sleep_mock, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertEqual(client.get_auth_file.call_count, 2)
        sleep_mock.assert_has_calls([call(5), call(5)])
        self.assertIn("启用成功", stdout.getvalue())

    def test_enable_accounts_reports_manual_check_after_max_attempts(self):
        client = Mock()
        client.set_disabled.return_value = True
        client.get_auth_file.side_effect = [
            {"name": "token-a", "disabled": True},
            {"name": "token-a", "disabled": True},
            {"name": "token-a", "disabled": True},
            {"name": "token-b", "disabled": False},
        ]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep"), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.set_disabled.call_count, 4)
        self.assertIn("失败账号: token-a", stdout.getvalue())
        self.assertIn("经过 3 次启用确认仍失败，请人工检查", stdout.getvalue())
        self.assertIn("token-b", stdout.getvalue())

    def test_enable_accounts_retries_after_verification_fetch_failure(self):
        client = Mock()
        client.set_disabled.side_effect = [True, True]
        client.get_auth_file.side_effect = [None, {"name": "token-a", "disabled": False}]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep"), patch("sys.stdout", new_callable=io.StringIO):
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertEqual(client.get_auth_file.call_count, 2)
```

Update the existing simple success test so it patches `time.sleep` and `get_auth_file` consistently with the new semantics:

```python
    def test_enable_accounts_enables_disabled_codex_accounts(self):
        client = Mock()
        client.set_disabled.return_value = True
        client.get_auth_file.return_value = {"name": "token-a", "disabled": False}
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep") as sleep_mock, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        client.set_disabled.assert_called_once_with("token-a", False)
        client.get_auth_file.assert_called_once_with("token-a")
        sleep_mock.assert_called_once_with(5)
        self.assertIn("启用成功", stdout.getvalue())
```

- [ ] **Step 2: Run the batch-script tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_enable_all_codex.py -k enable_accounts -v
```

Expected: failures because `enable_accounts()` currently treats the first `set_disabled()` success as final.

- [ ] **Step 3: Add the script-local helper and route account enabling through it**

At the top of `enable_all_codex.py`, add:

```python
import time
```

```python
DEFAULT_ENABLE_VERIFY_DELAY_SECONDS = 5
DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS = 3
```

Add a helper above `enable_accounts()`:

```python
def enable_account_with_verification(
    client: CPAClient,
    name: str,
    *,
    delay_seconds: int = DEFAULT_ENABLE_VERIFY_DELAY_SECONDS,
    max_attempts: int = DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS,
) -> tuple[bool, str | None]:
    for attempt in range(1, max_attempts + 1):
        log("INFO", f"第 {attempt}/{max_attempts} 次尝试启用账号 {name}")
        if not client.set_disabled(name, False):
            log("ERROR", "启用请求失败")
            continue
        log("INFO", f"等待 {delay_seconds} 秒后复查启用状态")
        time.sleep(delay_seconds)
        token_detail = client.get_auth_file(name)
        if not token_detail:
            log("ERROR", "复查账号详情失败")
            continue
        disabled = token_detail.get("disabled")
        if disabled is False:
            log("OK", "启用成功")
            return True, None
        log("WARN", f"复查发现账号仍为禁用状态: disabled={disabled}")
    return False, f"经过 {max_attempts} 次启用确认仍失败，请人工检查"
```

Update `enable_accounts()` to use it:

```python
        log("INFO", "开始设置 disabled=false")
        ok, failure_reason = enable_account_with_verification(client, name)
        if ok:
            enabled += 1
        else:
            failures += 1
            failed_names.append(name)
            log("ERROR", failure_reason or "启用失败")
```

Keep the existing final summary and add an extra manual-check line:

```python
    if failed_names:
        log("ERROR", f"失败账号: {', '.join(failed_names)}")
        log(
            "ERROR",
            f"以下账号经过 {DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS} 次启用确认仍失败，请人工检查: {', '.join(failed_names)}",
        )
```

- [ ] **Step 4: Run the batch-script tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_enable_all_codex.py -k enable_accounts -v
```

Expected: the new retry/failure tests PASS, and the existing success/continue tests still pass under the stronger semantics.

- [ ] **Step 5: Commit the batch-script work**

```powershell
git add -- "enable_all_codex.py" "tests/test_enable_all_codex.py"
git commit -m @'
为批量启用脚本增加状态复查

批量启用后增加延迟确认与重试，并在最终失败时输出人工检查提示。
'@
```

## Task 4: Run focused verification and a final regression sweep

**Files:**
- Verify: `tests/test_settings.py`
- Verify: `tests/test_maintainer.py`
- Verify: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Run the three focused test files together**

Run:

```powershell
python -m pytest tests/test_settings.py tests/test_maintainer.py tests/test_enable_all_codex.py -v
```

Expected: all tests in those files PASS.

- [ ] **Step 2: Run a targeted grep-free smoke check of the new env keys in the repo**

Run:

```powershell
python -c "from src.settings import load_settings; print('settings import ok')"
```

Expected: prints `settings import ok`.

- [ ] **Step 3: Run the full test suite**

Run:

```powershell
python -m pytest -v
```

Expected: full suite PASS with no regressions in CLI/settings/maintainer/script behavior.

- [ ] **Step 4: Review the final diff before shipping**

Run:

```powershell
git diff -- src/settings.py .env.example src/maintainer.py enable_all_codex.py tests/test_settings.py tests/test_maintainer.py tests/test_enable_all_codex.py
```

Expected: diff only shows the new enable verification config, maintainer helper integration, script helper integration, and the corresponding tests/docs update.

- [ ] **Step 5: Create the final implementation commit**

```powershell
git add -- "src/settings.py" ".env.example" "src/maintainer.py" "enable_all_codex.py" "tests/test_settings.py" "tests/test_maintainer.py" "tests/test_enable_all_codex.py"
git commit -m @'
增加启用后的复查确认与重试

为自动重新启用和批量启用补上延迟复查闭环，避免接口成功但状态未落地时误判成功。
'@
```

## Self-review checklist

- Spec coverage:
  - Maintainer-only configurable delay/attempts: covered in Task 1 and Task 2.
  - Batch script hard-coded `5s/3 attempts`: covered in Task 3.
  - Maintainer keeps tracked state on final failure: covered in Task 2 tests and implementation step.
  - Batch script continues after failure and prints manual-check prompt: covered in Task 3 tests and implementation step.
  - Focused and full-suite verification: covered in Task 4.
- Placeholder scan:
  - No `TODO`/`TBD` markers.
  - Each code change step includes concrete code snippets.
  - Each verification step includes exact commands and expected outcomes.
- Type consistency:
  - `Settings.enable_verify_delay_seconds`
  - `Settings.enable_verify_max_attempts`
  - `CPACodexKeeper._enable_with_verification(name, logger)`
  - `enable_all_codex.enable_account_with_verification(client, name, *, delay_seconds=5, max_attempts=3)`


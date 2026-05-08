# Enable-all-codex helper script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal standalone script that connects to one CPA instance and directly enables every `type == "codex"` auth-file by setting `disabled=false`, with detailed logs.

**Architecture:** Keep the change isolated in a new root-level helper script and a focused test file. Reuse `CPAClient` from `src/cpa_client.py`, resolve config from built-in constants with prompt fallback, and process codex accounts sequentially with detailed stdout logging and a meaningful exit code.

**Tech Stack:** Python 3, unittest, unittest.mock, getpass, existing `src.cpa_client.CPAClient`

---

## File map

- `enable_all_codex.py`
  - New standalone helper script.
  - Owns built-in config defaults, prompt fallback, timestamped stdout logging, codex filtering, enable loop, and exit code.
- `tests/test_enable_all_codex.py`
  - New focused unittest file for helper behavior.
  - Mocks `CPAClient`, prompt input, and stdout capture.
- `src/cpa_client.py`
  - Read-only dependency reused by the helper.
  - No changes needed.

### Task 1: Add the helper skeleton and config resolution

**Files:**
- Create: `enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Write the failing config-resolution tests**

Create `tests/test_enable_all_codex.py` with this initial test module:

```python
import io
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import enable_all_codex


class EnableAllCodexTests(unittest.TestCase):
    def test_resolve_config_uses_built_in_values_without_prompting(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", "https://built-in.example.com"), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", "built-in-token"), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", "http://127.0.0.1:7890"), \
             patch("builtins.input") as input_mock, \
             patch("enable_all_codex.getpass") as getpass_mock:
            endpoint, token, proxy, sources = enable_all_codex.resolve_config()

        self.assertEqual(endpoint, "https://built-in.example.com")
        self.assertEqual(token, "built-in-token")
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertEqual(sources, {
            "endpoint": "built-in",
            "token": "built-in",
            "proxy": "built-in",
        })
        input_mock.assert_not_called()
        getpass_mock.assert_not_called()

    def test_resolve_config_prompts_for_missing_required_values(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", ""), \
             patch("builtins.input", side_effect=["https://prompt.example.com", "http://127.0.0.1:8888"]), \
             patch("enable_all_codex.getpass", return_value="prompt-token"):
            endpoint, token, proxy, sources = enable_all_codex.resolve_config()

        self.assertEqual(endpoint, "https://prompt.example.com")
        self.assertEqual(token, "prompt-token")
        self.assertEqual(proxy, "http://127.0.0.1:8888")
        self.assertEqual(sources, {
            "endpoint": "prompt",
            "token": "prompt",
            "proxy": "prompt",
        })
```

- [ ] **Step 2: Run the config tests to verify they fail correctly**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_resolve_config_uses_built_in_values_without_prompting tests.test_enable_all_codex.EnableAllCodexTests.test_resolve_config_prompts_for_missing_required_values
```

Expected before implementation:

- import or attribute errors because `enable_all_codex.py` and `resolve_config()` do not exist yet

- [ ] **Step 3: Write the minimal helper skeleton in `enable_all_codex.py`**

Create `enable_all_codex.py` with:

```python
from __future__ import annotations

from datetime import datetime
from getpass import getpass

from src.cpa_client import CPAClient

DEFAULT_CPA_ENDPOINT = ""
DEFAULT_CPA_TOKEN = ""
DEFAULT_CPA_PROXY = ""
DEFAULT_CPA_TIMEOUT = 30
DEFAULT_CPA_MAX_RETRIES = 2


def log(level: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}][{level}]: {message}")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def prompt_if_missing(label: str, current: str | None, *, secret: bool = False) -> tuple[str | None, str]:
    value = (current or "").strip()
    if value:
        return value, "built-in"
    if secret:
        entered = getpass(f"请输入 {label}: ").strip()
    else:
        entered = input(f"请输入 {label}: ").strip()
    return (entered or None), "prompt"


def resolve_config() -> tuple[str, str, str | None, dict[str, str]]:
    endpoint, endpoint_source = prompt_if_missing("CPA_ENDPOINT", DEFAULT_CPA_ENDPOINT)
    token, token_source = prompt_if_missing("CPA_TOKEN", DEFAULT_CPA_TOKEN, secret=True)
    proxy, proxy_source = prompt_if_missing("CPA_PROXY（可选）", DEFAULT_CPA_PROXY)
    if endpoint is None or token is None:
        raise ValueError("CPA_ENDPOINT and CPA_TOKEN are required")
    return endpoint, token, proxy, {
        "endpoint": endpoint_source,
        "token": token_source,
        "proxy": proxy_source,
    }


def main() -> int:
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the config tests again to verify they pass**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_resolve_config_uses_built_in_values_without_prompting tests.test_enable_all_codex.EnableAllCodexTests.test_resolve_config_prompts_for_missing_required_values
```

Expected after implementation:

```text
..
----------------------------------------------------------------------
Ran 2 tests in ...

OK
```

- [ ] **Step 5: Commit the helper skeleton**

Run:

```bash
git add enable_all_codex.py tests/test_enable_all_codex.py
git commit -m "feat: add codex enable helper skeleton"
```

### Task 2: Filter auth-files to codex accounts only

**Files:**
- Modify: `enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Write the failing codex-filter test**

Add this test to `EnableAllCodexTests`:

```python
    def test_fetch_codex_accounts_filters_non_codex_entries(self):
        client = unittest.mock.Mock()
        client.list_auth_files.return_value = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "oauth", "email": "b@example.com", "disabled": True},
            {"name": "token-c", "email": "c@example.com", "disabled": True},
        ]

        accounts, total = enable_all_codex.fetch_codex_accounts(client)

        self.assertEqual(total, 3)
        self.assertEqual(accounts, [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ])
```

Also update the import line at the top of `tests/test_enable_all_codex.py` to:

```python
from unittest.mock import Mock, patch
```

And change the test body line:

```python
        client = unittest.mock.Mock()
```

to:

```python
        client = Mock()
```

- [ ] **Step 2: Run the new filter test to verify it fails**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_fetch_codex_accounts_filters_non_codex_entries
```

Expected before implementation:

- failure because `fetch_codex_accounts()` does not exist yet

- [ ] **Step 3: Implement minimal codex filtering**

Add this function to `enable_all_codex.py` above `main()`:

```python
def fetch_codex_accounts(client: CPAClient) -> tuple[list[dict], int]:
    files = client.list_auth_files()
    if not isinstance(files, list):
        return [], 0
    accounts = [item for item in files if item.get("type") == "codex"]
    return accounts, len(files)
```

- [ ] **Step 4: Run the filter test again to verify it passes**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_fetch_codex_accounts_filters_non_codex_entries
```

Expected after implementation:

```text
.
----------------------------------------------------------------------
Ran 1 test in ...

OK
```

- [ ] **Step 5: Commit the codex filtering change**

Run:

```bash
git add enable_all_codex.py tests/test_enable_all_codex.py
git commit -m "feat: filter enable helper to codex accounts"
```

### Task 3: Enable disabled codex accounts and skip already-enabled accounts

**Files:**
- Modify: `enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Write the failing per-account behavior tests**

Add these tests to `EnableAllCodexTests`:

```python
    def test_enable_accounts_skips_already_enabled_codex_accounts(self):
        client = Mock()
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": False},
        ]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        client.set_disabled.assert_not_called()
        self.assertIn("已是启用状态，跳过", stdout.getvalue())

    def test_enable_accounts_enables_disabled_codex_accounts(self):
        client = Mock()
        client.set_disabled.return_value = True
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        client.set_disabled.assert_called_once_with("token-a", False)
        self.assertIn("启用成功", stdout.getvalue())
```

- [ ] **Step 2: Run the per-account tests to verify they fail**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_enable_accounts_skips_already_enabled_codex_accounts tests.test_enable_all_codex.EnableAllCodexTests.test_enable_accounts_enables_disabled_codex_accounts
```

Expected before implementation:

- failure because `enable_accounts()` does not exist yet

- [ ] **Step 3: Implement the minimal sequential enable loop**

Add this function to `enable_all_codex.py` above `main()`:

```python
def enable_accounts(client: CPAClient, accounts: list[dict]) -> int:
    failures = 0
    already_enabled = 0
    enabled = 0
    skipped_invalid = 0

    for idx, account in enumerate(accounts, 1):
        name = (account.get("name") or "").strip()
        email = (account.get("email") or "unknown").strip() or "unknown"
        disabled = account.get("disabled")

        log("INFO", f"[{idx}/{len(accounts)}] 账号 name={name or 'unknown'} email={email} disabled={disabled}")

        if not name:
            skipped_invalid += 1
            failures += 1
            log("WARN", "缺少账号 name，跳过")
            continue

        if disabled is False:
            already_enabled += 1
            log("INFO", "已是启用状态，跳过")
            continue

        log("INFO", "开始设置 disabled=false")
        if client.set_disabled(name, False):
            enabled += 1
            log("OK", "启用成功")
        else:
            failures += 1
            log("ERROR", "启用失败")

    log("INFO", f"汇总: 总处理={len(accounts)} 已启用={already_enabled} 成功启用={enabled} 失败={failures} 无效={skipped_invalid}")
    return 1 if failures else 0
```

- [ ] **Step 4: Run the per-account tests again to verify they pass**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_enable_accounts_skips_already_enabled_codex_accounts tests.test_enable_all_codex.EnableAllCodexTests.test_enable_accounts_enables_disabled_codex_accounts
```

Expected after implementation:

```text
..
----------------------------------------------------------------------
Ran 2 tests in ...

OK
```

- [ ] **Step 5: Commit the enable-loop change**

Run:

```bash
git add enable_all_codex.py tests/test_enable_all_codex.py
git commit -m "feat: enable disabled codex accounts"
```

### Task 4: Add startup logging and masked token output

**Files:**
- Modify: `enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Write the failing token-mask and startup-log tests**

Add these tests to `EnableAllCodexTests`:

```python
    def test_mask_secret_hides_plaintext_token(self):
        masked = enable_all_codex.mask_secret("1234567890abcdef")

        self.assertNotEqual(masked, "1234567890abcdef")
        self.assertEqual(masked, "1234***cdef")

    @patch("enable_all_codex.enable_accounts", return_value=0)
    @patch("enable_all_codex.fetch_codex_accounts", return_value=([], 0))
    @patch("enable_all_codex.CPAClient")
    def test_main_logs_masked_token_and_sources(self, client_cls, _fetch_mock, _enable_mock):
        with patch("enable_all_codex.resolve_config", return_value=(
            "https://example.com",
            "1234567890abcdef",
            "http://127.0.0.1:7890",
            {"endpoint": "built-in", "token": "built-in", "proxy": "prompt"},
        )), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("目标 CPA: https://example.com", stdout.getvalue())
        self.assertIn("Token: 1234***cdef", stdout.getvalue())
        self.assertIn("endpoint 来源: built-in", stdout.getvalue())
        self.assertIn("token 来源: built-in", stdout.getvalue())
        self.assertIn("proxy 来源: prompt", stdout.getvalue())
        self.assertNotIn("1234567890abcdef", stdout.getvalue())
        client_cls.assert_called_once_with(
            "https://example.com",
            "1234567890abcdef",
            proxy="http://127.0.0.1:7890",
            timeout=enable_all_codex.DEFAULT_CPA_TIMEOUT,
            max_retries=enable_all_codex.DEFAULT_CPA_MAX_RETRIES,
        )
```

- [ ] **Step 2: Run the startup-log tests to verify they fail**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_mask_secret_hides_plaintext_token tests.test_enable_all_codex.EnableAllCodexTests.test_main_logs_masked_token_and_sources
```

Expected before implementation:

- `test_main_logs_masked_token_and_sources` fails because `main()` does not yet construct `CPAClient` or emit startup logs

- [ ] **Step 3: Implement the minimal startup logging and client construction**

Replace `main()` in `enable_all_codex.py` with:

```python
def main() -> int:
    try:
        endpoint, token, proxy, sources = resolve_config()
    except ValueError as exc:
        log("ERROR", str(exc))
        return 1

    log("INFO", "启动 codex 批量启用脚本")
    log("INFO", f"目标 CPA: {endpoint}")
    log("INFO", f"Proxy: {proxy or '未设置'}")
    log("INFO", f"Token: {mask_secret(token)}")
    log("INFO", f"endpoint 来源: {sources['endpoint']}")
    log("INFO", f"token 来源: {sources['token']}")
    log("INFO", f"proxy 来源: {sources['proxy']}")

    client = CPAClient(
        endpoint,
        token,
        proxy=proxy,
        timeout=DEFAULT_CPA_TIMEOUT,
        max_retries=DEFAULT_CPA_MAX_RETRIES,
    )
    accounts, total = fetch_codex_accounts(client)
    log("INFO", f"auth-files 总数: {total}")
    log("INFO", f"codex 账号数: {len(accounts)}")
    if total == 0:
        log("INFO", "未获取到任何 auth-file，结束")
        return 0
    if not accounts:
        log("INFO", "未找到任何 codex 账号，结束")
        return 0
    return enable_accounts(client, accounts)
```

- [ ] **Step 4: Run the startup-log tests again to verify they pass**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_mask_secret_hides_plaintext_token tests.test_enable_all_codex.EnableAllCodexTests.test_main_logs_masked_token_and_sources
```

Expected after implementation:

```text
..
----------------------------------------------------------------------
Ran 2 tests in ...

OK
```

- [ ] **Step 5: Commit the startup logging change**

Run:

```bash
git add enable_all_codex.py tests/test_enable_all_codex.py
git commit -m "feat: add startup logging to codex enable helper"
```

### Task 5: Return non-zero on per-account failures and continue processing

**Files:**
- Modify: `enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Write the failing continuation and exit-code tests**

Add these tests to `EnableAllCodexTests`:

```python
    def test_enable_accounts_continues_after_one_failure(self):
        client = Mock()
        client.set_disabled.side_effect = [False, True]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertIn("token-a", stdout.getvalue())
        self.assertIn("token-b", stdout.getvalue())
        self.assertIn("启用失败", stdout.getvalue())
        self.assertIn("启用成功", stdout.getvalue())

    @patch("enable_all_codex.fetch_codex_accounts", return_value=(
        [{"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True}],
        1,
    ))
    @patch("enable_all_codex.CPAClient")
    def test_main_returns_non_zero_when_any_enable_fails(self, client_cls, _fetch_mock):
        client = client_cls.return_value
        client.set_disabled.return_value = False

        with patch("enable_all_codex.resolve_config", return_value=(
            "https://example.com",
            "1234567890abcdef",
            None,
            {"endpoint": "built-in", "token": "built-in", "proxy": "built-in"},
        )):
            exit_code = enable_all_codex.main()

        self.assertEqual(exit_code, 1)
```

- [ ] **Step 2: Run the continuation tests to verify they fail if behavior regresses**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_enable_accounts_continues_after_one_failure tests.test_enable_all_codex.EnableAllCodexTests.test_main_returns_non_zero_when_any_enable_fails
```

Expected:

- if the implementation is correct, these tests already pass immediately
- if either test fails, fix `enable_accounts()` or `main()` before continuing

- [ ] **Step 3: Ensure failure details are included in the final summary**

Update `enable_accounts()` in `enable_all_codex.py` to track failed names and include them in the summary block:

```python
def enable_accounts(client: CPAClient, accounts: list[dict]) -> int:
    failures = 0
    already_enabled = 0
    enabled = 0
    skipped_invalid = 0
    failed_names: list[str] = []

    for idx, account in enumerate(accounts, 1):
        name = (account.get("name") or "").strip()
        email = (account.get("email") or "unknown").strip() or "unknown"
        disabled = account.get("disabled")

        log("INFO", f"[{idx}/{len(accounts)}] 账号 name={name or 'unknown'} email={email} disabled={disabled}")

        if not name:
            skipped_invalid += 1
            failures += 1
            failed_names.append("<missing-name>")
            log("WARN", "缺少账号 name，跳过")
            continue

        if disabled is False:
            already_enabled += 1
            log("INFO", "已是启用状态，跳过")
            continue

        log("INFO", "开始设置 disabled=false")
        if client.set_disabled(name, False):
            enabled += 1
            log("OK", "启用成功")
        else:
            failures += 1
            failed_names.append(name)
            log("ERROR", "启用失败")

    log("INFO", f"汇总: 总处理={len(accounts)} 已启用={already_enabled} 尝试启用={enabled + failures - skipped_invalid} 成功启用={enabled} 失败={failures} 无效={skipped_invalid}")
    if failed_names:
        log("ERROR", f"失败账号: {', '.join(failed_names)}")
    return 1 if failures else 0
```

- [ ] **Step 4: Run the continuation tests again to verify they pass cleanly**

Run:

```bash
python -m unittest tests.test_enable_all_codex.EnableAllCodexTests.test_enable_accounts_continues_after_one_failure tests.test_enable_all_codex.EnableAllCodexTests.test_main_returns_non_zero_when_any_enable_fails
```

Expected after implementation:

```text
..
----------------------------------------------------------------------
Ran 2 tests in ...

OK
```

- [ ] **Step 5: Commit the failure-summary change**

Run:

```bash
git add enable_all_codex.py tests/test_enable_all_codex.py
git commit -m "feat: report enable helper failures"
```

### Task 6: Run the final focused verification suite

**Files:**
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Run the full helper test file**

Run:

```bash
python -m unittest tests.test_enable_all_codex -v
```

Expected:

- all helper tests pass
- no real network calls occur

- [ ] **Step 2: Run a small regression check against the existing CLI tests**

Run:

```bash
python -m unittest tests.test_cli -v
```

Expected:

- all `CLITests` still pass
- no existing startup behavior regresses

- [ ] **Step 3: Commit the finished helper**

Run:

```bash
git add enable_all_codex.py tests/test_enable_all_codex.py
git commit -m "feat: add standalone codex enable script"
```

## Self-review

- Spec coverage checked:
  - standalone root-level script: covered by Tasks 1-6
  - built-in config with prompt fallback: covered by Task 1
  - only `type == "codex"`: covered by Task 2
  - direct `disabled=false` enable behavior: covered by Task 3
  - detailed startup/per-account/final logs with masked token: covered by Tasks 3-5
  - non-zero exit on failures: covered by Task 5
- Placeholder scan complete: no `TODO`, `TBD`, or deferred implementation steps remain
- Type consistency checked:
  - helper function names are consistent across all tasks
  - `main() -> int`, `fetch_codex_accounts(client)`, and `enable_accounts(client, accounts)` are used consistently

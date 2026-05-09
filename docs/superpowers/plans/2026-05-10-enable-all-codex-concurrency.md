# Enable All Codex Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `enable_all_codex.py` from serial enable verification into bounded concurrent execution so batch enable runs much faster without changing per-account retry and failure semantics.

**Architecture:** Keep `CPAClient` synchronous and leave `src/maintainer.py` untouched. Add a small per-account result boundary plus buffered per-account logging in `enable_all_codex.py`, then use `ThreadPoolExecutor(max_workers=8)` to run only disabled codex accounts concurrently while the main thread prints each account's logs as one contiguous block and performs the final summary.

**Tech Stack:** Python 3.12, `concurrent.futures`, `dataclasses`, `unittest`, `unittest.mock`, existing `CPAClient`.

---

## File map

- Modify: `enable_all_codex.py`
  - Add concurrency constant, per-account result type, buffered logging helpers, thread-pool orchestration, and main-thread log flushing.
- Modify: `tests/test_enable_all_codex.py`
  - Update unit tests to validate concurrent orchestration, per-account log block behavior, retry semantics, and summary semantics using mocked futures/executor behavior.

## Task 1: Write failing tests for concurrent orchestration

**Files:**
- Modify: `tests/test_enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Add failing tests for concurrent account processing and log block output**

Add these helpers and tests near the existing `enable_accounts` tests in `tests/test_enable_all_codex.py`:

```python
class FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class FakeExecutor:
    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.submitted = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, *args, **kwargs):
        future = FakeFuture(fn(*args, **kwargs))
        self.submitted.append((fn, args, kwargs, future))
        return future
```

```python
    @patch("enable_all_codex.as_completed", side_effect=lambda futures: list(futures))
    def test_enable_accounts_processes_disabled_accounts_with_thread_pool(self, _as_completed_mock):
        client = Mock()
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]
        fake_executor = FakeExecutor(enable_all_codex.DEFAULT_ENABLE_CONCURRENCY)

        with patch("enable_all_codex.ThreadPoolExecutor", side_effect=lambda max_workers: fake_executor), \
             patch("enable_all_codex.process_account", side_effect=[
                 enable_all_codex.AccountProcessResult(
                     name="token-a",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["[1/2] token-a", "token-a ok"],
                 ),
                 enable_all_codex.AccountProcessResult(
                     name="token-b",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["[2/2] token-b", "token-b ok"],
                 ),
             ]) as process_mock, \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_executor.max_workers, enable_all_codex.DEFAULT_ENABLE_CONCURRENCY)
        self.assertEqual(len(fake_executor.submitted), 2)
        self.assertEqual(process_mock.call_count, 2)
        self.assertIn("token-a ok", stdout.getvalue())
        self.assertIn("token-b ok", stdout.getvalue())
```

```python
    @patch("enable_all_codex.as_completed", side_effect=lambda futures: list(reversed(list(futures))))
    def test_enable_accounts_prints_each_account_logs_as_a_block(self, _as_completed_mock):
        client = Mock()
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]
        fake_executor = FakeExecutor(enable_all_codex.DEFAULT_ENABLE_CONCURRENCY)

        with patch("enable_all_codex.ThreadPoolExecutor", side_effect=lambda max_workers: fake_executor), \
             patch("enable_all_codex.process_account", side_effect=[
                 enable_all_codex.AccountProcessResult(
                     name="token-a",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["A-1", "A-2"],
                 ),
                 enable_all_codex.AccountProcessResult(
                     name="token-b",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["B-1", "B-2"],
                 ),
             ]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            enable_all_codex.enable_accounts(client, accounts)

        lines = [line for line in stdout.getvalue().splitlines() if line.endswith(("A-1", "A-2", "B-1", "B-2"))]
        self.assertEqual(lines, [
            "[" + lines[0].split("][", 1)[1],
        ])
```

Replace the final assertion above with an order assertion based on suffixes so the test verifies block output instead of timestamp text:

```python
        suffixes = [line.rsplit(": ", 1)[1] for line in stdout.getvalue().splitlines() if line.rsplit(": ", 1)[-1] in {"A-1", "A-2", "B-1", "B-2"}]
        self.assertEqual(suffixes, ["B-1", "B-2", "A-1", "A-2"])
```

```python
    def test_process_account_returns_failure_result_for_missing_name(self):
        client = Mock()

        result = enable_all_codex.process_account(client, {"email": "missing@example.com", "disabled": True}, 3, 10)

        self.assertFalse(result.success)
        self.assertTrue(result.invalid)
        self.assertEqual(result.name, "<missing-name>")
        self.assertEqual(result.failure_reason, "缺少账号 name")
        self.assertTrue(any("缺少账号 name，跳过" in line for line in result.log_lines))
```

```python
    def test_process_account_returns_already_enabled_result_without_submitting_enable(self):
        client = Mock()

        result = enable_all_codex.process_account(
            client,
            {"name": "token-a", "email": "a@example.com", "disabled": False},
            1,
            5,
        )

        self.assertTrue(result.success)
        self.assertTrue(result.already_enabled)
        client.set_disabled.assert_not_called()
        self.assertTrue(any("已是启用状态，跳过" in line for line in result.log_lines))
```

- [ ] **Step 2: Run the focused enable-all tests and confirm failure**

Run:

```powershell
python -m pytest tests/test_enable_all_codex.py -v
```

Expected: FAIL with `AttributeError` / `NameError` because `AccountProcessResult`, `process_account`, `ThreadPoolExecutor`, and `as_completed` integration do not exist yet.

- [ ] **Step 3: Commit only if the failing tests are in place and verified**

Do not commit yet. Move directly to Task 2 once the failures are confirmed.

## Task 2: Implement concurrent orchestration with buffered account logs

**Files:**
- Modify: `enable_all_codex.py`
- Test: `tests/test_enable_all_codex.py`

- [ ] **Step 1: Add concurrency imports, constants, and result dataclass**

At the top of `enable_all_codex.py`, add the imports and types:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
```

Add the concurrency constant near the existing enable verification constants:

```python
DEFAULT_ENABLE_CONCURRENCY = 8
```

Add the result type below `fetch_codex_accounts()`:

```python
@dataclass(slots=True)
class AccountProcessResult:
    name: str
    success: bool
    already_enabled: bool
    invalid: bool
    failure_reason: str | None
    log_lines: list[str]
```

- [ ] **Step 2: Refactor logging so account work can buffer lines instead of printing immediately**

Keep the existing top-level `log()` function for global script logs. Add a buffered account logger helper below it:

```python
def append_account_log(log_lines: list[str], level: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_lines.append(f"[{timestamp}][{level}]: {message}")
```

Add a helper that flushes one completed block to stdout from the main thread:

```python
def flush_account_logs(log_lines: list[str]) -> None:
    for line in log_lines:
        print(line)
```

- [ ] **Step 3: Refactor single-account verification to accept buffered logging**

Change `enable_account_with_verification()` to accept `log_lines` and replace direct `log(...)` calls with `append_account_log(...)`:

```python
def enable_account_with_verification(
    client: CPAClient,
    name: str,
    *,
    delay_seconds: int = DEFAULT_ENABLE_VERIFY_DELAY_SECONDS,
    max_attempts: int = DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS,
    log_lines: list[str],
) -> tuple[bool, str | None]:
    for attempt in range(1, max_attempts + 1):
        append_account_log(log_lines, "INFO", f"第 {attempt}/{max_attempts} 次尝试启用账号 {name}")
        if not client.set_disabled(name, False):
            append_account_log(log_lines, "ERROR", "启用请求失败")
            continue
        append_account_log(log_lines, "INFO", f"等待 {delay_seconds} 秒后复查启用状态")
        time.sleep(delay_seconds)
        token_detail = client.get_auth_file(name)
        if not token_detail:
            append_account_log(log_lines, "ERROR", "复查账号详情失败")
            continue
        disabled = token_detail.get("disabled")
        if disabled is False:
            append_account_log(log_lines, "OK", "启用成功")
            return True, None
        append_account_log(log_lines, "WARN", f"复查发现账号仍为禁用状态: disabled={disabled}")
    return False, f"经过 {max_attempts} 次启用确认仍失败，请人工检查"
```

- [ ] **Step 4: Add `process_account()` for one complete account result**

Insert this function above `enable_accounts()`:

```python
def process_account(client: CPAClient, account: dict, idx: int, total: int) -> AccountProcessResult:
    log_lines: list[str] = []
    raw_name = (account.get("name") or "").strip()
    email = (account.get("email") or "unknown").strip() or "unknown"
    disabled = account.get("disabled")
    display_name = raw_name or "unknown"

    append_account_log(
        log_lines,
        "INFO",
        f"[{idx}/{total}] 账号 name={display_name} email={email} disabled={disabled}",
    )

    if not raw_name:
        append_account_log(log_lines, "WARN", "缺少账号 name，跳过")
        return AccountProcessResult(
            name="<missing-name>",
            success=False,
            already_enabled=False,
            invalid=True,
            failure_reason="缺少账号 name",
            log_lines=log_lines,
        )

    if disabled is False:
        append_account_log(log_lines, "INFO", "已是启用状态，跳过")
        return AccountProcessResult(
            name=raw_name,
            success=True,
            already_enabled=True,
            invalid=False,
            failure_reason=None,
            log_lines=log_lines,
        )

    append_account_log(log_lines, "INFO", "开始设置 disabled=false")
    ok, failure_reason = enable_account_with_verification(client, raw_name, log_lines=log_lines)
    if not ok:
        append_account_log(log_lines, "ERROR", failure_reason or "启用失败")
    return AccountProcessResult(
        name=raw_name,
        success=ok,
        already_enabled=False,
        invalid=False,
        failure_reason=failure_reason,
        log_lines=log_lines,
    )
```

- [ ] **Step 5: Rewrite `enable_accounts()` to use a bounded thread pool**

Replace the existing serial loop with this structure:

```python
def enable_accounts(client: CPAClient, accounts: list[dict]) -> int:
    failures = 0
    already_enabled = 0
    enabled = 0
    skipped_invalid = 0
    failed_names: list[str] = []

    futures: dict = {}
    with ThreadPoolExecutor(max_workers=DEFAULT_ENABLE_CONCURRENCY) as executor:
        for idx, account in enumerate(accounts, 1):
            future = executor.submit(process_account, client, account, idx, len(accounts))
            futures[future] = account

        for future in as_completed(futures):
            result = future.result()
            flush_account_logs(result.log_lines)
            if result.invalid:
                skipped_invalid += 1
                failures += 1
                failed_names.append(result.name)
                continue
            if result.already_enabled:
                already_enabled += 1
                continue
            if result.success:
                enabled += 1
                continue
            failures += 1
            failed_names.append(result.name)

    attempted = enabled + failures - skipped_invalid
    log("INFO", f"汇总: 总处理={len(accounts)} 已启用={already_enabled} 尝试启用={attempted} 成功启用={enabled} 失败={failures} 无效={skipped_invalid}")
    if failed_names:
        log("ERROR", f"失败账号: {', '.join(failed_names)}")
        log(
            "ERROR",
            f"以下账号经过 {DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS} 次启用确认仍失败，请人工检查: {', '.join(failed_names)}",
        )
    return 1 if failures else 0
```

This keeps summary semantics unchanged while parallelizing every account and printing each account block only after completion.

- [ ] **Step 6: Update the existing tests to match the new buffered logging signature**

Adjust the existing tests that call `enable_accounts()` so they continue to assert retry semantics, but do not rely on serial `stdout` timing. Use patterns like:

```python
        self.assertIn("启用成功", stdout.getvalue())
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertEqual(client.get_auth_file.call_count, 2)
```

For the new block-output test, keep the suffix extraction assertion:

```python
        suffixes = [
            line.rsplit(": ", 1)[1]
            for line in stdout.getvalue().splitlines()
            if line.rsplit(": ", 1)[-1] in {"A-1", "A-2", "B-1", "B-2"}
        ]
        self.assertEqual(suffixes, ["B-1", "B-2", "A-1", "A-2"])
```

- [ ] **Step 7: Run the focused tests and confirm they pass**

Run:

```powershell
python -m pytest tests/test_enable_all_codex.py -v
```

Expected: PASS for all `enable_all_codex` tests.

- [ ] **Step 8: Commit the concurrent batch-enable implementation**

```powershell
git add -- "enable_all_codex.py" "tests/test_enable_all_codex.py"
git commit -m @'
改为并发批量启用 codex 账号

使用线程池并发执行启用后复查流程，并按账号整块输出日志以提升批量启用速度。
'@
```

## Task 3: Verify the branch-level impact and finalize

**Files:**
- Modify: `enable_all_codex.py`
- Modify: `tests/test_enable_all_codex.py`
- Verify: repository test suite and git diff

- [ ] **Step 1: Run the targeted regression set**

Run:

```powershell
python -m pytest tests/test_enable_all_codex.py tests/test_maintainer.py tests/test_settings.py -v
```

Expected: PASS. This confirms the batch script change did not regress the recently added enable verification behavior elsewhere.

- [ ] **Step 2: Run the full test suite**

Run:

```powershell
python -m pytest -v
```

Expected: PASS for the full suite.

- [ ] **Step 3: Review the final diff**

Run:

```powershell
git diff -- enable_all_codex.py tests/test_enable_all_codex.py
```

Expected: only the concurrent orchestration, buffered logging, and related test updates appear.

- [ ] **Step 4: If Task 2 commit was used, create a final verification commit only when necessary**

Normally no extra commit is needed here. Only create another commit if verification uncovered and required a follow-up fix.

## Self-review checklist

- Spec coverage: concurrent account execution, fixed concurrency of 8, per-account buffered logs, unchanged retry/failure semantics, and stable mocked-concurrency testing are all covered by Tasks 1-3.
- Placeholder scan: no TBD/TODO markers remain; every code step contains concrete code or commands.
- Type consistency: `AccountProcessResult`, `process_account`, `append_account_log`, and `flush_account_logs` are named consistently across tasks.

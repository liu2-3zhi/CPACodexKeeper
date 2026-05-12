# Tracked Recheck Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen tracked recheck so disabled accounts are still rediscovered and retried for enablement even if a timer misfires, while also normalizing maintainer state files so each account appears only once with the latest record last.

**Architecture:** Keep the existing `threading.Timer` path as the primary trigger, then add a periodic due-account compensation scan in both daemon and monitor loops as a secondary trigger. In parallel, normalize `disabled_accounts.json` and `delete_blocked_accounts.json` during load and write so both files enforce “single account entry + newest record at the bottom”, with lightweight in-process de-duplication to avoid running the same tracked recheck twice.

**Tech Stack:** Python 3.12, `threading`, `json`, `unittest`, `unittest.mock`, pytest, existing `CPACodexKeeper` / `ConsoleLogger` infrastructure.

---

## File map

- Modify: `src/maintainer.py`
  - Add tracked-recheck in-flight guards, due-account compensation scanning, normalization helpers for tracked state and delete-blocked history, and integration points in daemon/monitor loops.
- Modify: `tests/test_maintainer.py`
  - Add failing-first tests for due-account compensation, duplicate suppression, tracked/delete-blocked normalization, and loop integration logging.
- Create: `docs/superpowers/plans/2026-05-12-tracked-recheck-hardening.md`
  - This implementation plan. Keep on `new`; do not merge into `main`.

## Task 1: Normalize `disabled_accounts.json` on load and write

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tracked-state normalization tests**

Add these tests near the existing disabled-state load/save tests in `tests/test_maintainer.py`:

```python
    def test_load_disabled_accounts_state_keeps_latest_duplicate_entry(self):
        state_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        state_path.write_text(
            '{"token-a": {"next_check_at": 1000}, "token-b": {"next_check_at": 1100}, "token-a": {"next_check_at": 1200}}',
            encoding="utf-8",
        )
        self.maintainer.disabled_accounts_path = state_path

        loaded = self.maintainer._load_disabled_accounts_state()

        self.assertEqual(loaded, {
            "token-b": {"next_check_at": 1100},
            "token-a": {"next_check_at": 1200},
        })
```

```python
    @patch("src.maintainer.threading.Timer")
    def test_set_tracked_next_check_at_moves_existing_account_to_end(self, timer_cls):
        state_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        state_path.write_text(
            json.dumps(
                {
                    "token-a": {"next_check_at": 1000},
                    "token-b": {"next_check_at": 1100},
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        self.maintainer.disabled_accounts_path = state_path
        self.maintainer._tracked_disabled_accounts = {
            "token-a": {"next_check_at": 1000},
            "token-b": {"next_check_at": 1100},
        }

        with patch("src.maintainer.time.time", return_value=1000):
            self.maintainer._set_tracked_next_check_at("token-a", 1300)

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(list(payload.keys()), ["token-b", "token-a"])
        self.assertEqual(payload["token-a"], {"next_check_at": 1300})
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "load_disabled_accounts_state_keeps_latest_duplicate_entry or set_tracked_next_check_at_moves_existing_account_to_end" -v
```

Expected: FAIL because `_load_disabled_accounts_state()` and `_set_tracked_next_check_at()` do not yet normalize duplicate tracked entries or preserve the “newest at end” ordering explicitly.

- [ ] **Step 3: Add tracked-state normalization helpers in `src/maintainer.py`**

Add a helper below `_existing_state_path(...)` to normalize tracked state dictionaries while preserving “latest wins, latest at end” semantics:

```python
    def _normalize_tracked_disabled_accounts_state(self, data):
        if not isinstance(data, dict):
            return {}
        normalized = {}
        for name, entry in data.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            next_check_at = entry.get("next_check_at")
            if not isinstance(next_check_at, int):
                continue
            normalized.pop(name, None)
            normalized[name] = {"next_check_at": next_check_at}
        return normalized
```

Update `_load_disabled_accounts_state()` to use the helper and write back normalized content when the raw file differs:

```python
    def _load_disabled_accounts_state(self):
        target_path = self._existing_state_path(self.disabled_accounts_path, self.legacy_disabled_accounts_path)
        if not target_path.exists():
            return {}
        try:
            data = self._read_json_file(target_path)
        except (OSError, json.JSONDecodeError) as exc:
            self.log("ERROR", f"加载禁用账号计划失败：{exc}")
            return {}
        normalized = self._normalize_tracked_disabled_accounts_state(data)
        if data != normalized:
            try:
                self._save_disabled_accounts_state(normalized)
                self.log("INFO", "disabled_accounts.json 检测到重复账号记录，已按最新记录规范化")
            except OSError as exc:
                self.log("ERROR", f"disabled_accounts.json 规范化写回失败：{exc}")
        return normalized
```

Add a small helper for “remove old then append new” semantics and use it in `_set_tracked_next_check_at(...)`:

```python
    def _upsert_tracked_account_state(self, state, name, next_check_at):
        state.pop(name, None)
        state[name] = {"next_check_at": int(next_check_at)}
```

Then update `_set_tracked_next_check_at(...)`:

```python
        success = self._locked_update_tracked_disabled_accounts(
            f"记录 {name} 的下次复查时间",
            lambda state: self._upsert_tracked_account_state(state, name, ts_int),
        )
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "load_disabled_accounts_state_keeps_latest_duplicate_entry or set_tracked_next_check_at_moves_existing_account_to_end" -v
```

Expected: PASS.

- [ ] **Step 5: Commit the tracked-state normalization work**

```powershell
git add -- "src/maintainer.py" "tests/test_maintainer.py"
git commit -m @'
规范化禁用账号复查状态文件

清理重复账号记录并确保最新复查计划总是写在文件末尾。
'@
```

## Task 2: Normalize `delete_blocked_accounts.json` so each account keeps only its latest event

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing delete-blocked history tests**

Add these tests near the existing delete-blocked history tests in `tests/test_maintainer.py`:

```python
    def test_load_delete_blocked_history_keeps_latest_duplicate_account_event(self):
        self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete_blocked_accounts.json"
        self.maintainer.delete_blocked_accounts_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "name": "token-a",
                            "reason": "old reason",
                            "source_action": "delete",
                            "trigger": "quota_without_refresh_token",
                            "updated_at": "2026-05-10 10:00:00",
                        },
                        {
                            "name": "token-b",
                            "reason": "middle reason",
                            "source_action": "delete",
                            "trigger": "401_or_402",
                            "updated_at": "2026-05-10 11:00:00",
                        },
                        {
                            "name": "token-a",
                            "reason": "new reason",
                            "source_action": "delete",
                            "trigger": "expired_without_refresh_token",
                            "updated_at": "2026-05-10 12:00:00",
                        },
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        payload = self.maintainer._load_delete_blocked_history()

        self.assertEqual(
            [event["name"] for event in payload["events"]],
            ["token-b", "token-a"],
        )
        self.assertEqual(payload["events"][-1]["reason"], "new reason")
```

```python
    def test_append_delete_blocked_event_replaces_existing_account_and_moves_it_to_end(self):
        self.maintainer.delete_blocked_accounts_path = pathlib.Path(self.temp_dir.name) / "delete_blocked_accounts.json"
        self.maintainer.delete_blocked_accounts_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "name": "token-a",
                            "reason": "old reason",
                            "source_action": "delete",
                            "trigger": "quota_without_refresh_token",
                            "updated_at": "2026-05-10 10:00:00",
                        },
                        {
                            "name": "token-b",
                            "reason": "middle reason",
                            "source_action": "delete",
                            "trigger": "401_or_402",
                            "updated_at": "2026-05-10 11:00:00",
                        },
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        self.maintainer._append_delete_blocked_event(
            name="token-a",
            reason="new reason",
            trigger="expired_without_refresh_token",
        )

        payload = json.loads(self.maintainer.delete_blocked_accounts_path.read_text(encoding="utf-8"))
        self.assertEqual([event["name"] for event in payload["events"]], ["token-b", "token-a"])
        self.assertEqual(payload["events"][-1]["reason"], "new reason")
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "load_delete_blocked_history_keeps_latest_duplicate_account_event or append_delete_blocked_event_replaces_existing_account_and_moves_it_to_end" -v
```

Expected: FAIL because delete-blocked history currently preserves duplicates instead of replacing old account events.

- [ ] **Step 3: Add delete-blocked normalization helpers and minimal implementation**

Add a helper below `_load_delete_blocked_history()`:

```python
    def _normalize_delete_blocked_events(self, events):
        if not isinstance(events, list):
            return []
        normalized = []
        for event in events:
            if not isinstance(event, dict):
                continue
            name = event.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            normalized = [existing for existing in normalized if existing.get("name") != name]
            normalized.append(dict(event))
        return normalized
```

Update `_load_delete_blocked_history()`:

```python
    def _load_delete_blocked_history(self):
        target_path = self._existing_state_path(
            self.delete_blocked_accounts_path,
            self.legacy_delete_blocked_accounts_path,
        )
        if not target_path.exists():
            return {"events": []}
        try:
            data = self._read_json_file(target_path)
        except (OSError, json.JSONDecodeError):
            return {"events": []}
        if not isinstance(data, dict):
            return {"events": []}
        events = self._normalize_delete_blocked_events(data.get("events"))
        normalized = {"events": events}
        if data.get("events") != events:
            try:
                self._save_delete_blocked_history(normalized)
                self.log("INFO", "delete_blocked_accounts.json 检测到重复账号记录，已按最新记录规范化")
            except OSError as exc:
                self.log("ERROR", f"delete_blocked_accounts.json 规范化写回失败: {exc}")
        return normalized
```

Update `_append_delete_blocked_event(...)` so it removes an existing account event before appending the new one:

```python
    def _append_delete_blocked_event(self, *, name, reason, trigger):
        with self._state_lock:
            payload = self._load_delete_blocked_history()
            payload["events"] = [event for event in payload["events"] if event.get("name") != name]
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

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "load_delete_blocked_history_keeps_latest_duplicate_account_event or append_delete_blocked_event_replaces_existing_account_and_moves_it_to_end" -v
```

Expected: PASS.

- [ ] **Step 5: Commit the delete-blocked normalization work**

```powershell
git add -- "src/maintainer.py" "tests/test_maintainer.py"
git commit -m @'
规范化删除阻断记录文件

确保同一账号在 delete_blocked_accounts.json 中只保留最新一条记录。
'@
```

## Task 3: Add in-process duplicate suppression for tracked rechecks

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing duplicate-suppression tests**

Add these tests near the existing tracked-recheck tests:

```python
    def test_run_tracked_recheck_skips_when_account_is_already_running(self):
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        self.maintainer._running_tracked_rechecks = {"t-enable"}
        self.maintainer.process_token = Mock(return_value="alive")
        self.maintainer.log = Mock()

        self.maintainer._run_tracked_recheck("t-enable")

        self.maintainer.process_token.assert_not_called()
        self.maintainer.log.assert_any_call("INFO", "账号 t-enable 已有复查任务在执行，跳过本次补偿触发")
```

```python
    def test_run_tracked_recheck_clears_running_marker_after_execution(self):
        state_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        state_path.write_text('{"t-enable": {"next_check_at": 1000}}', encoding="utf-8")
        self.maintainer.disabled_accounts_path = state_path
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        self.maintainer.process_token = Mock(return_value="alive")

        self.maintainer._run_tracked_recheck("t-enable")

        self.assertNotIn("t-enable", self.maintainer._running_tracked_rechecks)
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "run_tracked_recheck_skips_when_account_is_already_running or run_tracked_recheck_clears_running_marker_after_execution" -v
```

Expected: FAIL because the maintainer does not yet track in-flight tracked rechecks.

- [ ] **Step 3: Add the minimal in-flight guard implementation**

In `CPACodexKeeper.__init__`, add:

```python
        self._running_tracked_rechecks: set[str] = set()
        self._running_tracked_rechecks_lock = threading.Lock()
```

Add helpers near the tracked-recheck methods:

```python
    def _try_mark_tracked_recheck_running(self, name):
        with self._running_tracked_rechecks_lock:
            if name in self._running_tracked_rechecks:
                return False
            self._running_tracked_rechecks.add(name)
            return True

    def _clear_tracked_recheck_running(self, name):
        with self._running_tracked_rechecks_lock:
            self._running_tracked_rechecks.discard(name)
```

At the top of `_run_tracked_recheck(...)`, after confirming the tracked entry still exists, add:

```python
        if not self._try_mark_tracked_recheck_running(name):
            self.log("INFO", f"账号 {name} 已有复查任务在执行，跳过本次补偿触发")
            return
```

In the `finally` block, clear the running marker before releasing priority:

```python
        finally:
            self._clear_tracked_recheck_running(name)
            self._release_priority("timer")
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "run_tracked_recheck_skips_when_account_is_already_running or run_tracked_recheck_clears_running_marker_after_execution" -v
```

Expected: PASS.

- [ ] **Step 5: Commit the in-flight duplicate suppression**

```powershell
git add -- "src/maintainer.py" "tests/test_maintainer.py"
git commit -m @'
避免重复执行到期复查任务

为 tracked recheck 增加进程内执行标记，防止补偿扫描与定时器重复处理同一账号。
'@
```

## Task 4: Add periodic due-account compensation scanning to daemon and monitor loops

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing compensation-scan tests**

Add these tests near the loop and tracked-recheck tests:

```python
    def test_scan_due_tracked_rechecks_runs_due_account(self):
        self.maintainer._tracked_disabled_accounts = {
            "t-due": {"next_check_at": 1000},
            "t-future": {"next_check_at": 2000},
        }
        self.maintainer._reload_tracked_disabled_accounts_state = Mock(return_value=self.maintainer._tracked_disabled_accounts)
        self.maintainer._run_tracked_recheck = Mock()
        self.maintainer.log = Mock()

        with patch("src.maintainer.time.time", return_value=1500):
            self.maintainer._scan_due_tracked_rechecks("daemon")

        self.maintainer._run_tracked_recheck.assert_called_once_with("t-due")
        self.maintainer.log.assert_any_call("INFO", "补偿扫描命中 1 个到期账号，准备立即复查")
```

```python
    def test_run_forever_scans_due_tracked_rechecks_each_round(self):
        maintainer = self.maintainer
        maintainer._scan_due_tracked_rechecks = Mock()
        maintainer.run = Mock(side_effect=KeyboardInterrupt)
        maintainer.log = Mock()

        with self.assertRaises(KeyboardInterrupt):
            maintainer.run_forever(interval_seconds=30)

        maintainer._scan_due_tracked_rechecks.assert_called_once_with("daemon")
```

```python
    def test_run_fill_forever_scans_due_tracked_rechecks_each_round(self):
        maintainer = self.maintainer
        maintainer._scan_due_tracked_rechecks = Mock()
        maintainer.run_fill_once = Mock(side_effect=KeyboardInterrupt)
        maintainer.log = Mock()

        with self.assertRaises(KeyboardInterrupt):
            maintainer.run_fill_forever(interval_seconds=10)

        maintainer._scan_due_tracked_rechecks.assert_called_once_with("monitor")
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "scan_due_tracked_rechecks_runs_due_account or run_forever_scans_due_tracked_rechecks_each_round or run_fill_forever_scans_due_tracked_rechecks_each_round" -v
```

Expected: FAIL because `_scan_due_tracked_rechecks()` does not exist and the loops do not call it.

- [ ] **Step 3: Add the minimal compensation scan implementation**

Add a helper below `_run_tracked_recheck(...)`:

```python
    def _scan_due_tracked_rechecks(self, source):
        self.log("INFO", f"开始扫描到期复查账号补偿队列（source={source}）")
        tracked_state = self._reload_tracked_disabled_accounts_state()
        now = int(time.time())
        due_names = []
        for name, entry in tracked_state.items():
            if not isinstance(entry, dict):
                continue
            next_check_at = entry.get("next_check_at")
            if isinstance(next_check_at, int) and next_check_at <= now:
                due_names.append(name)
        if not due_names:
            self.log("INFO", "补偿扫描未命中到期账号")
            return
        self.log("INFO", f"补偿扫描命中 {len(due_names)} 个到期账号，准备立即复查")
        for name in due_names:
            self.log("INFO", f"账号 {name} 已到计划复查时间，但未见定时复查完成，启动补偿复查")
            self._run_tracked_recheck(name)
```

Call it in `run_forever()` before each main round begins:

```python
            self._scan_due_tracked_rechecks("daemon")
            self.log("INFO", f"主巡检第 {round_no} 轮开始：准备扫描全部 codex 账号")
```

Call it in `run_fill_forever()` before each monitor round begins:

```python
                self._scan_due_tracked_rechecks("monitor")
                result = self.run_fill_once()
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_maintainer.py -k "scan_due_tracked_rechecks_runs_due_account or run_forever_scans_due_tracked_rechecks_each_round or run_fill_forever_scans_due_tracked_rechecks_each_round" -v
```

Expected: PASS.

- [ ] **Step 5: Commit the compensation scan work**

```powershell
git add -- "src/maintainer.py" "tests/test_maintainer.py"
git commit -m @'
增加到期复查补偿扫描机制

在 daemon 与 monitor 循环中补扫到期账号，为定时复查提供第二道保障。
'@
```

## Task 5: Run regression verification and review delivery hygiene

**Files:**
- Modify: `src/maintainer.py`
- Modify: `tests/test_maintainer.py`
- Verify: current branch state

- [ ] **Step 1: Run the full maintainer regression suite**

Run:

```powershell
python -m pytest tests/test_maintainer.py -v
```

Expected: PASS.

- [ ] **Step 2: Run the full repository test suite**

Run:

```powershell
python -m pytest -v
```

Expected: PASS.

- [ ] **Step 3: Review the final diff for code-only scope**

Run:

```powershell
git diff -- src/maintainer.py tests/test_maintainer.py docs/superpowers/specs/2026-05-12-tracked-recheck-hardening-design.md docs/superpowers/plans/2026-05-12-tracked-recheck-hardening.md
```

Expected: the code changes are limited to maintainer hardening and test coverage; spec/plan files remain confined to the development branch.

- [ ] **Step 4: Record the delivery hygiene rule for merge to `main`**

Before any future merge from `new` to `main`, exclude `docs/superpowers/**` and other AI-development artifacts. The merge candidate for `main` should include only:

- `src/maintainer.py`
- `tests/test_maintainer.py`
- any strictly necessary project files discovered during implementation

Do **not** merge spec/plan files into `main`.

- [ ] **Step 5: Commit the finished hardening work if any verification follow-up changed code**

If Task 5 uncovered no new code changes, do not create an extra commit. If a follow-up fix was necessary, create a new Chinese commit describing the verification fix.

## Self-review checklist

- Spec coverage: timer + periodic compensation, in-process duplicate suppression, tracked/delete-blocked normalization, explicit logs, and branch hygiene are all covered by Tasks 1-5.
- Placeholder scan: no TBD/TODO markers remain; every task includes concrete code or commands.
- Type consistency: `_normalize_tracked_disabled_accounts_state`, `_normalize_delete_blocked_events`, `_try_mark_tracked_recheck_running`, `_clear_tracked_recheck_running`, and `_scan_due_tracked_rechecks` are used consistently across all tasks.

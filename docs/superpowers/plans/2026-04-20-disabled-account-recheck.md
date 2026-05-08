# Disabled account recheck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist recheck times for quota-disabled accounts and only auto-enable them after their scheduled recheck passes usage validation.

**Architecture:** Extend `Settings` with one fallback interval, add a small JSON-backed state store inside `CPACodexKeeper`, and thread the schedule checks through the existing quota-policy flow in `src/maintainer.py`. Keep state updates local to the maintainer and verify behavior with focused unit tests around disable, defer, recheck, reschedule, and manual-disable cases.

**Tech Stack:** Python 3.11, unittest, JSON file persistence, existing CPA/OpenAI clients

---

### Task 1: Add configuration for missing reset timestamps

**Files:**
- Modify: `src/settings.py`
- Modify: `.env.example`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing test for the new settings field**

```python
settings = Settings(
    cpa_endpoint="https://example.com",
    cpa_token="secret",
    quota_threshold=100,
    expiry_threshold_days=3,
    quota_reset_none_recheck_seconds=18000,
)
assert settings.quota_reset_none_recheck_seconds == 18000
```

- [ ] **Step 2: Run the settings-related test target**

Run: `pytest tests/test_maintainer.py -k recheck_seconds -v`
Expected: FAIL because `Settings` has no `quota_reset_none_recheck_seconds`

- [ ] **Step 3: Add the new default and dataclass field**

```python
DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS = 18000

@dataclass(slots=True)
class Settings:
    cpa_endpoint: str
    cpa_token: str
    proxy: str | None = None
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    quota_threshold: int = DEFAULT_QUOTA_THRESHOLD
    quota_reset_none_recheck_seconds: int = DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS
```

- [ ] **Step 4: Read the new env var during settings load**

```python
quota_reset_none_recheck_seconds=_read_int(
    "CPA_QUOTA_RESET_NONE_RECHECK_SECONDS",
    DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS,
    env_values,
    minimum=1,
),
```

- [ ] **Step 5: Document the env var in `.env.example`**

```env
# Fallback recheck delay in seconds when threshold-reaching quota windows expose no reset_at
# 达到禁用阈值但对应 quota 窗口没有 reset_at 时的回查延迟（秒）
CPA_QUOTA_RESET_NONE_RECHECK_SECONDS=18000
```

- [ ] **Step 6: Run the focused tests**

Run: `pytest tests/test_maintainer.py -k recheck_seconds -v`
Expected: PASS

### Task 2: Add JSON-backed disabled-account schedule state

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write failing tests for state load, save, and lookup**

```python
maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)
maintainer._tracked_disabled_accounts = {"token-a": {"next_check_at": 123}}
assert maintainer._get_tracked_next_check_at("token-a") == 123
assert maintainer._get_tracked_next_check_at("missing") is None
```

- [ ] **Step 2: Run the focused state tests**

Run: `pytest tests/test_maintainer.py -k tracked_next_check -v`
Expected: FAIL because helper methods do not exist

- [ ] **Step 3: Add state path, lock, and initial load in `__init__`**

```python
import json
from pathlib import Path

self._state_lock = threading.Lock()
self.disabled_accounts_path = Path(__file__).resolve().parents[1] / "disabled_accounts.json"
self._tracked_disabled_accounts = self._load_disabled_accounts_state()
```

- [ ] **Step 4: Add JSON helper methods**

```python
def _load_disabled_accounts_state(self):
    if not self.disabled_accounts_path.exists():
        return {}
    try:
        data = json.loads(self.disabled_accounts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        self.log("ERROR", f"加载禁用账号计划失败: {exc}")
        return {}
    return data if isinstance(data, dict) else {}

def _save_disabled_accounts_state(self):
    payload = json.dumps(self._tracked_disabled_accounts, ensure_ascii=False, indent=2, sort_keys=True)
    self.disabled_accounts_path.write_text(payload + "\n", encoding="utf-8")

def _get_tracked_next_check_at(self, name):
    entry = self._tracked_disabled_accounts.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("next_check_at")
    return value if isinstance(value, int) else None
```

- [ ] **Step 5: Add update and removal helpers**

```python
def _set_tracked_next_check_at(self, name, ts):
    with self._state_lock:
        self._tracked_disabled_accounts[name] = {"next_check_at": int(ts)}
        self._save_disabled_accounts_state()

def _remove_tracked_account(self, name):
    with self._state_lock:
        if name in self._tracked_disabled_accounts:
            self._tracked_disabled_accounts.pop(name)
            self._save_disabled_accounts_state()
```

- [ ] **Step 6: Run the focused state tests**

Run: `pytest tests/test_maintainer.py -k tracked_next_check -v`
Expected: PASS

### Task 3: Compute recheck timestamps from threshold-reaching windows

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write failing tests for next-check computation**

```python
body_info = {
    "primary_used_percent": 58,
    "primary_reset_at": 1776634820,
    "secondary_used_percent": 100,
    "secondary_reset_at": 1777000096,
}
assert maintainer._compute_next_check_at_from_usage(body_info, now=1000, fallback_seconds=50) == 1777000096
```

```python
body_info = {
    "primary_used_percent": 100,
    "primary_reset_at": None,
    "secondary_used_percent": None,
    "secondary_reset_at": None,
}
assert maintainer._compute_next_check_at_from_usage(body_info, now=1000, fallback_seconds=50) == 1050
```

- [ ] **Step 2: Run the focused computation tests**

Run: `pytest tests/test_maintainer.py -k compute_next_check_at -v`
Expected: FAIL because helper method does not exist

- [ ] **Step 3: Implement the timestamp computation helper**

```python
def _compute_next_check_at_from_usage(self, body_info, now, fallback_seconds):
    reached_reset_ats = []
    primary_pct = body_info.get("primary_used_percent", 0)
    if primary_pct >= self.settings.quota_threshold:
        primary_reset_at = body_info.get("primary_reset_at")
        if isinstance(primary_reset_at, int):
            reached_reset_ats.append(primary_reset_at)
    secondary_pct = body_info.get("secondary_used_percent")
    if secondary_pct is not None and secondary_pct >= self.settings.quota_threshold:
        secondary_reset_at = body_info.get("secondary_reset_at")
        if isinstance(secondary_reset_at, int):
            reached_reset_ats.append(secondary_reset_at)
    if reached_reset_ats:
        return max(reached_reset_ats)
    return now + fallback_seconds
```

- [ ] **Step 4: Run the focused computation tests**

Run: `pytest tests/test_maintainer.py -k compute_next_check_at -v`
Expected: PASS

### Task 4: Gate disabled accounts by the persisted schedule

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write a failing test for skipping usage before `next_check_at`**

```python
self.maintainer._tracked_disabled_accounts = {"t-skip": {"next_check_at": 2000}}
with patch("src.maintainer.time.time", return_value=1000):
    result = self.maintainer.process_token({"name": "t-skip"}, 1, 1)
self.maintainer.check_token_live.assert_not_called()
assert result == "alive"
```

- [ ] **Step 2: Run the focused skip test**

Run: `pytest tests/test_maintainer.py -k skip_usage_before_next_check -v`
Expected: FAIL because disabled tracked accounts still check usage immediately

- [ ] **Step 3: Add the pre-usage schedule gate in `process_token`**

```python
now = int(time.time())
tracked_next_check_at = self._get_tracked_next_check_at(name)
if disabled and tracked_next_check_at is not None and now < tracked_next_check_at:
    logger.log("INFO", f"已禁用，计划于 {tracked_next_check_at} 后复查 usage，当前跳过", indent=1)
    self._inc_stat("alive")
    logger.blank_line()
    return "alive"
```

- [ ] **Step 4: Run the focused skip test**

Run: `pytest tests/test_maintainer.py -k skip_usage_before_next_check -v`
Expected: PASS

### Task 5: Update quota policy to schedule, enable, and reschedule tracked disables

**Files:**
- Modify: `src/maintainer.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write failing tests for auto-disable scheduling and timed re-enable**

```python
with patch("src.maintainer.time.time", return_value=1000):
    result = self.maintainer.process_token({"name": "t-auto"}, 1, 1)
assert self.maintainer._get_tracked_next_check_at("t-auto") == 1777000096
assert result == "alive"
```

```python
self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
with patch("src.maintainer.time.time", return_value=1000):
    result = self.maintainer.process_token({"name": "t-enable"}, 1, 1)
self.maintainer.set_disabled_status.assert_called_with("t-enable", disabled=False, logger=ANY)
assert self.maintainer._get_tracked_next_check_at("t-enable") is None
assert result == "alive"
```

- [ ] **Step 2: Run the focused quota-schedule tests**

Run: `pytest tests/test_maintainer.py -k "tracked or auto_disable" -v`
Expected: FAIL because quota policy does not manage persistent schedule state

- [ ] **Step 3: Thread `body_info` and current time into quota policy**

```python
quota_result, refresh_disabled = self._apply_quota_policy(
    name,
    disabled,
    primary_pct,
    secondary_pct,
    logger,
    has_refresh_token=self._has_refresh_token(token_detail),
    primary_label=primary_label,
    secondary_label=secondary_label,
    body_info=body_info,
    now=now,
)
```

- [ ] **Step 4: On new auto-disable, write `next_check_at`**

```python
next_check_at = self._compute_next_check_at_from_usage(
    body_info,
    now,
    self.settings.quota_reset_none_recheck_seconds,
)
self._set_tracked_next_check_at(name, next_check_at)
logger.log("INFO", f"已记录下次 quota 复查时间: {next_check_at}", indent=1)
```

- [ ] **Step 5: On tracked disabled accounts below threshold, enable and clear state**

```python
tracked_next_check_at = self._get_tracked_next_check_at(name)
if disabled and below_threshold and tracked_next_check_at is not None:
    if self.set_disabled_status(name, disabled=False, logger=logger):
        self._remove_tracked_account(name)
        logger.log("ENABLE", "已重新启用", indent=1)
```

- [ ] **Step 6: Keep manually disabled accounts disabled even if usage drops**

```python
if disabled and below_threshold and tracked_next_check_at is None:
    logger.log("INFO", "已禁用且未被 keeper 纳入自动复查，保持禁用", indent=1)
    return None, effective_disabled
```

- [ ] **Step 7: On tracked recheck still above threshold, reschedule**

```python
if disabled and tracked_next_check_at is not None and (primary_reached or secondary_reached):
    next_check_at = self._compute_next_check_at_from_usage(body_info, now, self.settings.interval_seconds)
    self._set_tracked_next_check_at(name, next_check_at)
    logger.log("INFO", f"额度仍未恢复，已重排下次复查时间: {next_check_at}", indent=1)
    return None, effective_disabled
```

- [ ] **Step 8: Remove schedule entry on delete paths**

```python
def _delete_token_with_reason(self, name, reason, logger):
    logger.log("WARN", reason, indent=1)
    if self.delete_token(name, logger=logger):
        self._remove_tracked_account(name)
        logger.log("DELETE", "已删除", indent=1)
```

- [ ] **Step 9: Run the focused quota tests**

Run: `pytest tests/test_maintainer.py -k "tracked or auto_disable or manual_disable" -v`
Expected: PASS

### Task 6: Cover restart persistence and reschedule fallbacks with tests

**Files:**
- Modify: `tests/test_maintainer.py`

- [ ] **Step 1: Write failing tests for restart persistence and `CPA_INTERVAL` reschedule**

```python
state_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
state_path.write_text('{"t-persist": {"next_check_at": 1234}}', encoding="utf-8")
maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)
maintainer.disabled_accounts_path = state_path
maintainer._tracked_disabled_accounts = maintainer._load_disabled_accounts_state()
assert maintainer._get_tracked_next_check_at("t-persist") == 1234
```

```python
self.maintainer._tracked_disabled_accounts = {"t-requeue": {"next_check_at": 1000}}
with patch("src.maintainer.time.time", return_value=1000):
    self.maintainer.process_token({"name": "t-requeue"}, 1, 1)
assert self.maintainer._get_tracked_next_check_at("t-requeue") == 2800
```

- [ ] **Step 2: Run the focused persistence tests**

Run: `pytest tests/test_maintainer.py -k "persist or requeue" -v`
Expected: FAIL before implementation is complete

- [ ] **Step 3: Add any minimal code/test fixture adjustments needed for deterministic state-file testing**

```python
def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp_dir.cleanup)
    self.maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)
    self.maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
    self.maintainer._tracked_disabled_accounts = {}
```

- [ ] **Step 4: Run the focused persistence tests**

Run: `pytest tests/test_maintainer.py -k "persist or requeue" -v`
Expected: PASS

### Task 7: Update user-facing documentation and run full verification

**Files:**
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `.env.example`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Update README configuration and behavior sections**

```md
- `CPA_QUOTA_RESET_NONE_RECHECK_SECONDS`：达到禁用阈值但 quota 窗口没有 `reset_at` 时的回查秒数，默认 `18000`
```

```md
- `CPA_QUOTA_RESET_NONE_RECHECK_SECONDS`: recheck delay in seconds when quota reached the disable threshold but the relevant windows expose no `reset_at`, default `18000`
```

- [ ] **Step 2: Document the new JSON state file behavior**

```md
程序会在项目根目录维护 `disabled_accounts.json`，用于记录 keeper 自动禁用账号的下一次 quota 复查时间。
```

```md
The keeper maintains `disabled_accounts.json` in the project root to persist the next quota recheck time for accounts it auto-disabled.
```

- [ ] **Step 3: Run the full maintainer test suite**

Run: `pytest tests/test_maintainer.py -v`
Expected: PASS

- [ ] **Step 4: Run the full project test suite**

Run: `pytest -v`
Expected: PASS

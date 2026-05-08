# Priority Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared priority coordinator so timer rechecks outrank usage-log inspection, which outranks full inspection, with cooperative preemption only at token boundaries.

**Architecture:** Keep all quota, disable, enable, refresh, and `disabled_accounts.json` semantics in `src/maintainer.py`. Add a small `PriorityCoordinator` that both keeper instances share, wire it through `src/cli.py`, integrate it at token boundaries for timer/log/full paths, and change full inspection from eager task submission to worker-driven token pulling.

**Tech Stack:** Python 3.11+, standard library `threading`, `ThreadPoolExecutor`, `unittest`, `unittest.mock`

---

## File map

- **Modify:** `src/maintainer.py`
  - add `PriorityCoordinator`
  - add optional shared coordinator injection to `CPACodexKeeper`
  - gate `_run_tracked_recheck()` at highest priority
  - gate `run_fill_once()` token processing at log priority
  - replace eager full-scan submission in `run()` with worker-pull-next-token flow at full priority
  - add minimal coordination logs
- **Modify:** `src/cli.py`
  - create one shared coordinator in daemon mode
  - pass the same coordinator to both keeper instances
- **Modify:** `tests/test_maintainer.py`
  - add coordinator unit tests
  - add integration-ish tests for timer > log > full token-boundary yielding
  - keep existing tracked recheck / fill / quota tests green
- **Modify:** `tests/test_cli.py`
  - assert daemon mode shares one coordinator across both keeper instances

---

### Task 1: Add the shared coordinator primitive

**Files:**
- Modify: `src/maintainer.py:17-61`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing state/timer tests in `tests/test_maintainer.py`:

```python
def test_priority_coordinator_blocks_full_when_log_is_waiting(self):
    coordinator = PriorityCoordinator()
    coordinator.request("log")

    self.assertFalse(coordinator.can_start("full"))
    self.assertTrue(coordinator.can_start("log"))


def test_priority_coordinator_blocks_log_when_timer_is_waiting(self):
    coordinator = PriorityCoordinator()
    coordinator.request("timer")

    self.assertFalse(coordinator.can_start("log"))
    self.assertTrue(coordinator.can_start("timer"))


def test_priority_coordinator_drains_multiple_timer_requests_before_lower_priority(self):
    coordinator = PriorityCoordinator()
    coordinator.request("timer")
    coordinator.request("timer")
    coordinator.request("log")

    self.assertTrue(coordinator.can_start("timer"))
    coordinator.release("timer")
    self.assertTrue(coordinator.can_start("timer"))
    coordinator.release("timer")
    self.assertTrue(coordinator.can_start("log"))
```

Expected first failure: `NameError: name 'PriorityCoordinator' is not defined`

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_priority_coordinator_blocks_full_when_log_is_waiting \
  tests.test_maintainer.MaintainerTests.test_priority_coordinator_blocks_log_when_timer_is_waiting \
  tests.test_maintainer.MaintainerTests.test_priority_coordinator_drains_multiple_timer_requests_before_lower_priority
```

Expected: FAIL because `PriorityCoordinator` does not exist yet.

- [ ] **Step 3: Write the minimal implementation**

Add this class near the top of `src/maintainer.py`, before `CPACodexKeeper`:

```python
class PriorityCoordinator:
    PRIORITY_VALUE = {
        "full": 1,
        "log": 2,
        "timer": 3,
    }

    def __init__(self):
        self._condition = threading.Condition()
        self._pending = {name: 0 for name in self.PRIORITY_VALUE}
        self._active = {name: 0 for name in self.PRIORITY_VALUE}

    def request(self, priority):
        with self._condition:
            self._pending[priority] += 1
            self._condition.notify_all()

    def can_start(self, priority):
        value = self.PRIORITY_VALUE[priority]
        return not any(
            self._pending[name] > 0 and self.PRIORITY_VALUE[name] > value
            for name in self.PRIORITY_VALUE
        )

    def acquire_next(self, priority):
        with self._condition:
            while not self.can_start(priority):
                self._condition.wait()
            self._pending[priority] -= 1
            self._active[priority] += 1

    def release(self, priority):
        with self._condition:
            self._active[priority] -= 1
            self._condition.notify_all()
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_maintainer.py src/maintainer.py
git commit -m "feat: add priority coordinator primitive"
```

---

### Task 2: Wire the coordinator into keeper construction and daemon CLI

**Files:**
- Modify: `src/maintainer.py:17-42`
- Modify: `src/cli.py:17-39`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add this test to `tests/test_cli.py` near the existing daemon tests:

```python
@patch("src.cli.threading.Thread")
@patch("src.cli.load_settings")
@patch("src.cli.CPACodexKeeper")
@patch("sys.argv", ["prog"])
def test_main_shares_one_priority_coordinator_between_main_and_fill_keepers(self, keeper_cls, load_settings_mock, thread_cls):
    load_settings_mock.return_value = Settings(
        cpa_endpoint="https://example.com",
        cpa_token="secret",
        usage_query_interval_seconds=7200,
    )
    main_keeper = Mock()
    fill_keeper = Mock()
    keeper_cls.side_effect = [main_keeper, fill_keeper]

    main()

    first_kwargs = keeper_cls.call_args_list[0].kwargs
    second_kwargs = keeper_cls.call_args_list[1].kwargs
    self.assertIs(first_kwargs["coordinator"], second_kwargs["coordinator"])
```

Expected failure: `KeyError: 'coordinator'`

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.test_cli.CLITests.test_main_shares_one_priority_coordinator_between_main_and_fill_keepers
```

Expected: FAIL because daemon mode does not pass a shared coordinator yet.

- [ ] **Step 3: Write the minimal implementation**

Update `CPACodexKeeper.__init__` in `src/maintainer.py`:

```python
class CPACodexKeeper:
    def __init__(self, settings: Settings, dry_run: bool = False, coordinator: PriorityCoordinator | None = None):
        self.settings = settings
        self.dry_run = dry_run
        self.coordinator = coordinator or PriorityCoordinator()
```

Update `src/cli.py`:

```python
from .maintainer import CPACodexKeeper, PriorityCoordinator


def main() -> int:
    ...
    coordinator = PriorityCoordinator()
    maintainer = CPACodexKeeper(settings=settings, dry_run=args.dry_run, coordinator=coordinator)
    if args.daemon:
        maintainer._start_tracked_rechecks()
        if settings.usage_query_interval_seconds > 0:
            fill_maintainer = CPACodexKeeper(settings=settings, dry_run=args.dry_run, coordinator=coordinator)
            ...
```

For `--once`, still construct the single keeper with the coordinator.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python -m unittest \
  tests.test_cli.CLITests.test_main_shares_one_priority_coordinator_between_main_and_fill_keepers \
  tests.test_cli.CLITests.test_main_runs_daemon_and_starts_fill_thread_when_usage_query_enabled \
  tests.test_cli.CLITests.test_main_runs_daemon_without_fill_thread_when_usage_query_disabled
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py src/cli.py tests/test_cli.py
git commit -m "feat: share priority coordinator across keepers"
```

---

### Task 3: Gate timer rechecks at highest priority

**Files:**
- Modify: `src/maintainer.py:191-203`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add this test near the existing tracked recheck tests:

```python
def test_run_tracked_recheck_requests_and_releases_timer_priority(self):
    coordinator = Mock()
    maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
    maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
    maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
    maintainer.process_token = Mock(return_value="alive")

    maintainer._run_tracked_recheck("t-enable")

    coordinator.request.assert_called_once_with("timer")
    coordinator.acquire_next.assert_called_once_with("timer")
    coordinator.release.assert_called_once_with("timer")
```

Expected failure: coordinator methods are never called.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.test_maintainer.MaintainerTests.test_run_tracked_recheck_requests_and_releases_timer_priority
```

Expected: FAIL because `_run_tracked_recheck()` does not use the coordinator.

- [ ] **Step 3: Write the minimal implementation**

Change `_run_tracked_recheck()` in `src/maintainer.py` to:

```python
def _run_tracked_recheck(self, name):
    with self._state_lock:
        if name not in self._tracked_disabled_accounts:
            self._tracked_recheck_timers.pop(name, None)
            return
    self._tracked_recheck_timers.pop(name, None)
    self.coordinator.request("timer")
    self.coordinator.acquire_next("timer")
    try:
        self.logger.emit_lines([
            f"{self.logger.PREFIX_MAP['INFO']} 账号 {name} 到达计划复查时间，开始复查使用额度"
        ])
        self.process_token({"name": name}, 1, 1)
    except Exception as exc:
        self.log("ERROR", f"账号 {name} 定时复查异常: {exc}")
    finally:
        self.coordinator.release("timer")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_run_tracked_recheck_requests_and_releases_timer_priority \
  tests.test_maintainer.MaintainerTests.test_run_tracked_recheck_enables_due_token_and_logs \
  tests.test_maintainer.MaintainerTests.test_run_tracked_recheck_logs_exceptions
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: prioritize tracked timer rechecks"
```

---

### Task 4: Gate usage-log inspection at token boundaries

**Files:**
- Modify: `src/maintainer.py:797-827`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add a test near the fill-mode tests:

```python
def test_run_fill_once_requests_and_releases_log_priority_for_each_token(self):
    coordinator = Mock()
    maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
    maintainer.last_usage_query_time = 1000
    maintainer.cpa_client.list_auth_files = Mock(return_value=[
        {"name": "token-a", "type": "codex", "email": "a@example.com"},
        {"name": "token-b", "type": "codex", "email": "b@example.com"},
    ])
    maintainer.get_usage_log = Mock(return_value={
        "usage": {
            "apis": {
                "api-1": {
                    "models": {
                        "gpt-5.3-codex": {
                            "details": [
                                {"source": "a@example.com", "timestamp": "1970-01-01T00:17:40+00:00"},
                                {"source": "b@example.com", "timestamp": "1970-01-01T00:17:50+00:00"},
                            ]
                        }
                    }
                }
            }
        }
    })
    maintainer.process_fill_token = Mock(side_effect=["alive", "alive"])

    with patch("src.maintainer.time.time", return_value=1100):
        maintainer.run_fill_once()

    self.assertEqual(coordinator.request.call_args_list, [call("log"), call("log")])
    self.assertEqual(coordinator.acquire_next.call_args_list, [call("log"), call("log")])
    self.assertEqual(coordinator.release.call_args_list, [call("log"), call("log")])
```

Expected failure: coordinator calls do not happen.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest tests.test_maintainer.MaintainerTests.test_run_fill_once_requests_and_releases_log_priority_for_each_token
```

Expected: FAIL.

- [ ] **Step 3: Write the minimal implementation**

Wrap each matched token in `run_fill_once()`:

```python
for idx, token_info in enumerate(matched_tokens, 1):
    self.coordinator.request("log")
    self.coordinator.acquire_next("log")
    try:
        self.process_fill_token(token_info, idx, total)
    finally:
        self.coordinator.release("log")
```

Add one concise wait log only when blocking is observed. Keep it out of the hot path unless contention exists.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_run_fill_once_requests_and_releases_log_priority_for_each_token \
  tests.test_maintainer.MaintainerTests.test_fill_mode_processes_new_usage_emails_and_disables_threshold_accounts \
  tests.test_maintainer.MaintainerTests.test_fill_mode_filters_usage_emails_against_filtered_auth_files \
  tests.test_maintainer.MaintainerTests.test_fill_mode_skips_usage_query_when_interval_disabled
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: gate log inspection by priority"
```

---

### Task 5: Change full inspection to worker-pull token processing

**Files:**
- Modify: `src/maintainer.py:734-778`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add tests that lock in the new shape without relying on real threads:

```python
@patch("src.maintainer.ThreadPoolExecutor")
def test_run_full_inspection_workers_request_full_priority_per_token(self, executor_cls):
    coordinator = Mock()
    maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
    maintainer.cpa_client.list_auth_files = Mock(return_value=[
        {"name": "a", "type": "codex"},
        {"name": "b", "type": "codex"},
    ])
    maintainer.process_token = Mock(return_value="alive")

    submitted = []

    class InlineExecutor:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def submit(self, fn, *args, **kwargs):
            future = Future()
            submitted.append((fn, args, kwargs))
            future.set_result(fn(*args, **kwargs))
            return future

    executor_cls.return_value = InlineExecutor()

    maintainer.run()

    self.assertEqual(coordinator.request.call_count, 2)
    self.assertEqual(coordinator.acquire_next.call_count, 2)
    self.assertEqual(coordinator.release.call_count, 2)
    self.assertEqual(maintainer.process_token.call_count, 2)
```

Add a second test for release-on-exception:

```python
@patch("src.maintainer.ThreadPoolExecutor")
def test_run_full_inspection_releases_priority_when_process_token_raises(self, executor_cls):
    coordinator = Mock()
    maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
    maintainer.cpa_client.list_auth_files = Mock(return_value=[{"name": "a", "type": "codex"}])
    maintainer.process_token = Mock(side_effect=RuntimeError("boom"))

    class InlineExecutor:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def submit(self, fn, *args, **kwargs):
            future = Future()
            try:
                fn(*args, **kwargs)
            except Exception as exc:
                future.set_exception(exc)
            return future

    executor_cls.return_value = InlineExecutor()

    maintainer.run()

    coordinator.release.assert_called_once_with("full")
```

Expected failure: `run()` does eager submit and never calls the coordinator.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_run_full_inspection_workers_request_full_priority_per_token \
  tests.test_maintainer.MaintainerTests.test_run_full_inspection_releases_priority_when_process_token_raises
```

Expected: FAIL.

- [ ] **Step 3: Write the minimal implementation**

In `src/maintainer.py`, extract a worker helper:

```python
def _process_tokens_with_priority(self, tokens):
    total = len(tokens)
    token_iter = iter(enumerate(tokens, 1))
    token_iter_lock = threading.Lock()

    def worker():
        while True:
            self.coordinator.request("full")
            self.coordinator.acquire_next("full")
            try:
                with token_iter_lock:
                    try:
                        idx, token_info = next(token_iter)
                    except StopIteration:
                        return
                self.process_token(token_info, idx, total)
            finally:
                self.coordinator.release("full")
```

Then replace the eager loop in `run()` with a fixed number of worker submissions:

```python
with ThreadPoolExecutor(max_workers=self.settings.worker_threads) as executor:
    futures = [executor.submit(worker) for _ in range(min(total, self.settings.worker_threads))]
    for future in as_completed(futures):
        ...
```

Important: if no token remains after acquiring full priority, release in `finally` and return cleanly.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_run_full_inspection_workers_request_full_priority_per_token \
  tests.test_maintainer.MaintainerTests.test_run_full_inspection_releases_priority_when_process_token_raises
```

Then run existing regression coverage:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_process_token_skips_usage_before_tracked_next_check \
  tests.test_maintainer.MaintainerTests.test_process_token_enables_tracked_disabled_token_when_due_and_below_threshold \
  tests.test_maintainer.MaintainerTests.test_process_token_reschedules_tracked_disabled_token_with_interval_when_reset_missing
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/maintainer.py tests/test_maintainer.py
git commit -m "feat: gate full inspection by token priority"
```

---

### Task 6: Add coordination behavior tests for token-boundary yielding

**Files:**
- Modify: `tests/test_maintainer.py`

- [ ] **Step 1: Write the failing tests**

Add a focused fake coordinator for deterministic tests:

```python
class RecordingCoordinator:
    def __init__(self):
        self.events = []
        self.allowed = {"full": True, "log": True, "timer": True}

    def request(self, priority):
        self.events.append(("request", priority))

    def acquire_next(self, priority):
        self.events.append(("acquire", priority))

    def release(self, priority):
        self.events.append(("release", priority))
```

Then add tests that assert the sequence around one token at a time:

```python
def test_log_priority_is_requested_between_fill_tokens(self):
    coordinator = RecordingCoordinator()
    maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
    ...
    self.assertEqual(
        coordinator.events,
        [
            ("request", "log"), ("acquire", "log"), ("release", "log"),
            ("request", "log"), ("acquire", "log"), ("release", "log"),
        ],
    )
```

```python
def test_timer_priority_wraps_tracked_recheck_once_per_due_token(self):
    coordinator = RecordingCoordinator()
    maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
    ...
    self.assertEqual(
        coordinator.events,
        [("request", "timer"), ("acquire", "timer"), ("release", "timer")],
    )
```

Expected initial failure only if earlier tasks were skipped; if earlier tasks already satisfy this, treat this task as regression coverage and keep the tests.

- [ ] **Step 2: Run tests to verify they fail or prove coverage gap**

Run:

```bash
python -m unittest \
  tests.test_maintainer.MaintainerTests.test_log_priority_is_requested_between_fill_tokens \
  tests.test_maintainer.MaintainerTests.test_timer_priority_wraps_tracked_recheck_once_per_due_token
```

Expected: either FAIL before implementation or PASS as explicit regression coverage.

- [ ] **Step 3: Write minimal implementation only if needed**

If the tests already pass, do not change production code in this step.

If they fail, only adjust the smallest integration point that is missing.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_maintainer.py src/maintainer.py
git commit -m "test: lock priority coordination behavior"
```

---

### Task 7: Run focused regression suite and document confidence

**Files:**
- Modify: `docs/superpowers/plans/2026-04-20-priority-coordination.md`

- [ ] **Step 1: Run the focused regression suite**

Run:

```bash
python -m unittest tests.test_cli tests.test_maintainer
```

Expected: PASS.

- [ ] **Step 2: If any test fails, fix one root cause at a time**

For each failure:

1. write or tighten the failing test first if coverage is unclear
2. make the smallest production change
3. rerun the relevant subset
4. rerun the full command above

Do not bundle unrelated cleanup.

- [ ] **Step 3: Mark verification notes in this plan file**

Append a short checked note after implementation saying which command passed.

```markdown
## Verification notes

- [x] `python -m unittest tests.test_cli`
- [x] `python -m unittest tests.test_cli tests.test_maintainer`
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-04-20-priority-coordination.md
git commit -m "docs: record priority coordination verification"
```

## Verification notes

- [x] `python -m unittest tests.test_cli`
- [x] `python -m unittest tests.test_cli tests.test_maintainer`

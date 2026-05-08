# Monitor-only mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `-monitor` startup flag that runs only usage-log polling plus tracked recheck timers, without starting the full inspection loop.

**Architecture:** Keep the change at the CLI boundary. Extend `build_arg_parser()` in `src/cli.py` to accept `-monitor` and reject `-monitor --once`, then branch in `main()` so monitor-only mode starts `_start_tracked_rechecks()` and `run_fill_forever(...)` on a single keeper instance without creating the extra fill thread or entering `run_forever(...)`.

**Tech Stack:** Python 3, argparse, unittest.mock, pytest

---

## File map

- `src/cli.py`
  - Parses startup flags and decides which keeper loops start.
  - This is the only production file that needs to change.
- `tests/test_cli.py`
  - Covers parser behavior and `main()` startup wiring with mocks.
  - New tests should stay in the existing `CLITests` class and match the current unittest style.
- No changes are needed in `src/maintainer.py` because monitor-only mode reuses existing `_start_tracked_rechecks()` and `run_fill_forever(...)` behavior.

### Task 1: Add parser support for `-monitor`

**Files:**
- Modify: `src/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing parser tests**

Add these tests to `tests/test_cli.py` near the existing parser tests:

```python
    def test_monitor_flag_enables_monitor_mode(self):
        parser = build_arg_parser()
        args = parser.parse_args(["-monitor"])

        self.assertTrue(args.monitor)
        self.assertTrue(args.daemon)

    def test_monitor_flag_cannot_be_combined_with_once(self):
        parser = build_arg_parser()

        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                parser.parse_args(["-monitor", "--once"])

        self.assertIn("not allowed with argument", stderr.getvalue())
```

- [ ] **Step 2: Run the new tests to verify they fail for the right reason**

Run:

```bash
pytest tests/test_cli.py::CLITests::test_monitor_flag_enables_monitor_mode tests/test_cli.py::CLITests::test_monitor_flag_cannot_be_combined_with_once -v
```

Expected before implementation:

- `test_monitor_flag_enables_monitor_mode` fails because `-monitor` is not recognized yet
- `test_monitor_flag_cannot_be_combined_with_once` fails because the parser does not yet report a mutual-exclusion error

- [ ] **Step 3: Implement the minimal parser change in `src/cli.py`**

Replace `build_arg_parser()` with:

```python
def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(description="CPACodexKeeper")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不实际修改 / Dry run")
    parser.add_argument("--daemon", action="store_true", default=True, help="守护模式，默认开启 / Run forever")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", dest="daemon", action="store_false", help="仅执行一轮后退出 / Run once")
    mode_group.add_argument("-monitor", dest="monitor", action="store_true", help="仅启动日志巡检与定时复查 / Monitor only")
    parser.set_defaults(monitor=False)
    return parser
```

- [ ] **Step 4: Run the parser tests again to verify they pass**

Run:

```bash
pytest tests/test_cli.py::CLITests::test_monitor_flag_enables_monitor_mode tests/test_cli.py::CLITests::test_monitor_flag_cannot_be_combined_with_once -v
```

Expected after implementation:

```text
2 passed
```

- [ ] **Step 5: Commit the parser change**

Run:

```bash
git add tests/test_cli.py src/cli.py
git commit -m "feat: add monitor startup flag"
```

### Task 2: Wire monitor-only startup behavior in `main()`

**Files:**
- Modify: `src/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing runtime tests for monitor-only mode**

Add these tests to `tests/test_cli.py` after the existing `main()` daemon tests:

```python
    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "-monitor"])
    def test_main_runs_monitor_mode_with_tracked_rechecks_and_fill_forever(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            usage_query_interval_seconds=7200,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(keeper_cls.call_count, 1)
        keeper._start_tracked_rechecks.assert_called_once()
        keeper.run_fill_forever.assert_called_once_with(interval_seconds=7200)
        keeper.run_forever.assert_not_called()
        keeper.run.assert_not_called()
        thread_cls.assert_not_called()

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "-monitor"])
    def test_main_monitor_mode_runs_fill_forever_even_when_usage_query_disabled(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            usage_query_interval_seconds=0,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(keeper_cls.call_count, 1)
        keeper._start_tracked_rechecks.assert_called_once()
        keeper.run_fill_forever.assert_called_once_with(interval_seconds=0)
        keeper.run_forever.assert_not_called()
        keeper.run.assert_not_called()
        thread_cls.assert_not_called()
```

- [ ] **Step 2: Run the new runtime tests to verify they fail**

Run:

```bash
pytest tests/test_cli.py::CLITests::test_main_runs_monitor_mode_with_tracked_rechecks_and_fill_forever tests/test_cli.py::CLITests::test_main_monitor_mode_runs_fill_forever_even_when_usage_query_disabled -v
```

Expected before implementation:

- the tests fail because `main()` still follows the normal daemon branch
- the current code starts `run_forever(...)` and may start the extra fill thread instead of entering monitor-only mode

- [ ] **Step 3: Implement the minimal `main()` branch in `src/cli.py`**

Replace `main()` with:

```python
def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        settings = load_settings()
    except SettingsError as exc:
        parser.exit(status=2, message=f"Configuration error: {exc}\n")

    coordinator = PriorityCoordinator()
    logger = None
    maintainer = CPACodexKeeper(settings=settings, dry_run=args.dry_run, coordinator=coordinator, logger=logger)
    if args.monitor:
        maintainer._start_tracked_rechecks()
        maintainer.run_fill_forever(interval_seconds=settings.usage_query_interval_seconds)
        return 0
    if args.daemon:
        maintainer._start_tracked_rechecks()
        if settings.usage_query_interval_seconds > 0:
            fill_maintainer = CPACodexKeeper(settings=settings, dry_run=args.dry_run, coordinator=coordinator, logger=maintainer.logger)
            fill_thread = threading.Thread(
                target=fill_maintainer.run_fill_forever,
                kwargs={"interval_seconds": settings.usage_query_interval_seconds},
                daemon=True,
            )
            fill_thread.start()
        maintainer.run_forever(interval_seconds=settings.interval_seconds)
        return 0
    maintainer.run()
    return 0
```

- [ ] **Step 4: Run the runtime tests again to verify they pass**

Run:

```bash
pytest tests/test_cli.py::CLITests::test_main_runs_monitor_mode_with_tracked_rechecks_and_fill_forever tests/test_cli.py::CLITests::test_main_monitor_mode_runs_fill_forever_even_when_usage_query_disabled -v
```

Expected after implementation:

```text
2 passed
```

- [ ] **Step 5: Run the full CLI test file as a focused regression check**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected:

- all `CLITests` pass
- existing daemon-mode tests still pass unchanged

- [ ] **Step 6: Commit the runtime wiring change**

Run:

```bash
git add tests/test_cli.py src/cli.py
git commit -m "feat: add monitor-only startup mode"
```

### Task 3: Run the final targeted verification suite

**Files:**
- Test: `tests/test_cli.py`
- Test: `tests/test_maintainer.py`

- [ ] **Step 1: Run the final targeted verification suite**

Run:

```bash
pytest tests/test_cli.py tests/test_maintainer.py -v
```

Expected:

- the new monitor-only CLI tests pass
- existing CLI tests still pass
- maintainer tests still pass, proving the CLI change did not alter existing timer or log-polling behavior

- [ ] **Step 2: Record the final manual verification result in the work log or PR description**

Use this exact note in the PR description or handoff comment:

```text
Verified monitor-only mode through CLI unit tests. `-monitor` starts tracked rechecks plus usage-log polling, does not start the full inspection loop, and rejects `-monitor --once`.
```

## Self-review checklist

- Spec coverage: covered `-monitor` parsing, `-monitor --once` rejection, timer startup, usage-log startup, no full inspection, and disabled-interval behavior.
- Placeholder scan: no `TODO`, `TBD`, or implied code steps without concrete snippets.
- Type consistency: the plan uses `args.monitor`, existing `args.daemon`, `CPACodexKeeper`, `_start_tracked_rechecks()`, `run_fill_forever(...)`, and `run_forever(...)`, all matching current code.

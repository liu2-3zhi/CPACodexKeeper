import json
import pathlib
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, Mock, call, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.logging_utils import ConsoleLogger as RealConsoleLogger
from src.maintainer import CPACodexKeeper, PriorityCoordinator
from src.openai_client import parse_usage_info
from src.settings import Settings


class RecordingCoordinator:
    def __init__(self):
        self.events = []

    def request(self, priority):
        self.events.append(("request", priority))

    def acquire_next(self, priority):
        self.events.append(("acquire", priority))

    def release(self, priority):
        self.events.append(("release", priority))


class MaintainerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.created_loggers = []
        self.logger_index = 0

        def build_logger(*args, **kwargs):
            log_dir = pathlib.Path(self.temp_dir.name) / f"logs-{self.logger_index}"
            self.logger_index += 1
            logger = RealConsoleLogger(
                log_dir=log_dir,
                archive_max_size_bytes=kwargs.get("archive_max_size_bytes", 500 * 1024 * 1024),
            )
            self.created_loggers.append(logger)
            return logger

        self.console_logger_patcher = patch("src.maintainer.ConsoleLogger", side_effect=build_logger)
        self.console_logger_patcher.start()
        self.addCleanup(self.console_logger_patcher.stop)
        self.addCleanup(lambda: [logger.close() for logger in self.created_loggers])

        self.settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
        )
        self.maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)
        self.maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        self.maintainer._tracked_disabled_accounts = {}

    def test_filter_tokens_keeps_only_codex_type(self):
        tokens = [
            {"name": "a", "type": "codex"},
            {"name": "b", "type": "oauth"},
            {"name": "c", "type": "codex"},
            {"name": "d"},
        ]
        filtered = self.maintainer.filter_tokens(tokens)
        self.assertEqual([token["name"] for token in filtered], ["a", "c"])

    def test_parse_usage_info_reads_team_primary_and_secondary_windows(self):
        usage = parse_usage_info({
            "plan_type": "team",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 15,
                    "limit_window_seconds": 18000,
                    "reset_at": 1,
                },
                "secondary_window": {
                    "used_percent": 80,
                    "limit_window_seconds": 604800,
                    "reset_at": 2,
                },
            },
            "credits": {"has_credits": False, "balance": None},
        })
        self.assertEqual(usage.plan_type, "team")
        self.assertEqual(usage.primary_used_percent, 15)
        self.assertEqual(usage.secondary_used_percent, 80)
        self.assertEqual(usage.quota_check_percent, 80)
        self.assertEqual(usage.quota_check_label, "Week")

    def test_parse_usage_info_falls_back_to_primary_when_secondary_missing(self):
        usage = parse_usage_info({
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 30,
                    "limit_window_seconds": 604800,
                },
                "secondary_window": None,
            },
        })
        self.assertEqual(usage.secondary_used_percent, None)
        self.assertEqual(usage.quota_check_percent, 30)
        self.assertEqual(usage.quota_check_label, "Week")

    def test_process_token_deletes_invalid_token_on_401(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(401, {"brief": "unauthorized"}))
        result = self.maintainer.process_token({"name": "t1"}, 1, 1)
        self.assertEqual(result, "dead")
        self.assertEqual(self.maintainer.stats.dead, 1)

    def test_process_token_deletes_invalid_token_on_402(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(402, {"brief": "deactivated_workspace"}))
        result = self.maintainer.process_token({"name": "t402"}, 1, 1)
        self.assertEqual(result, "dead")
        self.assertEqual(self.maintainer.stats.dead, 1)

    def test_console_logger_creates_timestamped_log_file_and_archives_previous_logs(self):
        log_dir = pathlib.Path(self.temp_dir.name) / "logger-archive-test" / "logs"
        archive_dir = log_dir / "archive"
        log_dir.mkdir(parents=True)
        old_log = log_dir / "2026-02-28-23-59-59.txt"
        old_log.write_text("old log", encoding="utf-8")

        from src.logging_utils import ConsoleLogger
        logger = ConsoleLogger(log_dir=log_dir, archive_max_size_bytes=500 * 1024 * 1024)
        self.addCleanup(logger.close)
        logger.log("INFO", "message")

        self.assertFalse(old_log.exists())
        archived_files = list(archive_dir.glob("*.zip"))
        self.assertEqual(len(archived_files), 1)
        new_logs = sorted(log_dir.glob("*.txt"))
        self.assertEqual(len(new_logs), 1)
        self.assertRegex(new_logs[0].name, r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.txt")
        self.assertRegex(
            new_logs[0].read_text(encoding="utf-8"),
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]: message",
        )

    def test_console_logger_prunes_old_archives_when_size_limit_exceeded(self):
        log_dir = pathlib.Path(self.temp_dir.name) / "logger-prune-test" / "logs"
        archive_dir = log_dir / "archive"
        archive_dir.mkdir(parents=True)
        old_archive = archive_dir / "2026-02-27-00-00-00.zip"
        old_archive.write_bytes(b"12345")
        new_archive = archive_dir / "2026-02-28-00-00-00.zip"
        new_archive.write_bytes(b"67890")

        from src.logging_utils import ConsoleLogger
        logger = ConsoleLogger(log_dir=log_dir, archive_max_size_bytes=5)
        self.addCleanup(logger.close)

        self.assertFalse(old_archive.exists())
        self.assertTrue(new_archive.exists())

    def test_process_token_disables_when_weekly_quota_reaches_threshold(self):
        captured_lines = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_lines.append(list(lines)))
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 10, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 100, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.set_disabled_status = Mock(return_value=True)
        result = self.maintainer.process_token({"name": "t2"}, 1, 1)
        self.assertEqual(result, "alive")
        self.maintainer.set_disabled_status.assert_called_once()
        args, kwargs = self.maintainer.set_disabled_status.call_args
        self.assertEqual(args, ("t2",))
        self.assertEqual(kwargs["disabled"], True)
        self.assertEqual(self.maintainer.stats.disabled, 1)
        emitted = "\n".join(captured_lines[0])
        self.assertIn("账号已禁用", emitted)
        self.assertIn("已记录下次检查额度时间:", emitted)

    def test_process_token_disables_when_primary_quota_reaches_threshold_even_if_weekly_is_below(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 100, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 28, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.set_disabled_status = Mock(return_value=True)

        result = self.maintainer.process_token({"name": "t2-primary"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.set_disabled_status.assert_called_once()
        args, kwargs = self.maintainer.set_disabled_status.call_args
        self.assertEqual(args, ("t2-primary",))
        self.assertEqual(kwargs["disabled"], True)
        self.assertEqual(self.maintainer.stats.disabled, 1)

    def test_process_token_keeps_untracked_disabled_token_when_weekly_quota_below_threshold(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 90, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.set_disabled_status = Mock(return_value=True)

        result = self.maintainer.process_token({"name": "t3"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.set_disabled_status.assert_not_called()
        self.assertEqual(self.maintainer.stats.enabled, 0)

    def test_settings_accepts_quota_reset_none_recheck_seconds(self):
        settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
            quota_reset_none_recheck_seconds=18000,
        )

        self.assertEqual(settings.quota_reset_none_recheck_seconds, 18000)

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
        coordinator.acquire_next("timer")
        coordinator.release("timer")
        self.assertTrue(coordinator.can_start("timer"))
        coordinator.acquire_next("timer")
        coordinator.release("timer")
        self.assertTrue(coordinator.can_start("log"))

    def test_priority_coordinator_acquire_next_consumes_pending_request_without_deadlock(self):
        coordinator = PriorityCoordinator()
        completed = []

        def worker():
            coordinator.acquire_next("full")
            completed.append(True)
            coordinator.release("full")

        coordinator.request("full")
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(completed, [True])

    def test_tracked_next_check_lookup_reads_saved_value(self):
        self.maintainer._tracked_disabled_accounts = {"token-a": {"next_check_at": 123}}

        self.assertEqual(self.maintainer._get_tracked_next_check_at("token-a"), 123)
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("missing"))

    def test_format_tracked_next_check_at_converts_seconds_timestamp_to_utc8_time(self):
        self.assertEqual(self.maintainer._format_tracked_next_check_at(0), "1970-01-01 08:00:00")

    @patch("src.maintainer.threading.Timer")
    def test_set_tracked_next_check_at_arms_recheck_timer(self, timer_cls):
        timer = timer_cls.return_value

        with patch("src.maintainer.time.time", return_value=1000):
            self.maintainer._set_tracked_next_check_at("token-a", 1050)

        timer_cls.assert_called_once_with(50, self.maintainer._run_tracked_recheck, args=("token-a",))
        self.assertTrue(timer.daemon)
        timer.start.assert_called_once()

    @patch("src.maintainer.threading.Timer")
    def test_start_tracked_rechecks_loads_state_file_and_arms_due_and_future_timers(self, timer_cls):
        state_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        state_path.write_text(
            '{"token-due": {"next_check_at": 900}, "token-future": {"next_check_at": 1100}}',
            encoding="utf-8",
        )
        self.maintainer.disabled_accounts_path = state_path
        self.maintainer._tracked_disabled_accounts = {}

        with patch("src.maintainer.time.time", return_value=1000):
            self.maintainer._start_tracked_rechecks()

        self.assertEqual(
            self.maintainer._tracked_disabled_accounts,
            {"token-due": {"next_check_at": 900}, "token-future": {"next_check_at": 1100}},
        )
        self.assertEqual(timer_cls.call_count, 2)
        self.assertEqual(timer_cls.call_args_list[0].args[0], 0)
        self.assertEqual(timer_cls.call_args_list[1].args[0], 100)

    @patch("src.maintainer.threading.Timer")
    def test_set_tracked_next_check_at_caps_timer_delay_at_timeout_max(self, timer_cls):
        with patch("src.maintainer.time.time", return_value=0):
            self.maintainer._set_tracked_next_check_at("token-long", int(threading.TIMEOUT_MAX) + 100)

        self.assertEqual(timer_cls.call_args.args[0], int(threading.TIMEOUT_MAX))

    def test_extract_usage_detail_entries_returns_source_and_timestamp_pairs(self):
        details = self.maintainer._extract_usage_detail_entries({
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "user-a@example.com", "timestamp": "2026-04-19T22:33:34+08:00"},
                                    {"source": "user-b@example.com", "timestamp": "2026-04-19T22:40:00+08:00"},
                                ]
                            }
                        }
                    }
                }
            }
        })

        self.assertEqual(
            details,
            [
                ("user-a@example.com", "2026-04-19T22:33:34+08:00"),
                ("user-b@example.com", "2026-04-19T22:40:00+08:00"),
            ],
        )

    def test_compute_next_check_at_uses_usage_log_window_when_threshold_resets_are_missing(self):
        body_info = {
            "primary_used_percent": 100,
            "primary_reset_at": None,
            "primary_window_seconds": 18000,
            "secondary_used_percent": None,
            "secondary_reset_at": None,
            "secondary_window_seconds": None,
        }
        usage_data = {
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "token@example.com", "timestamp": "2026-04-19T20:00:00+00:00"},
                                    {"source": "other@example.com", "timestamp": "2026-04-19T22:00:00+00:00"},
                                ]
                            }
                        }
                    }
                }
            }
        }

        self.assertEqual(
            self.maintainer._compute_next_check_at_from_usage(
                body_info,
                now=1000,
                fallback_seconds=50,
                usage_data=usage_data,
                token_detail={"email": "token@example.com"},
            ),
            1776646800,
        )

    def test_process_token_uses_reset_none_fallback_when_usage_logs_do_not_match(self):
        self.maintainer.settings.quota_reset_none_recheck_seconds = 3600
        self.maintainer.settings.usage_query_interval_seconds = 7200
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "missing@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "free",
                "rate_limit": {
                    "primary_window": {"used_percent": 100, "limit_window_seconds": 18000, "reset_at": None},
                    "secondary_window": None,
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.get_usage_log = Mock(return_value={
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "other@example.com", "timestamp": "2026-04-19T20:00:00+00:00"}
                                ]
                            }
                        }
                    }
                }
            }
        })
        self.maintainer.set_disabled_status = Mock(return_value=True)

        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.process_token({"name": "t-log-fallback"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.get_usage_log.assert_called_once()
        self.assertEqual(self.maintainer._get_tracked_next_check_at("t-log-fallback"), 4600)

    def test_compute_next_check_at_falls_back_when_threshold_resets_are_missing(self):
        body_info = {
            "primary_used_percent": 100,
            "primary_reset_at": None,
            "secondary_used_percent": None,
            "secondary_reset_at": None,
        }

        self.assertEqual(
            self.maintainer._compute_next_check_at_from_usage(body_info, now=1000, fallback_seconds=50),
            1050,
        )

    def test_process_token_skips_usage_before_tracked_next_check(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer._tracked_disabled_accounts = {"t-skip": {"next_check_at": 2000}}
        self.maintainer.check_token_live = Mock(return_value=(200, {"json": {}}))
        captured_lines = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_lines.append(list(lines)))

        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.process_token({"name": "t-skip"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.check_token_live.assert_not_called()
        self.assertTrue(captured_lines)
        self.assertIn("1970-01-01 08:33:20", "\n".join(captured_lines[0]))

    def test_process_token_checks_usage_for_manually_enabled_tracked_token(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer._tracked_disabled_accounts = {"t-manually-enabled": {"next_check_at": 2000}}
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
            result = self.maintainer.process_token({"name": "t-manually-enabled"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.check_token_live.assert_called_once()

    def test_process_token_schedules_next_check_when_auto_disabling(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
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
        self.maintainer.set_disabled_status = Mock(return_value=True)

        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.process_token({"name": "t-auto"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertEqual(self.maintainer._get_tracked_next_check_at("t-auto"), 1777000096)

    def test_process_token_enables_tracked_disabled_token_when_due_and_below_threshold(self):
        captured_lines = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_lines.append(list(lines)))
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
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

        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.process_token({"name": "t-enable"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.set_disabled_status.assert_called_once_with("t-enable", disabled=False, logger=ANY)
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-enable"))
        emitted = "\n".join(captured_lines[0])
        self.assertIn("账号已重新启用", emitted)

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

    def test_run_tracked_recheck_logs_priority_transitions_when_lower_work_exists(self):
        coordinator = PriorityCoordinator()
        coordinator.request("full")
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        maintainer.process_token = Mock(return_value="alive")
        maintainer.log = Mock()

        maintainer._run_tracked_recheck("t-enable")

        maintainer.log.assert_any_call("INFO", "定时复查已取得最高优先级，开始处理到期账号")
        maintainer.log.assert_any_call("INFO", "定时复查队列已清空，较低优先级任务可以继续执行")

    def test_run_tracked_recheck_does_not_log_resume_when_no_lower_work_exists(self):
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)
        maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        maintainer.process_token = Mock(return_value="alive")
        maintainer.log = Mock()

        maintainer._run_tracked_recheck("t-enable")

        self.assertNotIn(
            call("INFO", "定时复查队列已清空，较低优先级任务可以继续执行"),
            maintainer.log.call_args_list,
        )

    def test_run_tracked_recheck_does_not_log_resume_while_timer_work_still_pending(self):
        coordinator = PriorityCoordinator()
        coordinator.request("full")
        coordinator.request("timer")
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        maintainer.process_token = Mock(return_value="alive")
        maintainer.log = Mock()

        maintainer._run_tracked_recheck("t-enable")

        self.assertNotIn(
            call("INFO", "定时复查队列已清空，较低优先级任务可以继续执行"),
            maintainer.log.call_args_list,
        )

    def test_timer_priority_wraps_tracked_recheck_once_per_due_token(self):
        coordinator = RecordingCoordinator()
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        maintainer.process_token = Mock(return_value="alive")

        maintainer._run_tracked_recheck("t-enable")

        self.assertEqual(
            coordinator.events,
            [("request", "timer"), ("acquire", "timer"), ("release", "timer")],
        )

    def test_run_tracked_recheck_enables_due_token_and_logs(self):
        self.maintainer._tracked_disabled_accounts = {"t-enable": {"next_check_at": 1000}}
        self.maintainer.logger.emit_lines = Mock()
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

        self.maintainer.set_disabled_status.assert_called_once_with("t-enable", disabled=False, logger=ANY)
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-enable"))
        emitted = "\n".join(
            line
            for call in self.maintainer.logger.emit_lines.call_args_list
            for line in call.args[0]
        )
        self.assertIn("到达计划复查时间，开始复查使用额度", emitted)
        self.assertIn("已重新启用", emitted)

    def test_run_tracked_recheck_logs_exceptions(self):
        self.maintainer._tracked_disabled_accounts = {"t-error": {"next_check_at": 1000}}
        self.maintainer.process_token = Mock(side_effect=RuntimeError("boom"))
        self.maintainer.log = Mock()

        self.maintainer._run_tracked_recheck("t-error")

        self.maintainer.log.assert_any_call("ERROR", "账号 t-error 定时复查异常: boom")

    def test_process_token_reschedules_tracked_disabled_token_with_interval_when_reset_missing(self):
        self.maintainer.settings.interval_seconds = 1800
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer._tracked_disabled_accounts = {"t-requeue": {"next_check_at": 1000}}
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
        self.maintainer.set_disabled_status = Mock(return_value=True)

        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.process_token({"name": "t-requeue"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertEqual(self.maintainer._get_tracked_next_check_at("t-requeue"), 2800)

    def test_process_token_keeps_manual_disabled_token_disabled_when_below_threshold(self):
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

        result = self.maintainer.process_token({"name": "t-manual-disabled"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.set_disabled_status.assert_not_called()
        self.assertEqual(self.maintainer.stats.enabled, 0)

    def test_state_load_reads_existing_disabled_account_schedule(self):
        state_path = pathlib.Path(self.temp_dir.name) / "disabled_accounts.json"
        state_path.write_text('{"t-persist": {"next_check_at": 1234}}', encoding="utf-8")
        self.maintainer.disabled_accounts_path = state_path

        loaded = self.maintainer._load_disabled_accounts_state()

        self.assertEqual(loaded, {"t-persist": {"next_check_at": 1234}})

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

    def test_process_token_removes_schedule_entry_when_token_deleted(self):
        self.maintainer._tracked_disabled_accounts = {"t-no-rt": {"next_check_at": 1234}}
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

        result = self.maintainer.process_token({"name": "t-no-rt"}, 1, 1)

        self.assertEqual(result, "dead")
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("t-no-rt"))

    def test_process_token_keeps_disabled_when_primary_quota_still_reaches_threshold(self):
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
                    "primary_window": {"used_percent": 100, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 95, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.set_disabled_status = Mock(return_value=True)

        result = self.maintainer.process_token({"name": "t3-still-disabled"}, 1, 1)

        self.assertEqual(result, "alive")
        self.maintainer.set_disabled_status.assert_not_called()
        self.assertEqual(self.maintainer.stats.enabled, 0)

    def test_process_token_refreshes_disabled_token_when_near_expiry(self):
        captured_lines = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_lines.append(list(lines)))
        self.maintainer.settings.enable_refresh = True
        near_expiry = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": near_expiry,
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 100, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 95, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.try_refresh = Mock(return_value=(True, {
            "access_token": "new-token",
            "refresh_token": "new-rt",
            "expired": "2099-03-01T00:00:00Z",
        }, "刷新成功"))
        self.maintainer.upload_updated_token = Mock(return_value=True)
        self.maintainer.set_disabled_status = Mock(return_value=True)
        result = self.maintainer.process_token({"name": "t4"}, 1, 1)
        self.assertEqual(result, "alive")
        self.maintainer.upload_updated_token.assert_called_once()
        self.maintainer.set_disabled_status.assert_called_once_with("t4", disabled=True, logger=ANY)
        self.assertEqual(self.maintainer.stats.refreshed, 1)
        emitted = "\n".join(captured_lines[0])
        self.assertIn("刷新后继续保持禁用状态", emitted)
        self.assertIn("账号刷新成功，新剩余有效期:", emitted)

    def test_process_token_logs_week_label_when_primary_window_is_weekly(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "free",
                "rate_limit": {
                    "primary_window": {"used_percent": 100, "limit_window_seconds": 604800},
                    "secondary_window": None,
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.set_disabled_status = Mock(return_value=True)
        captured_lines = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_lines.append(list(lines)))

        result = self.maintainer.process_token({"name": "t-week-primary"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertTrue(captured_lines)
        emitted = "\n".join(captured_lines[0])
        self.assertIn("Week: 100%", emitted)
        self.assertIn("Week额度 100% >= 100%，准备禁用", emitted)
        self.assertNotIn("5h: 100%", emitted)

    def test_process_token_does_not_refresh_when_refresh_disabled(self):
        settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
            enable_refresh=False,
        )
        maintainer = CPACodexKeeper(settings=settings, dry_run=True)
        near_expiry = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": near_expiry,
        })
        maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 0, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        maintainer.try_refresh = Mock(return_value=(True, {
            "access_token": "new-token",
            "refresh_token": "new-rt",
            "expired": "2099-03-01T00:00:00Z",
        }, "刷新成功"))
        maintainer.set_disabled_status = Mock(return_value=True)
        maintainer.upload_updated_token = Mock(return_value=True)

        result = maintainer.process_token({"name": "t4-disabled"}, 1, 1)

        self.assertEqual(result, "alive")
        maintainer.try_refresh.assert_not_called()
        maintainer.upload_updated_token.assert_not_called()
        self.assertEqual(maintainer.stats.refreshed, 0)

    def test_process_token_does_not_refresh_enabled_token_even_when_refresh_enabled(self):
        settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
            enable_refresh=True,
        )
        maintainer = CPACodexKeeper(settings=settings, dry_run=True)
        near_expiry = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": near_expiry,
        })
        maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 0, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        maintainer.try_refresh = Mock(return_value=(True, {
            "access_token": "new-token",
            "refresh_token": "new-rt",
            "expired": "2099-03-01T00:00:00Z",
        }, "刷新成功"))
        maintainer.set_disabled_status = Mock(return_value=True)
        maintainer.upload_updated_token = Mock(return_value=True)

        result = maintainer.process_token({"name": "t4-enabled"}, 1, 1)

        self.assertEqual(result, "alive")
        maintainer.try_refresh.assert_not_called()
        maintainer.upload_updated_token.assert_not_called()
        self.assertEqual(maintainer.stats.refreshed, 0)

    def test_process_token_does_not_refresh_token_reenabled_by_tracked_quota_policy(self):
        settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
            enable_refresh=True,
        )
        maintainer = CPACodexKeeper(settings=settings, dry_run=True)
        maintainer.disabled_accounts_path = pathlib.Path(self.temp_dir.name) / "tracked-disabled-refresh.json"
        maintainer._tracked_disabled_accounts = {"t4-enabled-disabled": {"next_check_at": 1000}}
        near_expiry = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": True,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": near_expiry,
        })
        maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 0, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))
        maintainer.try_refresh = Mock(return_value=(True, {
            "access_token": "new-token",
            "refresh_token": "new-rt",
            "expired": "2099-03-01T00:00:00Z",
        }, "刷新成功"))
        maintainer.set_disabled_status = Mock(return_value=True)
        maintainer.upload_updated_token = Mock(return_value=True)

        with patch("src.maintainer.time.time", return_value=1000):
            result = maintainer.process_token({"name": "t4-enabled-disabled"}, 1, 1)

        self.assertEqual(result, "alive")
        maintainer.set_disabled_status.assert_called_once()
        args, kwargs = maintainer.set_disabled_status.call_args
        self.assertEqual(args, ("t4-enabled-disabled",))
        self.assertEqual(kwargs["disabled"], False)
        maintainer.try_refresh.assert_not_called()
        maintainer.upload_updated_token.assert_not_called()
        self.assertEqual(maintainer.stats.refreshed, 0)

    def test_log_startup_includes_usage_query_interval(self):
        captured = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured.extend(lines))

        self.maintainer.log_startup()

        joined = "\n".join(captured)
        self.assertRegex(joined, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]: 启动配置")
        self.assertIn(f"CPA 接口: {self.settings.cpa_endpoint}", joined)
        self.assertIn(f"配额阈值: {self.settings.quota_threshold}%", joined)
        self.assertIn(f"主巡检间隔: {self.settings.interval_seconds} 秒", joined)
        self.assertIn(f"日志巡检间隔: {self.settings.usage_query_interval_seconds} 秒", joined)
        self.assertIn(f"主巡检线程数: {self.settings.worker_threads}", joined)
        self.assertIn("][DRY-RUN]: 运行模式: 演练模式（不实际修改）", joined)

    def test_log_startup_marks_usage_query_interval_disabled_when_zero(self):
        self.maintainer.settings.usage_query_interval_seconds = 0
        captured = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured.extend(lines))

        self.maintainer.log_startup()

        joined = "\n".join(captured)
        self.assertIn("日志巡检间隔: 已禁用（CPA_USAGE_QUERY_INTERVAL=0）", joined)

    def test_log_startup_includes_delete_guard_setting(self):
        self.maintainer.settings.allow_delete = False
        captured = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured.extend(lines))

        self.maintainer.log_startup()

        joined = "\n".join(captured)
        self.assertIn("允许删除账号文件: 关闭", joined)

    def test_fill_mode_primes_query_time_without_requesting_usage_on_first_run(self):
        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.run_fill_once()

        self.assertEqual(result, "primed")
        self.assertEqual(self.maintainer.last_usage_query_time, 1000)

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

    def test_log_priority_is_requested_between_fill_tokens(self):
        coordinator = RecordingCoordinator()
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

        self.assertEqual(
            coordinator.events,
            [
                ("request", "log"), ("acquire", "log"), ("release", "log"),
                ("request", "log"), ("acquire", "log"), ("release", "log"),
            ],
        )

    def test_run_fill_once_logs_pause_when_timer_priority_is_waiting(self):
        coordinator = Mock()
        coordinator.blocking_priority.return_value = "timer"
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.last_usage_query_time = 1000
        maintainer.cpa_client.list_auth_files = Mock(return_value=[
            {"name": "token-a", "type": "codex", "email": "a@example.com"},
        ])
        maintainer.get_usage_log = Mock(return_value={
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "a@example.com", "timestamp": "1970-01-01T00:17:40+00:00"},
                                ]
                            }
                        }
                    }
                }
            }
        })
        maintainer.process_fill_token = Mock(return_value="alive")
        maintainer.log = Mock()

        with patch("src.maintainer.time.time", return_value=1100):
            maintainer.run_fill_once()

        maintainer.log.assert_any_call("INFO", "优先级协调：定时复查正在等待，日志巡检将在当前 Token 完成后暂停")

    def test_fill_mode_processes_new_usage_emails_and_disables_threshold_accounts(self):
        self.maintainer.last_usage_query_time = 1000
        self.maintainer.cpa_client.list_auth_files = Mock(return_value=[
            {"name": "token-a", "type": "codex", "email": "a@example.com"},
            {"name": "token-b", "type": "codex", "email": "b@example.com"},
            {"name": "token-c", "type": "oauth", "email": "c@example.com"},
        ])
        self.maintainer.get_usage_log = Mock(return_value={
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "a@example.com", "timestamp": "1970-01-01T00:16:50+00:00"},
                                    {"source": "a@example.com", "timestamp": "1970-01-01T00:17:40+00:00"},
                                    {"source": "b@example.com", "timestamp": "1970-01-01T00:17:20+00:00"},
                                    {"source": "old@example.com", "timestamp": "1970-01-01T00:15:00+00:00"},
                                ]
                            }
                        }
                    }
                }
            }
        })
        self.maintainer.process_fill_token = Mock(side_effect=["disabled", "alive"])

        with patch("src.maintainer.time.time", return_value=1100):
            result = self.maintainer.run_fill_once()

        self.assertEqual(result, "processed")
        self.assertEqual(self.maintainer.last_usage_query_time, 1100)
        self.maintainer.process_fill_token.assert_any_call({"name": "token-a", "type": "codex", "email": "a@example.com"}, 1, 2)
        self.maintainer.process_fill_token.assert_any_call({"name": "token-b", "type": "codex", "email": "b@example.com"}, 2, 2)
        self.assertEqual(self.maintainer.process_fill_token.call_count, 2)

    def test_fill_mode_processes_all_tokens_sharing_same_email(self):
        self.maintainer.last_usage_query_time = 1000
        self.maintainer.cpa_client.list_auth_files = Mock(return_value=[
            {"name": "token-a1", "type": "codex", "email": "dup@example.com"},
            {"name": "token-a2", "type": "codex", "email": "dup@example.com"},
        ])
        self.maintainer.get_usage_log = Mock(return_value={
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "dup@example.com", "timestamp": "1970-01-01T00:17:40+00:00"},
                                ]
                            }
                        }
                    }
                }
            }
        })
        self.maintainer.process_fill_token = Mock(side_effect=["alive", "disabled"])

        with patch("src.maintainer.time.time", return_value=1100):
            result = self.maintainer.run_fill_once()

        self.assertEqual(result, "processed")
        self.assertEqual(self.maintainer.process_fill_token.call_count, 2)
        self.maintainer.process_fill_token.assert_any_call(
            {"name": "token-a1", "type": "codex", "email": "dup@example.com"},
            1,
            2,
        )
        self.maintainer.process_fill_token.assert_any_call(
            {"name": "token-a2", "type": "codex", "email": "dup@example.com"},
            2,
            2,
        )

    def test_fill_mode_filters_usage_emails_against_filtered_auth_files(self):
        self.maintainer.last_usage_query_time = 1000
        self.maintainer.cpa_client.list_auth_files = Mock(return_value=[
            {"name": "token-keep", "type": "codex", "email": "keep@example.com"},
            {"name": "token-oauth", "type": "oauth", "email": "oauth@example.com"},
            {"name": "token-missing-type", "email": "ghost@example.com"},
        ])
        self.maintainer.get_usage_log = Mock(return_value={
            "usage": {
                "apis": {
                    "api-1": {
                        "models": {
                            "gpt-5.3-codex": {
                                "details": [
                                    {"source": "keep@example.com", "timestamp": "1970-01-01T00:17:40+00:00"},
                                    {"source": "oauth@example.com", "timestamp": "1970-01-01T00:17:50+00:00"},
                                    {"source": "ghost@example.com", "timestamp": "1970-01-01T00:17:55+00:00"},
                                ]
                            }
                        }
                    }
                }
            }
        })
        self.maintainer.process_fill_token = Mock(return_value="alive")

        with patch("src.maintainer.time.time", return_value=1100):
            result = self.maintainer.run_fill_once()

        self.assertEqual(result, "processed")
        self.assertEqual(self.maintainer.last_usage_query_time, 1100)
        self.maintainer.process_fill_token.assert_called_once_with(
            {"name": "token-keep", "type": "codex", "email": "keep@example.com"},
            1,
            1,
        )

    def test_fill_mode_skips_delete_flow_for_401_and_402(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(401, {"brief": "unauthorized"}))
        self.maintainer.delete_token = Mock(return_value=True)

        result = self.maintainer.process_fill_token({"name": "t-fill"}, 1, 1)

        self.assertEqual(result, "skipped")
        self.maintainer.delete_token.assert_not_called()

    def test_fill_mode_skips_usage_query_when_interval_disabled(self):
        self.maintainer.settings.usage_query_interval_seconds = 0
        self.maintainer.last_usage_query_time = 1000
        self.maintainer.get_usage_log = Mock(return_value={"usage": {}})

        with patch("src.maintainer.time.time", return_value=1100):
            result = self.maintainer.run_fill_once()

        self.assertEqual(result, "disabled")
        self.assertEqual(self.maintainer.last_usage_query_time, 1000)
        self.maintainer.get_usage_log.assert_not_called()

    def test_run_fill_forever_logs_next_poll_interval(self):
        messages = []
        self.maintainer.log = Mock(side_effect=lambda level, message, indent=0: messages.append((level, message, indent)))
        self.maintainer.run_fill_once = Mock(return_value="processed")

        def stop_after_first_sleep(_seconds):
            raise KeyboardInterrupt

        with patch("src.maintainer.time.sleep", side_effect=stop_after_first_sleep):
            with self.assertRaises(KeyboardInterrupt):
                self.maintainer.run_fill_forever(interval_seconds=10)

        self.assertIn(("INFO", "日志巡检守护进程已启动（轮询间隔: 10 秒）", 0), messages)
        self.assertIn(("INFO", "日志巡检第 1 轮开始：准备扫描新增CPA使用日志", 0), messages)
        self.assertIn(("INFO", "日志巡检第 1 轮结束：已完成本轮CPA使用日志扫描", 0), messages)
        self.assertIn(("INFO", "日志巡检休眠中：10 秒后开始下一轮CPA使用日志扫描", 0), messages)

    def test_process_token_deletes_expired_token_without_refresh_token(self):
        captured_lines = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_lines.append(list(lines)))
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "",
            "account_id": "acc",
            "expired": "2000-01-01T00:00:00Z",
        })
        self.maintainer.delete_token = Mock(return_value=True)
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

        result = self.maintainer.process_token({"name": "t-expired"}, 1, 1)

        self.assertEqual(result, "dead")
        self.assertEqual(self.maintainer.stats.dead, 1)
        self.maintainer.check_token_live.assert_not_called()
        args, kwargs = self.maintainer.delete_token.call_args
        self.assertEqual(args, ("t-expired",))
        self.assertIn("logger", kwargs)
        emitted = "\n".join(captured_lines[0])
        self.assertIn("账号文件已删除", emitted)

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

    def test_process_token_deletes_quota_exhausted_token_without_refresh_token(self):
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
                    "primary_window": {"used_percent": 100, "limit_window_seconds": 604800},
                    "secondary_window": None,
                },
                "credits": {"has_credits": False},
            }
        }))
        self.maintainer.delete_token = Mock(return_value=True)
        self.maintainer.set_disabled_status = Mock(return_value=True)

        result = self.maintainer.process_token({"name": "t-no-rt"}, 1, 1)

        self.assertEqual(result, "dead")
        self.assertEqual(self.maintainer.stats.dead, 1)
        self.maintainer.set_disabled_status.assert_not_called()
        args, kwargs = self.maintainer.delete_token.call_args
        self.assertEqual(args, ("t-no-rt",))
        self.assertIn("logger", kwargs)

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

    def test_process_token_keeps_non_refreshable_token_when_expiry_is_unknown(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "not-a-jwt",
            "refresh_token": "",
            "account_id": "acc",
            "expired": "",
        })
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
        self.maintainer.delete_token = Mock(return_value=True)

        result = self.maintainer.process_token({"name": "t-unknown-expiry"}, 1, 1)

        self.assertEqual(result, "alive")
        self.assertEqual(self.maintainer.stats.alive, 1)
        self.maintainer.delete_token.assert_not_called()
        self.maintainer.check_token_live.assert_called_once()

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_full_inspection_workers_request_full_priority_per_token(self, executor_cls, _shuffle_mock):
        coordinator = Mock()
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.get_token_list = Mock(return_value=[{"name": "a"}, {"name": "b"}])
        maintainer.log_startup = Mock()
        maintainer.process_token = Mock(return_value="alive")

        class InlineExecutor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def submit(self, fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

        executor_cls.return_value = InlineExecutor()

        maintainer.run()

        self.assertEqual(coordinator.request.call_count, 2)
        self.assertEqual(coordinator.acquire_next.call_count, 2)
        self.assertEqual(coordinator.release.call_count, 2)
        self.assertEqual(maintainer.process_token.call_count, 2)

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_full_priority_is_requested_between_full_tokens(self, executor_cls, _shuffle_mock):
        coordinator = RecordingCoordinator()
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.get_token_list = Mock(return_value=[{"name": "a"}, {"name": "b"}])
        maintainer.log_startup = Mock()
        maintainer.process_token = Mock(return_value="alive")

        class InlineExecutor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def submit(self, fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

        executor_cls.return_value = InlineExecutor()

        maintainer.run()

        self.assertEqual(
            coordinator.events,
            [
                ("request", "full"), ("acquire", "full"), ("release", "full"),
                ("request", "full"), ("acquire", "full"), ("release", "full"),
            ],
        )

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_logs_pause_when_log_priority_is_waiting(self, executor_cls, _shuffle_mock):
        coordinator = Mock()
        coordinator.blocking_priority.return_value = "log"
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.get_token_list = Mock(return_value=[{"name": "a"}])
        maintainer.log_startup = Mock()
        maintainer.process_token = Mock(return_value="alive")
        maintainer.log = Mock()

        class InlineExecutor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def submit(self, fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

        executor_cls.return_value = InlineExecutor()

        maintainer.run()

        maintainer.log.assert_any_call("INFO", "优先级协调：日志巡检正在等待，主巡检将在当前 Token 完成后暂停")

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_full_inspection_releases_priority_when_process_token_raises(self, executor_cls, _shuffle_mock):
        coordinator = Mock()
        maintainer = CPACodexKeeper(settings=self.settings, dry_run=True, coordinator=coordinator)
        maintainer.get_token_list = Mock(return_value=[{"name": "a"}])
        maintainer.log_startup = Mock()
        maintainer.log = Mock()
        maintainer.process_token = Mock(side_effect=RuntimeError("boom"))

        class InlineExecutor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def submit(self, fn, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(fn(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                return future

        inline_executor = InlineExecutor()
        future = inline_executor.submit(lambda: None)

        self.assertTrue(future.done())

        executor_cls.return_value = inline_executor

        maintainer.run()

        coordinator.release.assert_called_once_with("full")

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.as_completed")
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_uses_configured_worker_threads_and_processes_all_tokens(self, executor_cls, as_completed_mock, _shuffle_mock):
        tokens = [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}]
        self.maintainer.settings.worker_threads = 6
        self.maintainer.get_token_list = Mock(return_value=tokens)
        self.maintainer.log_startup = Mock()

        futures = []

        def submit_side_effect(fn):
            future = Future()
            future.set_result(fn())
            futures.append(future)
            return future

        executor = executor_cls.return_value.__enter__.return_value
        executor.submit.side_effect = submit_side_effect
        as_completed_mock.side_effect = lambda items: list(items)
        self.maintainer.process_token = Mock(side_effect=["alive", "alive", "alive"])

        self.maintainer.run()

        executor_cls.assert_called_once_with(max_workers=6)
        self.assertEqual(executor.submit.call_count, 3)
        self.maintainer.process_token.assert_any_call({"name": "t1"}, 1, 3)
        self.maintainer.process_token.assert_any_call({"name": "t2"}, 2, 3)
        self.maintainer.process_token.assert_any_call({"name": "t3"}, 3, 3)

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.as_completed")
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_logs_task_exception_and_continues(self, executor_cls, as_completed_mock, _shuffle_mock):
        tokens = [{"name": "ok-1"}, {"name": "boom"}, {"name": "ok-2"}]
        self.maintainer.get_token_list = Mock(return_value=tokens)
        self.maintainer.log_startup = Mock()
        self.maintainer.log = Mock()

        futures = []

        def submit_side_effect(fn):
            future = Future()
            future.set_result(fn())
            futures.append(future)
            return future

        executor = executor_cls.return_value.__enter__.return_value
        executor.submit.side_effect = submit_side_effect
        as_completed_mock.side_effect = lambda items: list(items)

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

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.as_completed")
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_preserves_total_stat_with_threaded_execution(self, executor_cls, as_completed_mock, _shuffle_mock):
        tokens = [{"name": "t1"}, {"name": "t2"}]
        self.maintainer.get_token_list = Mock(return_value=tokens)
        self.maintainer.log_startup = Mock()

        def submit_side_effect(fn):
            future = Future()
            future.set_result(fn())
            return future

        executor = executor_cls.return_value.__enter__.return_value
        executor.submit.side_effect = submit_side_effect
        as_completed_mock.side_effect = lambda items: list(items)

        def process_side_effect(token_info, idx, total):
            if token_info["name"] == "t1":
                self.maintainer.stats.alive += 1
            else:
                self.maintainer.stats.skipped += 1
            return token_info["name"]

        self.maintainer.process_token = Mock(side_effect=process_side_effect)

        self.maintainer.run()

        self.assertEqual(self.maintainer.stats.total, 2)
        self.assertEqual(self.maintainer.stats.alive, 1)
        self.assertEqual(self.maintainer.stats.skipped, 1)

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.as_completed")
    @patch("src.maintainer.ThreadPoolExecutor")
    @patch("src.maintainer.time.time", side_effect=[100.0, 101.2])
    def test_run_emits_detailed_summary(self, _time_mock, executor_cls, as_completed_mock, _shuffle_mock):
        tokens = [{"name": "t1"}, {"name": "t2"}]
        self.maintainer.get_token_list = Mock(return_value=tokens)
        self.maintainer.log_startup = Mock()
        emitted_batches = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: emitted_batches.append(list(lines)))

        def submit_side_effect(fn):
            future = Future()
            future.set_result(fn())
            return future

        executor = executor_cls.return_value.__enter__.return_value
        executor.submit.side_effect = submit_side_effect
        as_completed_mock.side_effect = lambda items: list(items)

        def process_side_effect(token_info, idx, total):
            if token_info["name"] == "t1":
                self.maintainer.stats.alive += 1
            else:
                self.maintainer.stats.disabled += 1
            return "alive"

        self.maintainer.process_token = Mock(side_effect=process_side_effect)

        self.maintainer.run()

        summary = emitted_batches[-1]
        self.assertRegex(summary[0], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]: ={60}")
        self.assertTrue(any("执行总结" in line and "][INFO]:" in line for line in summary))
        self.assertTrue(any("总耗时: 1.2 秒" in line and "][INFO]:" in line for line in summary))
        self.assertTrue(any("Token 总数: 2" in line and "][INFO]:" in line for line in summary))
        self.assertTrue(any("工作线程: 8" in line and "][INFO]:" in line for line in summary))
        self.assertTrue(any("状态统计" in line and "][INFO]:" in line for line in summary))
        self.assertTrue(any("存活: 1" in line and "][OK]:" in line for line in summary))
        self.assertTrue(any("已禁用: 1" in line and "][DISABLED]:" in line for line in summary))
        self.assertTrue(any("死号(已删除): 0" in line and "][DELETE]:" in line for line in summary))
        self.assertTrue(any("其他统计" in line and "][INFO]:" in line for line in summary))
        self.assertTrue(any("跳过: 0" in line and "][SKIP]:" in line for line in summary))
        self.assertTrue(any("网络失败: 0" in line and "][ERROR]:" in line for line in summary))
        self.assertRegex(summary[-1], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]: ={60}")

    @patch("src.maintainer.random.shuffle", side_effect=lambda seq: None)
    @patch("src.maintainer.as_completed")
    @patch("src.maintainer.ThreadPoolExecutor")
    @patch("src.maintainer.time.time", side_effect=[1000, 1001.2])
    def test_run_summary_counts_delete_blocked_fallback_as_disabled_not_dead(self, _time_mock, executor_cls, as_completed_mock, _shuffle_mock):
        emitted_batches = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: emitted_batches.append(list(lines)))
        self.maintainer.settings.allow_delete = False
        self.maintainer.get_token_list = Mock(return_value=[{"name": "token-a", "type": "codex"}])
        self.maintainer.log_startup = Mock()

        def submit_side_effect(fn):
            future = Future()
            future.set_result(fn())
            return future

        executor = executor_cls.return_value.__enter__.return_value
        executor.submit.side_effect = submit_side_effect
        as_completed_mock.side_effect = lambda items: list(items)

        def process_side_effect(*_args, **_kwargs):
            self.maintainer.stats.disabled += 1
            return "alive"

        self.maintainer.process_token = Mock(side_effect=process_side_effect)

        self.maintainer.run()

        summary = emitted_batches[-1]
        self.assertTrue(any("已禁用: 1" in line and "][DISABLED]:" in line for line in summary))
        self.assertTrue(any("死号(已删除): 0" in line and "][DELETE]:" in line for line in summary))

    def test_token_logger_emits_detailed_multiline_token_block(self):
        captured_batches = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured_batches.append(list(lines)))
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": "2099-01-01T00:00:00Z",
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 12, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 34, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": False},
            }
        }))

        result = self.maintainer.process_token({"name": "token-a"}, 1, 1)

        self.assertEqual(result, "alive")
        token_block = captured_batches[-1]
        self.assertRegex(token_block[0], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]: \[1/1\] Token: token-a")
        joined = "\n".join(token_block)
        self.assertIn("步骤: 获取账号详情", joined)
        self.assertIn("账号邮箱: a@example.com", joined)
        self.assertIn("当前状态: 正常", joined)
        self.assertIn("过期时间: 2099-01-01T00:00:00Z", joined)
        self.assertIn("剩余有效期:", joined)
        self.assertIn("步骤: 检查在线状态与额度", joined)
        self.assertIn("在线状态: 正常 | 套餐: team | 5h: 12% | Week: 34% | Credits: False", joined)

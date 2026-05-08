import pathlib
import sys
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.maintainer import CPACodexKeeper
from src.openai_client import parse_usage_info
from src.settings import Settings


class MaintainerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
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

    def test_process_token_disables_when_weekly_quota_reaches_threshold(self):
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

    def test_tracked_next_check_lookup_reads_saved_value(self):
        self.maintainer._tracked_disabled_accounts = {"token-a": {"next_check_at": 123}}

        self.assertEqual(self.maintainer._get_tracked_next_check_at("token-a"), 123)
        self.assertIsNone(self.maintainer._get_tracked_next_check_at("missing"))

    def test_format_tracked_next_check_at_converts_seconds_timestamp_to_utc8_time(self):
        self.assertEqual(self.maintainer._format_tracked_next_check_at(0), "1970-01-01 08:00:00")

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
        self.assertIn(f"Usage query interval: {self.settings.usage_query_interval_seconds} seconds", joined)

    def test_log_startup_marks_usage_query_interval_disabled_when_zero(self):
        self.maintainer.settings.usage_query_interval_seconds = 0
        captured = []
        self.maintainer.logger.emit_lines = Mock(side_effect=lambda lines: captured.extend(lines))

        self.maintainer.log_startup()

        joined = "\n".join(captured)
        self.assertIn("Usage query interval: disabled", joined)

    def test_fill_mode_primes_query_time_without_requesting_usage_on_first_run(self):
        with patch("src.maintainer.time.time", return_value=1000):
            result = self.maintainer.run_fill_once()

        self.assertEqual(result, "primed")
        self.assertEqual(self.maintainer.last_usage_query_time, 1000)

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

        self.assertIn(("INFO", "日志 巡检模式启动，执行间隔: 10 秒", 0), messages)
        self.assertIn(("INFO", "开始第 1 轮 日志 巡检", 0), messages)
        self.assertIn(("INFO", "第 1 轮 日志 巡检结束", 0), messages)
        self.assertIn(("INFO", "等待 10 秒后开始下一轮 日志 巡检", 0), messages)

    def test_process_token_deletes_expired_token_without_refresh_token(self):
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
    @patch("src.maintainer.as_completed")
    @patch("src.maintainer.ThreadPoolExecutor")
    def test_run_uses_configured_worker_threads_and_processes_all_tokens(self, executor_cls, as_completed_mock, _shuffle_mock):
        tokens = [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}]
        self.maintainer.settings.worker_threads = 6
        self.maintainer.get_token_list = Mock(return_value=tokens)
        self.maintainer.log_startup = Mock()

        futures = []

        def submit_side_effect(fn, token_info, idx, total):
            future = Future()
            future.set_result(fn(token_info, idx, total))
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

        def submit_side_effect(fn, token_info, idx, total):
            future = Future()
            try:
                future.set_result(fn(token_info, idx, total))
            except Exception as exc:
                future.set_exception(exc)
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

        def submit_side_effect(fn, token_info, idx, total):
            future = Future()
            future.set_result(fn(token_info, idx, total))
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
    def test_run_logs_configured_worker_threads(self, executor_cls, as_completed_mock, _shuffle_mock):
        tokens = [{"name": "t1"}]
        self.maintainer.settings.worker_threads = 5
        self.maintainer.get_token_list = Mock(return_value=tokens)
        self.maintainer.log_startup = Mock()
        self.maintainer.log = Mock()

        def submit_side_effect(fn, token_info, idx, total):
            future = Future()
            future.set_result(fn(token_info, idx, total))
            return future

        executor = executor_cls.return_value.__enter__.return_value
        executor.submit.side_effect = submit_side_effect
        as_completed_mock.side_effect = lambda items: list(items)
        self.maintainer.process_token = Mock(return_value="alive")

        self.maintainer.run()

        self.maintainer.log.assert_any_call("INFO", "线程数: 5")

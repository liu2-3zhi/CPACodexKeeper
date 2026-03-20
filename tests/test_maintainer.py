import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.maintainer import CPACodexKeeper
from src.openai_client import parse_usage_info
from src.settings import Settings


class MaintainerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            quota_threshold=100,
            expiry_threshold_days=3,
        )
        self.maintainer = CPACodexKeeper(settings=self.settings, dry_run=True)

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
        self.assertEqual(usage.quota_check_label, "week")

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
        self.assertEqual(usage.quota_check_label, "5h")

    def test_process_token_deletes_invalid_token_on_401(self):
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
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
        self.maintainer.set_disabled_status.assert_called_once_with("t2", disabled=True)
        self.assertEqual(self.maintainer.stats.disabled, 1)

    def test_process_token_enables_when_disabled_and_weekly_quota_below_threshold(self):
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
        self.maintainer.set_disabled_status.assert_called_once_with("t3", disabled=False)
        self.assertEqual(self.maintainer.stats.enabled, 1)

    def test_process_token_refreshes_when_near_expiry(self):
        near_expiry = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.maintainer.get_token_detail = Mock(return_value={
            "email": "a@example.com",
            "disabled": False,
            "access_token": "token",
            "refresh_token": "rt",
            "account_id": "acc",
            "expired": near_expiry,
        })
        self.maintainer.check_token_live = Mock(return_value=(200, {
            "json": {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {"used_percent": 0, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800},
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
        result = self.maintainer.process_token({"name": "t4"}, 1, 1)
        self.assertEqual(result, "alive")
        self.maintainer.upload_updated_token.assert_called_once()
        self.assertEqual(self.maintainer.stats.refreshed, 1)

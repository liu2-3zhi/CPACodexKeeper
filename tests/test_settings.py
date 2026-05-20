import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.settings import SettingsError, load_settings


class SettingsTests(unittest.TestCase):
    def _make_env_file(self, content: str) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        env_path = Path(temp_dir.name) / ".env"
        env_path.write_text(content, encoding="utf-8")
        return env_path

    def test_load_settings_reads_required_values(self):
        with patch.dict(os.environ, {"CPA_ENDPOINT": "https://example.com", "CPA_TOKEN": "secret"}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.cpa_endpoint, "https://example.com")
        self.assertEqual(settings.cpa_token, "secret")
        self.assertEqual(settings.interval_seconds, 1800)
        self.assertTrue(settings.enable_refresh)

    def test_load_settings_reads_from_project_env_file(self):
        env_file = self._make_env_file("CPA_ENDPOINT=https://env-file.example.com\nCPA_TOKEN=file-secret\nCPA_INTERVAL=120\n")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(env_file=env_file)
        self.assertEqual(settings.cpa_endpoint, "https://env-file.example.com")
        self.assertEqual(settings.cpa_token, "file-secret")
        self.assertEqual(settings.interval_seconds, 120)

    def test_environment_variables_override_project_env_file(self):
        env_file = self._make_env_file("CPA_ENDPOINT=https://env-file.example.com\nCPA_TOKEN=file-secret\n")
        with patch.dict(os.environ, {"CPA_ENDPOINT": "https://shell.example.com", "CPA_TOKEN": "shell-secret"}, clear=True):
            settings = load_settings(env_file=env_file)
        self.assertEqual(settings.cpa_endpoint, "https://shell.example.com")
        self.assertEqual(settings.cpa_token, "shell-secret")

    def test_load_settings_rejects_missing_endpoint(self):
        env_file = Path("does-not-exist.env")
        with patch.dict(os.environ, {"CPA_TOKEN": "secret"}, clear=True):
            with self.assertRaises(SettingsError):
                load_settings(env_file=env_file)

    def test_load_settings_rejects_bad_integer(self):
        env_file = Path("does-not-exist.env")
        with patch.dict(os.environ, {"CPA_ENDPOINT": "https://example.com", "CPA_TOKEN": "secret", "CPA_INTERVAL": "abc"}, clear=True):
            with self.assertRaises(SettingsError):
                load_settings(env_file=env_file)

    def test_load_settings_reads_fill_interval_zero(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_FILL_INTERVAL": "0",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.fill_interval_seconds, 0)

    def test_load_settings_uses_default_full_scan_interval_bounds(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.full_scan_min_interval_seconds, 10)
        self.assertEqual(settings.full_scan_max_interval_seconds, 60)

    def test_load_settings_reads_full_scan_interval_bounds(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_FULL_SCAN_MIN_INTERVAL_SECONDS": "15",
                "CPA_FULL_SCAN_MAX_INTERVAL_SECONDS": "45",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.full_scan_min_interval_seconds, 15)
        self.assertEqual(settings.full_scan_max_interval_seconds, 45)

    def test_load_settings_rejects_inverted_full_scan_interval_bounds(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_FULL_SCAN_MIN_INTERVAL_SECONDS": "50",
                "CPA_FULL_SCAN_MAX_INTERVAL_SECONDS": "20",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()

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

    def test_load_settings_reads_log_archive_max_size_mb(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_LOG_ARCHIVE_MAX_SIZE_MB": "256",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.log_archive_max_size_mb, 256)

    def test_load_settings_uses_default_log_archive_max_size_mb(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.log_archive_max_size_mb, 500)

    def test_load_settings_reads_fill_interval(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_FILL_INTERVAL": "10",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.fill_interval_seconds, 10)

    def test_load_settings_reads_fill_interval_negative_one(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_FILL_INTERVAL": "-1",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.fill_interval_seconds, -1)

    def test_load_settings_reads_allow_delete_false(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_ALLOW_DELETE": "false",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertFalse(settings.allow_delete)

    def test_load_settings_uses_default_allow_delete(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertTrue(settings.allow_delete)

    def test_load_settings_reads_force_refresh_on_expiry_true(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_FORCE_REFRESH_ON_EXPIRY": "true",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertTrue(settings.force_refresh_on_expiry)

    def test_load_settings_uses_default_force_refresh_on_expiry_false(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertFalse(settings.force_refresh_on_expiry)

    def test_load_settings_uses_default_disabled_state_lock_values(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.disabled_state_lock_timeout_seconds, 10.0)
        self.assertEqual(settings.disabled_state_lock_retry_interval_seconds, 0.2)

    def test_load_settings_reads_disabled_state_lock_values(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS": "3.5",
                "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS": "0.05",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.disabled_state_lock_timeout_seconds, 3.5)
        self.assertEqual(settings.disabled_state_lock_retry_interval_seconds, 0.05)

    def test_load_settings_rejects_non_positive_disabled_state_lock_timeout(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()

    def test_load_settings_rejects_non_positive_disabled_state_lock_retry_interval(self):
        with patch.dict(
            os.environ,
            {
                "CPA_ENDPOINT": "https://example.com",
                "CPA_TOKEN": "secret",
                "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(SettingsError):
                load_settings()


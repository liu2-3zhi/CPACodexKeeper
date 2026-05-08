import io
import pathlib
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.cli import build_arg_parser, main
from src.settings import Settings


class CLITests(unittest.TestCase):
    def assert_parser_rejects_args(self, parser, args):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(args)

    def test_defaults_to_daemon_mode(self):
        parser = build_arg_parser()
        args = parser.parse_args([])

        self.assertTrue(args.daemon)

    def test_once_disables_daemon_mode(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--once"])

        self.assertFalse(args.daemon)

    def test_parser_accepts_once_force_refresh(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--once", "--force-refresh"])

        self.assertFalse(args.daemon)
        self.assertTrue(args.force_refresh)

    def test_parser_rejects_force_refresh_without_once(self):
        parser = build_arg_parser()

        self.assert_parser_rejects_args(parser, ["--force-refresh"])

    def test_parser_rejects_monitor_with_force_refresh(self):
        parser = build_arg_parser()

        self.assert_parser_rejects_args(parser, ["-monitor", "--force-refresh"])

    def test_fill_flag_is_not_supported(self):
        parser = build_arg_parser()

        self.assert_parser_rejects_args(parser, ["--fill"])

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

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "--once"])
    def test_main_runs_once_without_starting_fill(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        keeper.run.assert_called_once_with(force_refresh_on_expiry=False)
        keeper.run_forever.assert_not_called()
        thread_cls.assert_not_called()

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "--once", "--force-refresh"])
    def test_main_runs_once_with_force_refresh_override(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        keeper.run.assert_called_once_with(force_refresh_on_expiry=True)
        keeper.run_forever.assert_not_called()
        thread_cls.assert_not_called()

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "--once"])
    def test_main_once_ignores_env_force_refresh_setting_when_flag_absent(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            force_refresh_on_expiry=True,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        keeper.run.assert_called_once_with(force_refresh_on_expiry=False)
        keeper.run_forever.assert_not_called()
        thread_cls.assert_not_called()

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
        main_keeper.logger = object()
        fill_keeper = Mock()
        keeper_cls.side_effect = [main_keeper, fill_keeper]

        main()

        first_kwargs = keeper_cls.call_args_list[0].kwargs
        second_kwargs = keeper_cls.call_args_list[1].kwargs
        self.assertIs(first_kwargs["coordinator"], second_kwargs["coordinator"])

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog"])
    def test_main_shares_one_logger_between_main_and_fill_keepers(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            usage_query_interval_seconds=7200,
        )
        main_keeper = Mock()
        main_keeper.logger = object()
        fill_keeper = Mock()
        keeper_cls.side_effect = [main_keeper, fill_keeper]

        main()

        first_kwargs = keeper_cls.call_args_list[0].kwargs
        second_kwargs = keeper_cls.call_args_list[1].kwargs
        self.assertIsNone(first_kwargs["logger"])
        self.assertIs(second_kwargs["logger"], main_keeper.logger)

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog"])
    def test_main_runs_daemon_and_starts_fill_thread_when_usage_query_enabled(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            fill_interval_seconds=10,
            usage_query_interval_seconds=7200,
        )
        main_keeper = Mock()
        fill_keeper = Mock()
        keeper_cls.side_effect = [main_keeper, fill_keeper]
        thread = thread_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        main_keeper.run_forever.assert_called_once_with(interval_seconds=1800)
        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertEqual(kwargs["target"], fill_keeper.run_fill_forever)
        self.assertEqual(kwargs["kwargs"], {"interval_seconds": 10})
        self.assertTrue(kwargs["daemon"])
        thread.start.assert_called_once()

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "-monitor"])
    def test_main_runs_monitor_mode_with_tracked_rechecks_and_fill_forever(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            fill_interval_seconds=10,
            usage_query_interval_seconds=7200,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(keeper_cls.call_count, 1)
        keeper._start_tracked_rechecks.assert_called_once()
        keeper.run_fill_forever.assert_called_once_with(interval_seconds=10)
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
            fill_interval_seconds=10,
            usage_query_interval_seconds=0,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(keeper_cls.call_count, 1)
        keeper._start_tracked_rechecks.assert_called_once()
        keeper.run_fill_forever.assert_called_once_with(interval_seconds=10)
        keeper.run_forever.assert_not_called()
        keeper.run.assert_not_called()
        thread_cls.assert_not_called()

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog"])
    def test_main_starts_tracked_recheck_timers_before_daemon_loop(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            usage_query_interval_seconds=0,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        keeper._start_tracked_rechecks.assert_called_once()
        keeper.run_forever.assert_called_once_with(interval_seconds=1800)
        thread_cls.assert_not_called()

    @patch("src.cli.threading.Thread")
    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog"])
    def test_main_runs_daemon_without_fill_thread_when_usage_query_disabled(self, keeper_cls, load_settings_mock, thread_cls):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
            usage_query_interval_seconds=0,
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        keeper.run_forever.assert_called_once_with(interval_seconds=1800)
        thread_cls.assert_not_called()

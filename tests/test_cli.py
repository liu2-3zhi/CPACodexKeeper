import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.cli import build_arg_parser, main
from src.settings import Settings


class CLITests(unittest.TestCase):
    def test_defaults_to_daemon_mode(self):
        parser = build_arg_parser()
        args = parser.parse_args([])

        self.assertTrue(args.daemon)

    def test_once_disables_daemon_mode(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--once"])

        self.assertFalse(args.daemon)

    @patch("src.cli.load_settings")
    @patch("src.cli.CPACodexKeeper")
    @patch("sys.argv", ["prog", "--once"])
    def test_main_runs_once(self, keeper_cls, load_settings_mock):
        load_settings_mock.return_value = Settings(
            cpa_endpoint="https://example.com",
            cpa_token="secret",
        )
        keeper = keeper_cls.return_value

        exit_code = main()

        self.assertEqual(exit_code, 0)
        keeper.run.assert_called_once()
        keeper.run_forever.assert_not_called()

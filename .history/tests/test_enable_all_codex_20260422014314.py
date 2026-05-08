import io
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import enable_all_codex


class EnableAllCodexTests(unittest.TestCase):
    def test_resolve_config_uses_built_in_values_without_prompting(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", "https://built-in.example.com"), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", "built-in-token"), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", "http://127.0.0.1:7890"), \
             patch("builtins.input") as input_mock, \
             patch("enable_all_codex.getpass") as getpass_mock:
            endpoint, token, proxy, sources = enable_all_codex.resolve_config()

        self.assertEqual(endpoint, "https://built-in.example.com")
        self.assertEqual(token, "built-in-token")
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertEqual(sources, {
            "endpoint": "built-in",
            "token": "built-in",
            "proxy": "built-in",
        })
        input_mock.assert_not_called()
        getpass_mock.assert_not_called()

    def test_resolve_config_prompts_for_missing_required_values(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", ""), \
             patch("builtins.input", side_effect=["https://prompt.example.com", "http://127.0.0.1:8888"]), \
             patch("enable_all_codex.getpass", return_value="prompt-token"):
            endpoint, token, proxy, sources = enable_all_codex.resolve_config()

        self.assertEqual(endpoint, "https://prompt.example.com")
        self.assertEqual(token, "prompt-token")
        self.assertEqual(proxy, "http://127.0.0.1:8888")
        self.assertEqual(sources, {
            "endpoint": "prompt",
            "token": "prompt",
            "proxy": "prompt",
        })

    def test_resolve_config_uses_descriptive_prompts(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", ""), \
             patch("builtins.input", side_effect=["https://prompt.example.com", ""]) as input_mock, \
             patch("enable_all_codex.getpass", return_value="prompt-token") as getpass_mock:
            enable_all_codex.resolve_config()

        self.assertEqual(input_mock.call_args_list[0].args[0], "请输入 CPA_ENDPOINT（CPA连接地址）: ")
        self.assertEqual(getpass_mock.call_args.args[0], "请输入 CPA_TOKEN（CPA管理密码）: ")

    def test_fetch_codex_accounts_filters_non_codex_entries(self):
        client = Mock()
        client.list_auth_files.return_value = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "oauth", "email": "b@example.com", "disabled": True},
            {"name": "token-c", "email": "c@example.com", "disabled": True},
        ]

        accounts, total = enable_all_codex.fetch_codex_accounts(client)

        self.assertEqual(total, 3)
        self.assertEqual(accounts, [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ])

    def test_enable_accounts_skips_already_enabled_codex_accounts(self):
        client = Mock()
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": False},
        ]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        client.set_disabled.assert_not_called()
        self.assertIn("已是启用状态，跳过", stdout.getvalue())

    def test_enable_accounts_enables_disabled_codex_accounts(self):
        client = Mock()
        client.set_disabled.return_value = True
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        client.set_disabled.assert_called_once_with("token-a", False)
        self.assertIn("启用成功", stdout.getvalue())

    def test_mask_secret_hides_plaintext_token(self):
        masked = enable_all_codex.mask_secret("1234567890abcdef")

        self.assertNotEqual(masked, "1234567890abcdef")
        self.assertEqual(masked, "1234***cdef")

    @patch("enable_all_codex.enable_accounts", return_value=0)
    @patch("enable_all_codex.fetch_codex_accounts", return_value=([], 0))
    @patch("enable_all_codex.CPAClient")
    def test_main_logs_masked_token_and_sources(self, client_cls, _fetch_mock, _enable_mock):
        with patch("enable_all_codex.resolve_config", return_value=(
            "https://example.com",
            "1234567890abcdef",
            "http://127.0.0.1:7890",
            {"endpoint": "built-in", "token": "built-in", "proxy": "prompt"},
        )), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("目标 CPA: https://example.com", stdout.getvalue())
        self.assertIn("Token: 1234***cdef", stdout.getvalue())
        self.assertIn("endpoint 来源: built-in", stdout.getvalue())
        self.assertIn("token 来源: built-in", stdout.getvalue())
        self.assertIn("proxy 来源: prompt", stdout.getvalue())
        self.assertNotIn("1234567890abcdef", stdout.getvalue())
        client_cls.assert_called_once_with(
            "https://example.com",
            "1234567890abcdef",
            proxy="http://127.0.0.1:7890",
            timeout=enable_all_codex.DEFAULT_CPA_TIMEOUT,
            max_retries=enable_all_codex.DEFAULT_CPA_MAX_RETRIES,
        )

    def test_enable_accounts_continues_after_one_failure(self):
        client = Mock()
        client.set_disabled.side_effect = [False, True]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertIn("token-a", stdout.getvalue())
        self.assertIn("token-b", stdout.getvalue())
        self.assertIn("启用失败", stdout.getvalue())
        self.assertIn("启用成功", stdout.getvalue())
        self.assertIn("失败账号: token-a", stdout.getvalue())

    @patch("enable_all_codex.fetch_codex_accounts", return_value=(
        [{"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True}],
        1,
    ))
    @patch("enable_all_codex.CPAClient")
    def test_main_returns_non_zero_when_any_enable_fails(self, client_cls, _fetch_mock):
        client = client_cls.return_value
        client.set_disabled.return_value = False

        with patch("enable_all_codex.resolve_config", return_value=(
            "https://example.com",
            "1234567890abcdef",
            None,
            {"endpoint": "built-in", "token": "built-in", "proxy": "built-in"},
        )):
            exit_code = enable_all_codex.main()

        self.assertEqual(exit_code, 1)

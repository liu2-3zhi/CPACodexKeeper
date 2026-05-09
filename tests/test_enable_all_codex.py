import io
import pathlib
import sys
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import enable_all_codex


class FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class FakeExecutor:
    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.submitted = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, *args, **kwargs):
        future = FakeFuture(fn(*args, **kwargs))
        self.submitted.append((fn, args, kwargs, future))
        return future


class EnableAllCodexTests(unittest.TestCase):
    def test_resolve_config_uses_built_in_values_without_prompting(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", "https://built-in.example.com"), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", "built-in-token"), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", "http://127.0.0.1:7890"), \
             patch("builtins.input") as input_mock, \
             patch("enable_all_codex.prompt_secret_with_mask") as secret_prompt_mock:
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
        secret_prompt_mock.assert_not_called()

    def test_resolve_config_prompts_for_missing_required_values(self):
        with patch.object(enable_all_codex, "DEFAULT_CPA_ENDPOINT", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_TOKEN", ""), \
             patch.object(enable_all_codex, "DEFAULT_CPA_PROXY", ""), \
             patch("builtins.input", side_effect=["https://prompt.example.com", "http://127.0.0.1:8888"]), \
             patch("enable_all_codex.prompt_secret_with_mask", return_value="prompt-token"):
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
             patch("enable_all_codex.prompt_secret_with_mask", return_value="prompt-token") as secret_prompt_mock:
            enable_all_codex.resolve_config()

        self.assertEqual(input_mock.call_args_list[0].args[0], "请输入 CPA_ENDPOINT（CPA连接地址）: ")
        self.assertEqual(secret_prompt_mock.call_args.args[0], "请输入 CPA_TOKEN（CPA管理密码）: ")

    def test_prompt_secret_with_mask_shows_asterisks(self):
        msvcrt_mock = Mock()
        msvcrt_mock.getwch.side_effect = ["s", "e", "c", "r", "e", "t", "\r"]

        with patch.object(enable_all_codex, "msvcrt", msvcrt_mock), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            entered = enable_all_codex.prompt_secret_with_mask("请输入 CPA_TOKEN（CPA管理密码）: ")

        self.assertEqual(entered, "secret")
        self.assertIn("请输入 CPA_TOKEN（CPA管理密码）: ******\n", stdout.getvalue())

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
        client.get_auth_file.return_value = {"name": "token-a", "disabled": False}
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep") as sleep_mock, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        client.set_disabled.assert_called_once_with("token-a", False)
        client.get_auth_file.assert_called_once_with("token-a")
        sleep_mock.assert_called_once_with(5)
        self.assertIn("启用成功", stdout.getvalue())

    def test_enable_accounts_retries_until_verification_succeeds(self):
        client = Mock()
        client.set_disabled.side_effect = [True, True]
        client.get_auth_file.side_effect = [
            {"name": "token-a", "disabled": True},
            {"name": "token-a", "disabled": False},
        ]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep") as sleep_mock, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertEqual(client.get_auth_file.call_count, 2)
        sleep_mock.assert_has_calls([call(5), call(5)])
        self.assertIn("启用成功", stdout.getvalue())

    def test_enable_accounts_reports_manual_check_after_max_attempts(self):
        client = Mock()
        client.set_disabled.side_effect = [True, True, True, True]
        client.get_auth_file.side_effect = [
            {"name": "token-a", "disabled": True},
            {"name": "token-a", "disabled": True},
            {"name": "token-a", "disabled": True},
            {"name": "token-b", "disabled": False},
        ]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep"), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.set_disabled.call_count, 4)
        self.assertIn("失败账号: token-a", stdout.getvalue())
        self.assertIn("经过 3 次启用确认仍失败，请人工检查", stdout.getvalue())
        self.assertIn("token-b", stdout.getvalue())

    def test_enable_accounts_retries_after_verification_fetch_failure(self):
        client = Mock()
        client.set_disabled.side_effect = [True, True]
        client.get_auth_file.side_effect = [None, {"name": "token-a", "disabled": False}]
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep"), patch("sys.stdout", new_callable=io.StringIO):
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.set_disabled.call_count, 2)
        self.assertEqual(client.get_auth_file.call_count, 2)

    @patch("enable_all_codex.as_completed", side_effect=lambda futures: list(futures))
    def test_enable_accounts_processes_disabled_accounts_with_thread_pool(self, _as_completed_mock):
        client = Mock()
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]
        fake_executor = FakeExecutor(enable_all_codex.DEFAULT_ENABLE_CONCURRENCY)

        with patch("enable_all_codex.ThreadPoolExecutor", side_effect=lambda max_workers: fake_executor), \
             patch("enable_all_codex.process_account", side_effect=[
                 enable_all_codex.AccountProcessResult(
                     name="token-a",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["[1/2] token-a", "token-a ok"],
                 ),
                 enable_all_codex.AccountProcessResult(
                     name="token-b",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["[2/2] token-b", "token-b ok"],
                 ),
             ]) as process_mock, \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_executor.max_workers, enable_all_codex.DEFAULT_ENABLE_CONCURRENCY)
        self.assertEqual(len(fake_executor.submitted), 2)
        self.assertEqual(process_mock.call_count, 2)
        self.assertIn("token-a ok", stdout.getvalue())
        self.assertIn("token-b ok", stdout.getvalue())

    @patch("enable_all_codex.as_completed", side_effect=lambda futures: list(reversed(list(futures))))
    def test_enable_accounts_prints_each_account_logs_as_a_block(self, _as_completed_mock):
        client = Mock()
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]
        fake_executor = FakeExecutor(enable_all_codex.DEFAULT_ENABLE_CONCURRENCY)

        with patch("enable_all_codex.ThreadPoolExecutor", side_effect=lambda max_workers: fake_executor), \
             patch("enable_all_codex.process_account", side_effect=[
                 enable_all_codex.AccountProcessResult(
                     name="token-a",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["A-1", "A-2"],
                 ),
                 enable_all_codex.AccountProcessResult(
                     name="token-b",
                     success=True,
                     already_enabled=False,
                     invalid=False,
                     failure_reason=None,
                     log_lines=["B-1", "B-2"],
                 ),
             ]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            enable_all_codex.enable_accounts(client, accounts)

        suffixes = [
            line.rsplit(": ", 1)[1] if ": " in line else line
            for line in stdout.getvalue().splitlines()
            if (line.rsplit(": ", 1)[-1] if ": " in line else line) in {"A-1", "A-2", "B-1", "B-2"}
        ]
        self.assertEqual(suffixes, ["B-1", "B-2", "A-1", "A-2"])

    def test_process_account_returns_failure_result_for_missing_name(self):
        client = Mock()

        result = enable_all_codex.process_account(client, {"email": "missing@example.com", "disabled": True}, 3, 10)

        self.assertFalse(result.success)
        self.assertTrue(result.invalid)
        self.assertEqual(result.name, "<missing-name>")
        self.assertEqual(result.failure_reason, "缺少账号 name")
        self.assertTrue(any("缺少账号 name，跳过" in line for line in result.log_lines))

    def test_process_account_returns_already_enabled_result_without_submitting_enable(self):
        client = Mock()

        result = enable_all_codex.process_account(
            client,
            {"name": "token-a", "email": "a@example.com", "disabled": False},
            1,
            5,
        )

        self.assertTrue(result.success)
        self.assertTrue(result.already_enabled)
        client.set_disabled.assert_not_called()
        self.assertTrue(any("已是启用状态，跳过" in line for line in result.log_lines))

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
        client.set_disabled.side_effect = [False, False, False, True]
        client.get_auth_file.return_value = {"name": "token-b", "disabled": False}
        accounts = [
            {"name": "token-a", "type": "codex", "email": "a@example.com", "disabled": True},
            {"name": "token-b", "type": "codex", "email": "b@example.com", "disabled": True},
        ]

        with patch("enable_all_codex.time.sleep"), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = enable_all_codex.enable_accounts(client, accounts)

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.set_disabled.call_count, 4)
        self.assertIn("token-a", stdout.getvalue())
        self.assertIn("token-b", stdout.getvalue())
        self.assertIn("启用成功", stdout.getvalue())
        self.assertIn("失败账号: token-a", stdout.getvalue())
        self.assertIn("经过 3 次启用确认仍失败，请人工检查", stdout.getvalue())

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

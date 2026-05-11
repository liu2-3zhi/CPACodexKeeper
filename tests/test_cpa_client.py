import pathlib
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.cpa_client import CPAClient

THREAD_BLOCKING_VERIFICATION_TIMEOUT = 0.1


class CPAClientTests(unittest.TestCase):
    def test_upload_auth_file_passes_name_via_params(self):
        client = CPAClient("https://example.com", "secret")
        client._request = Mock(return_value=Mock(status_code=200))

        token_data = {
            "email": "jamessnyder20000630+89730080@outlook.com",
            "access_token": "token",
        }

        ok = client.upload_auth_file("jamessnyder20000630+89730080@outlook.com.json", token_data)

        self.assertTrue(ok)
        client._request.assert_called_once_with(
            "POST",
            "/v0/management/auth-files",
            params={"name": "jamessnyder20000630+89730080@outlook.com.json"},
            data='{"email": "jamessnyder20000630+89730080@outlook.com", "access_token": "token"}',
        )

    @patch("src.cpa_client.requests.request")
    def test_get_usage_log_uses_management_usage_endpoint_and_auth_headers(self, request_mock):
        response = Mock()
        response.status_code = 200
        response.text = "{}"
        response.json.return_value = {"usage": {}}
        request_mock.return_value = response

        client = CPAClient("https://example.com", "secret")

        result = client.get_usage_log(lookback_seconds=7200)

        self.assertEqual(result, {"usage": {}})
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "https://example.com/v0/management/usage")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["params"], {"lookback_seconds": 7200})

    @patch("src.cpa_client.requests.request")
    def test_request_is_queued_across_threads(self, request_mock):
        order_lock = threading.Lock()
        call_order: list[str] = []
        release_first = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()

        def side_effect(*args, **kwargs):
            label = kwargs["params"]["name"]
            with order_lock:
                call_order.append(f"{label}-start")
            if label == "first":
                first_started.set()
                release_first.wait(timeout=2)
            else:
                second_started.set()
            with order_lock:
                call_order.append(f"{label}-end")
            response = Mock()
            response.status_code = 200
            response.text = "{}"
            response.json.return_value = {"ok": True}
            return response

        request_mock.side_effect = side_effect
        client = CPAClient("https://example.com", "secret")

        def do_first():
            client.get_auth_file("first")

        def do_second():
            client.get_auth_file("second")

        t1 = threading.Thread(target=do_first)
        t2 = threading.Thread(target=do_second)
        t1.start()
        first_started.wait(timeout=1)
        t2.start()
        self.assertFalse(second_started.wait(timeout=THREAD_BLOCKING_VERIFICATION_TIMEOUT))
        release_first.set()
        t1.join()
        t2.join()

        self.assertEqual(call_order, ["first-start", "first-end", "second-start", "second-end"])


if __name__ == "__main__":
    unittest.main()

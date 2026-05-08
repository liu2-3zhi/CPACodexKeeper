import pathlib
import sys
import unittest
<<<<<<< Updated upstream
from unittest.mock import Mock
=======
from unittest.mock import Mock, patch
>>>>>>> Stashed changes

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.cpa_client import CPAClient


class CPAClientTests(unittest.TestCase):
<<<<<<< Updated upstream
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


if __name__ == "__main__":
    unittest.main()
=======
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
>>>>>>> Stashed changes

import time

from curl_cffi import requests

from .models import RequestResult, TokenQuota, UsageInfo
from .utils import brief_response_text


class OpenAIClient:
    USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
    REFRESH_URL = "https://auth.openai.com/oauth/token"
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    REDIRECT_URI = "http://localhost:1455/auth/callback"

    def __init__(self, *, proxy: str | None = None, timeout: int = 15, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def _request(self, method: str, url: str, **kwargs) -> RequestResult:
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    proxies=self.proxies,
                    impersonate="chrome",
                    timeout=self.timeout,
                    **kwargs,
                )
                json_data = None
                try:
                    json_data = response.json()
                except (ValueError, TypeError):
                    pass
                if response.status_code >= 500 and attempt < self.max_retries:
                    time.sleep(1)
                    continue
                return RequestResult(
                    status_code=response.status_code,
                    body=response.text,
                    brief=brief_response_text(response),
                    json_data=json_data,
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(1)
                    continue
        return RequestResult(status_code=None, error=last_error or "request failed")

    def check_usage(self, access_token: str, account_id: str | None = None) -> RequestResult:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.76.0",
        }
        if account_id:
            headers["Chatgpt-Account-Id"] = account_id
        return self._request("GET", self.USAGE_URL, headers=headers)

    def refresh_token(self, refresh_token: str) -> RequestResult:
        payload = {
            "redirect_uri": self.REDIRECT_URI,
            "grant_type": "refresh_token",
            "client_id": self.CLIENT_ID,
            "refresh_token": refresh_token,
        }
        return self._request("POST", self.REFRESH_URL, json=payload)


def parse_usage_info(result: RequestResult | dict | None) -> UsageInfo:
    if isinstance(result, RequestResult):
        body = result.json_data
    elif isinstance(result, dict):
        body = result.get("json") or result
    else:
        body = None

    if not isinstance(body, dict):
        return UsageInfo()

    rate_limit = body.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window")
    credits = body.get("credits") or {}

    primary_window = TokenQuota(
        used_percent=int(primary.get("used_percent", 0) or 0),
        limit_window_seconds=primary.get("limit_window_seconds"),
        reset_after_seconds=primary.get("reset_after_seconds"),
        reset_at=primary.get("reset_at"),
    )
    secondary_window = None
    if isinstance(secondary, dict):
        secondary_window = TokenQuota(
            used_percent=int(secondary.get("used_percent", 0) or 0),
            limit_window_seconds=secondary.get("limit_window_seconds"),
            reset_after_seconds=secondary.get("reset_after_seconds"),
            reset_at=secondary.get("reset_at"),
        )

    return UsageInfo(
        plan_type=body.get("plan_type", "unknown"),
        primary_window=primary_window,
        secondary_window=secondary_window,
        has_credits=bool(credits.get("has_credits", False)),
        credits_balance=credits.get("balance"),
    )

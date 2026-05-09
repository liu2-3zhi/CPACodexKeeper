from __future__ import annotations

import time
from datetime import datetime
from getpass import getpass

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

from src.cpa_client import CPAClient

# CPA 连接地址（例如 https://cpa.example.com）；为空时会在运行时提示输入。
DEFAULT_CPA_ENDPOINT = ""
# CPA 管理密码（Token）；为空时会在运行时提示输入。
DEFAULT_CPA_TOKEN = ""
DEFAULT_CPA_PROXY = ""
DEFAULT_CPA_TIMEOUT = 30
DEFAULT_CPA_MAX_RETRIES = 2
DEFAULT_ENABLE_VERIFY_DELAY_SECONDS = 5
DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS = 3


def log(level: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}][{level}]: {message}")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def prompt_secret_with_mask(prompt: str) -> str:
    if msvcrt is None:
        return getpass(prompt)

    print(prompt, end="", flush=True)
    chars: list[str] = []
    while True:
        char = msvcrt.getwch()
        if char in ("\r", "\n"):
            print()
            break
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\b":
            if chars:
                chars.pop()
                print("\b \b", end="", flush=True)
            continue
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue

        chars.append(char)
        print("*", end="", flush=True)

    return "".join(chars)


def prompt_if_missing(label: str, current: str | None, *, secret: bool = False) -> tuple[str | None, str]:
    value = (current or "").strip()
    if value:
        return value, "built-in"
    if secret:
        entered = prompt_secret_with_mask(f"请输入 {label}: ").strip()
    else:
        entered = input(f"请输入 {label}: ").strip()
    return (entered or None), "prompt"


def resolve_config() -> tuple[str, str, str | None, dict[str, str]]:
    endpoint, endpoint_source = prompt_if_missing("CPA_ENDPOINT（CPA连接地址）", DEFAULT_CPA_ENDPOINT)
    token, token_source = prompt_if_missing("CPA_TOKEN（CPA管理密码）", DEFAULT_CPA_TOKEN, secret=True)
    proxy, proxy_source = prompt_if_missing("CPA_PROXY（可选）", DEFAULT_CPA_PROXY)
    if endpoint is None or token is None:
        raise ValueError("CPA_ENDPOINT and CPA_TOKEN are required")
    return endpoint, token, proxy, {
        "endpoint": endpoint_source,
        "token": token_source,
        "proxy": proxy_source,
    }


def fetch_codex_accounts(client: CPAClient) -> tuple[list[dict], int]:
    files = client.list_auth_files()
    if not isinstance(files, list):
        return [], 0
    accounts = [item for item in files if item.get("type") == "codex"]
    return accounts, len(files)


def enable_account_with_verification(
    client: CPAClient,
    name: str,
    *,
    delay_seconds: int = DEFAULT_ENABLE_VERIFY_DELAY_SECONDS,
    max_attempts: int = DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS,
) -> tuple[bool, str | None]:
    for attempt in range(1, max_attempts + 1):
        log("INFO", f"第 {attempt}/{max_attempts} 次尝试启用账号 {name}")
        if not client.set_disabled(name, False):
            log("ERROR", "启用请求失败")
            continue
        log("INFO", f"等待 {delay_seconds} 秒后复查启用状态")
        time.sleep(delay_seconds)
        token_detail = client.get_auth_file(name)
        if not token_detail:
            log("ERROR", "复查账号详情失败")
            continue
        disabled = token_detail.get("disabled")
        if disabled is False:
            log("OK", "启用成功")
            return True, None
        log("WARN", f"复查发现账号仍为禁用状态: disabled={disabled}")
    return False, f"经过 {max_attempts} 次启用确认仍失败，请人工检查"


def enable_accounts(client: CPAClient, accounts: list[dict]) -> int:
    failures = 0
    already_enabled = 0
    enabled = 0
    skipped_invalid = 0
    failed_names: list[str] = []

    for idx, account in enumerate(accounts, 1):
        name = (account.get("name") or "").strip()
        email = (account.get("email") or "unknown").strip() or "unknown"
        disabled = account.get("disabled")

        log("INFO", f"[{idx}/{len(accounts)}] 账号 name={name or 'unknown'} email={email} disabled={disabled}")

        if not name:
            skipped_invalid += 1
            failures += 1
            failed_names.append("<missing-name>")
            log("WARN", "缺少账号 name，跳过")
            continue

        if disabled is False:
            already_enabled += 1
            log("INFO", "已是启用状态，跳过")
            continue

        log("INFO", "开始设置 disabled=false")
        ok, failure_reason = enable_account_with_verification(client, name)
        if ok:
            enabled += 1
        else:
            failures += 1
            failed_names.append(name)
            log("ERROR", failure_reason or "启用失败")

    attempted = enabled + failures - skipped_invalid
    log("INFO", f"汇总: 总处理={len(accounts)} 已启用={already_enabled} 尝试启用={attempted} 成功启用={enabled} 失败={failures} 无效={skipped_invalid}")
    if failed_names:
        log("ERROR", f"失败账号: {', '.join(failed_names)}")
        log(
            "ERROR",
            f"以下账号经过 {DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS} 次启用确认仍失败，请人工检查: {', '.join(failed_names)}",
        )
    return 1 if failures else 0


def main() -> int:
    try:
        endpoint, token, proxy, sources = resolve_config()
    except ValueError as exc:
        log("ERROR", str(exc))
        return 1

    log("INFO", "启动 codex 批量启用脚本")
    log("INFO", f"目标 CPA: {endpoint}")
    log("INFO", f"Proxy: {proxy or '未设置'}")
    log("INFO", f"Token: {mask_secret(token)}")
    log("INFO", f"endpoint 来源: {sources['endpoint']}")
    log("INFO", f"token 来源: {sources['token']}")
    log("INFO", f"proxy 来源: {sources['proxy']}")

    client = CPAClient(
        endpoint,
        token,
        proxy=proxy,
        timeout=DEFAULT_CPA_TIMEOUT,
        max_retries=DEFAULT_CPA_MAX_RETRIES,
    )
    accounts, total = fetch_codex_accounts(client)
    log("INFO", f"auth-files 总数: {total}")
    log("INFO", f"codex 账号数: {len(accounts)}")
    if total == 0:
        log("INFO", "未获取到任何 auth-file，结束")
        return 0
    if not accounts:
        log("INFO", "未找到任何 codex 账号，结束")
        return 0
    return enable_accounts(client, accounts)


if __name__ == "__main__":
    raise SystemExit(main())

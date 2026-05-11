from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
DEFAULT_ENABLE_CONCURRENCY = 8


def log(level: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}][{level}]: {message}")


def append_account_log(log_lines: list[str], level: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_lines.append(f"[{timestamp}][{level}]: {message}")


def flush_account_logs(log_lines: list[str]) -> None:
    for line in log_lines:
        print(line)


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


@dataclass(slots=True)
class AccountProcessResult:
    name: str
    success: bool
    already_enabled: bool
    invalid: bool
    failure_reason: str | None
    log_lines: list[str]


def enable_account_with_verification(
    client: CPAClient,
    name: str,
    *,
    delay_seconds: int = DEFAULT_ENABLE_VERIFY_DELAY_SECONDS,
    max_attempts: int = DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS,
    log_lines: list[str],
) -> tuple[bool, str | None]:
    for attempt in range(1, max_attempts + 1):
        append_account_log(log_lines, "INFO", f"第 {attempt}/{max_attempts} 次尝试启用账号 {name}")
        if not client.set_disabled(name, False):
            append_account_log(log_lines, "ERROR", "启用请求失败")
            continue
        append_account_log(log_lines, "INFO", f"等待 {delay_seconds} 秒后复查启用状态")
        time.sleep(delay_seconds)
        token_detail = client.get_auth_file(name)
        if not token_detail:
            append_account_log(log_lines, "ERROR", "复查账号详情失败")
            continue
        disabled = token_detail.get("disabled")
        if disabled is False:
            append_account_log(log_lines, "OK", "启用成功")
            return True, None
        append_account_log(log_lines, "WARN", f"复查发现账号仍为禁用状态: disabled={disabled}")
    return False, f"经过 {max_attempts} 次启用确认仍失败，请人工检查"


def process_account(client: CPAClient, account: dict, idx: int, total: int) -> AccountProcessResult:
    log_lines: list[str] = []
    raw_name = (account.get("name") or "").strip()
    email = (account.get("email") or "unknown").strip() or "unknown"
    disabled = account.get("disabled")
    display_name = raw_name or "unknown"

    append_account_log(
        log_lines,
        "INFO",
        f"[{idx}/{total}] 账号 name={display_name} email={email} disabled={disabled}",
    )

    if not raw_name:
        append_account_log(log_lines, "WARN", "缺少账号 name，跳过")
        return AccountProcessResult(
            name="<missing-name>",
            success=False,
            already_enabled=False,
            invalid=True,
            failure_reason="缺少账号 name",
            log_lines=log_lines,
        )

    if disabled is False:
        append_account_log(log_lines, "INFO", "已是启用状态，跳过")
        return AccountProcessResult(
            name=raw_name,
            success=True,
            already_enabled=True,
            invalid=False,
            failure_reason=None,
            log_lines=log_lines,
        )

    append_account_log(log_lines, "INFO", "开始设置 disabled=false")
    ok, failure_reason = enable_account_with_verification(client, raw_name, log_lines=log_lines)
    if not ok:
        append_account_log(log_lines, "ERROR", failure_reason or "启用失败")
    return AccountProcessResult(
        name=raw_name,
        success=ok,
        already_enabled=False,
        invalid=False,
        failure_reason=failure_reason,
        log_lines=log_lines,
    )


def enable_accounts(client: CPAClient, accounts: list[dict]) -> int:
    failures = 0
    already_enabled = 0
    enabled = 0
    skipped_invalid = 0
    failed_names: list[str] = []

    futures: dict = {}
    with ThreadPoolExecutor(max_workers=DEFAULT_ENABLE_CONCURRENCY) as executor:
        for idx, account in enumerate(accounts, 1):
            future = executor.submit(process_account, client, account, idx, len(accounts))
            futures[future] = account

        for future in as_completed(futures):
            result = future.result()
            flush_account_logs(result.log_lines)
            if result.invalid:
                skipped_invalid += 1
                failures += 1
                failed_names.append(result.name)
                continue
            if result.already_enabled:
                already_enabled += 1
                continue
            if result.success:
                enabled += 1
                continue
            failures += 1
            failed_names.append(result.name)

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

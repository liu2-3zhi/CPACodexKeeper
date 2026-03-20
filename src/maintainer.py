import random
import time

from .cpa_client import CPAClient
from .logging_utils import ConsoleLogger
from .models import MaintainerStats
from .openai_client import OpenAIClient, parse_usage_info
from .settings import Settings
from .utils import format_seconds, get_expired_remaining


class CPACodexKeeper:
    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.logger = ConsoleLogger()
        self.cpa_client = CPAClient(
            settings.cpa_endpoint,
            settings.cpa_token,
            proxy=settings.proxy,
            timeout=settings.cpa_timeout_seconds,
            max_retries=settings.max_retries,
        )
        self.openai_client = OpenAIClient(
            proxy=settings.proxy,
            timeout=settings.usage_timeout_seconds,
            max_retries=settings.max_retries,
        )
        self.stats = MaintainerStats()

    def reset_stats(self):
        self.stats = MaintainerStats()

    def log(self, level, message, indent=0):
        self.logger.log(level, message, indent=indent)

    def log_token_header(self, idx, total, name):
        self.logger.token_header(idx, total, name)

    def filter_tokens(self, tokens):
        return [token for token in tokens if token.get("type") == "codex"]

    def get_token_list(self):
        return self.filter_tokens(self.cpa_client.list_auth_files())

    def get_token_detail(self, name):
        return self.cpa_client.get_auth_file(name)

    def delete_token(self, name):
        if self.dry_run:
            self.log("DRY", f"将删除: {name}", indent=1)
            return True
        return self.cpa_client.delete_auth_file(name)

    def set_disabled_status(self, name, disabled=True):
        if self.dry_run:
            self.log("DRY", f"将{'禁用' if disabled else '启用'}: {name}", indent=1)
            return True
        return self.cpa_client.set_disabled(name, disabled)

    def check_token_live(self, access_token, account_id=None):
        if not access_token:
            return None, "missing access_token"
        result = self.openai_client.check_usage(access_token, account_id)
        if result.status_code is None:
            return None, result.error or "request failed"
        return result.status_code, {
            "status_code": result.status_code,
            "body": result.body,
            "brief": result.brief or result.error or "",
            "json": result.json_data,
        }

    def parse_usage_info(self, resp_data):
        usage = parse_usage_info(resp_data)
        return {
            "plan_type": usage.plan_type,
            "primary_used_percent": usage.primary_used_percent,
            "primary_window_seconds": usage.primary_window.limit_window_seconds,
            "primary_reset_at": usage.primary_window.reset_at,
            "secondary_used_percent": usage.secondary_used_percent,
            "secondary_window_seconds": None if usage.secondary_window is None else usage.secondary_window.limit_window_seconds,
            "secondary_reset_at": None if usage.secondary_window is None else usage.secondary_window.reset_at,
            "used_percent": usage.primary_used_percent,
            "has_credits": usage.has_credits,
        }

    def try_refresh(self, token_data):
        rt = token_data.get("refresh_token")
        if not rt:
            return False, None, "缺少 Refresh Token"
        result = self.openai_client.refresh_token(rt)
        if result.status_code != 200 or not result.json_data:
            return False, None, f"刷新被拒({result.status_code})" if result.status_code else (result.error or "刷新失败")
        new_tokens = result.json_data
        expires_in = new_tokens.get("expires_in", 864000)
        new_data = dict(token_data)
        new_data.update({
            "access_token": new_tokens["access_token"],
            "refresh_token": new_tokens.get("refresh_token", rt),
            "id_token": new_tokens.get("id_token"),
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in)),
        })
        return True, new_data, f"刷新成功，新有效期: {format_seconds(expires_in)}"

    def upload_updated_token(self, name, token_data):
        if self.dry_run:
            self.log("DRY", f"将上传更新: {name}", indent=1)
            return True
        return self.cpa_client.upload_auth_file(name, token_data)

    def _skip_token(self, message, *, network_error=False):
        self.log("WARN" if network_error else "SKIP", message, indent=1)
        if network_error:
            self.stats.network_error += 1
            print()
            return "network_error"
        self.stats.skipped += 1
        print()
        return "skipped"

    def _log_token_details(self, token_detail):
        email = token_detail.get("email", "unknown")
        disabled = token_detail.get("disabled", False)
        expired_str, remaining_seconds = get_expired_remaining(token_detail)
        remaining_str = format_seconds(remaining_seconds)

        self.log("INFO", f"Email: {email}", indent=1)
        self.log("INFO", f"状态: {'已禁用' if disabled else '正常'}", indent=1)
        self.log("INFO", f"过期时间: {expired_str or '未知'}", indent=1)
        self.log("INFO", f"剩余有效期: {remaining_str}", indent=1)
        return disabled, remaining_seconds, remaining_str

    def _handle_invalid_token(self, name):
        self.log("WARN", "Token 无效或 workspace 已停用，准备删除", indent=1)
        if self.delete_token(name):
            self.log("DELETE", "已删除", indent=1)
            self.stats.dead += 1
            print()
            return "dead"
        return self._skip_token("删除失败")

    def _handle_non_200_status(self, status, resp_data):
        detail = resp_data.get("brief", "") if isinstance(resp_data, dict) else str(resp_data)
        msg = f"状态异常 ({status})"
        if detail:
            msg += f" | {detail}"
        return self._skip_token(msg)

    def _log_usage_summary(self, body_info):
        plan = body_info.get("plan_type", "unknown")
        primary_pct = body_info.get("primary_used_percent", 0)
        secondary_pct = body_info.get("secondary_used_percent")
        credits = body_info.get("has_credits", False)

        quota_info = f"5h: {primary_pct}%"
        if secondary_pct is not None:
            quota_info += f" | Week: {secondary_pct}%"
        quota_info += f" | Credits: {credits}"
        self.log("OK", f"存活 | Plan: {plan} | {quota_info}", indent=1)
        return primary_pct, secondary_pct

    def _apply_quota_policy(self, name, disabled, primary_pct, secondary_pct):
        check_pct = secondary_pct if secondary_pct is not None else primary_pct
        check_label = "周" if secondary_pct is not None else "5h"

        if disabled:
            if check_pct < self.settings.quota_threshold:
                self.log("WARN", f"已禁用但{check_label}额度已降至 {check_pct}% < {self.settings.quota_threshold}%，准备启用", indent=1)
                if self.set_disabled_status(name, disabled=False):
                    self.log("ENABLE", "已重新启用", indent=1)
                    self.stats.enabled += 1
                else:
                    self.log("ERROR", "启用失败", indent=1)
                return
            self.log("INFO", f"已禁用，{check_label}额度 {check_pct}% >= {self.settings.quota_threshold}%，保持禁用", indent=1)
            return

        if check_pct >= self.settings.quota_threshold:
            self.log("WARN", f"{check_label}额度达到 {check_pct}% >= {self.settings.quota_threshold}%，准备禁用", indent=1)
            if self.set_disabled_status(name, disabled=True):
                self.log("DISABLE", "已禁用", indent=1)
                self.stats.disabled += 1
            else:
                self.log("ERROR", "禁用失败", indent=1)

    def _apply_refresh_policy(self, name, token_detail, remaining_seconds, remaining_str):
        expiry_threshold_seconds = self.settings.expiry_threshold_days * 86400
        if remaining_seconds > 0 and remaining_seconds < expiry_threshold_seconds:
            self.log("WARN", f"剩余 {remaining_str} < {self.settings.expiry_threshold_days} 天，准备刷新", indent=1)
            success, new_data, msg = self.try_refresh(token_detail)
            if success:
                if self.upload_updated_token(name, new_data):
                    _, new_remaining = get_expired_remaining(new_data)
                    self.log("REFRESH", f"{msg}，新剩余: {format_seconds(new_remaining)}", indent=1)
                    self.stats.refreshed += 1
                else:
                    self.log("ERROR", "刷新成功但上传失败", indent=1)
            else:
                self.log("ERROR", f"刷新失败: {msg}", indent=1)
        elif remaining_seconds > 0:
            self.log("INFO", f"过期时间充足 ({remaining_str})", indent=1)

    def process_token(self, token_info, idx, total):
        name = token_info.get("name", "unknown")
        self.log_token_header(idx, total, name)
        self.log("INFO", "获取详情...", indent=1)
        token_detail = self.get_token_detail(name)
        if not token_detail:
            return self._skip_token("获取详情失败")

        disabled, remaining_seconds, remaining_str = self._log_token_details(token_detail)
        access_token = token_detail.get("access_token")
        account_id = token_detail.get("account_id")
        if not access_token:
            return self._skip_token("缺少 access_token")

        self.log("INFO", "检测在线状态...", indent=1)
        status, resp_data = self.check_token_live(access_token, account_id)
        if status in (401, 402):
            return self._handle_invalid_token(name)
        if status is None:
            detail = resp_data.get("brief", "") if isinstance(resp_data, dict) else str(resp_data)
            msg = "网络检测失败"
            if detail:
                msg += f" | {detail}"
            return self._skip_token(msg, network_error=True)
        if status != 200:
            return self._handle_non_200_status(status, resp_data)

        body_info = self.parse_usage_info(resp_data)
        primary_pct, secondary_pct = self._log_usage_summary(body_info)
        self._apply_quota_policy(name, disabled, primary_pct, secondary_pct)
        self._apply_refresh_policy(name, token_detail, remaining_seconds, remaining_str)

        self.stats.alive += 1
        print()
        return "alive"

    def log_startup(self):
        self.logger.divider()
        self.log("INFO", "CPACodexKeeper 启动")
        self.log("INFO", f"API: {self.settings.cpa_endpoint}")
        self.log("INFO", f"Quota threshold: {self.settings.quota_threshold}% (disable when reached)")
        self.log("INFO", f"Expiry threshold: {self.settings.expiry_threshold_days} days (refresh when below)")
        if self.dry_run:
            self.log("DRY", "演练模式 (不实际修改)")
        self.logger.divider()

    def run(self):
        self.reset_stats()
        self.log_startup()
        tokens = self.get_token_list()
        if not tokens:
            self.log("WARN", "未获取到任何 codex Token")
            return

        self.stats.total = len(tokens)
        random.shuffle(tokens)
        start_time = time.time()
        self.log("INFO", f"共计: {len(tokens)} 个 codex Token")
        print()

        for idx, token_info in enumerate(tokens, 1):
            self.process_token(token_info, idx, len(tokens))

        elapsed = time.time() - start_time
        self.logger.divider()
        self.log("INFO", "执行完成")
        self.log("INFO", f"耗时: {elapsed:.1f} 秒")
        self.log("INFO", "统计:")
        print(f"    - 总计: {self.stats.total}")
        print(f"    - 存活: {self.stats.alive}")
        print(f"    - 死号(已删除): {self.stats.dead}")
        print(f"    - 已禁用: {self.stats.disabled}")
        print(f"    - 已启用: {self.stats.enabled}")
        print(f"    - 已刷新: {self.stats.refreshed}")
        print(f"    - 跳过: {self.stats.skipped}")
        print(f"    - 网络失败: {self.stats.network_error}")
        self.logger.divider()

    def run_forever(self, interval_seconds=1800):
        round_no = 0
        self.log("INFO", f"守护模式启动，执行间隔: {interval_seconds} 秒")
        while True:
            round_no += 1
            self.log("INFO", f"开始第 {round_no} 轮巡检")
            try:
                self.run()
                self.log("INFO", f"第 {round_no} 轮巡检结束")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.log("ERROR", f"第 {round_no} 轮巡检异常: {exc}")
            self.log("INFO", f"等待 {interval_seconds} 秒后开始下一轮")
            time.sleep(interval_seconds)

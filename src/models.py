from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TokenQuota:
    used_percent: int = 0
    limit_window_seconds: int | None = None
    reset_after_seconds: int | None = None
    reset_at: int | None = None


@dataclass(slots=True)
class UsageInfo:
    plan_type: str = "unknown"
    primary_window: TokenQuota = field(default_factory=TokenQuota)
    secondary_window: TokenQuota | None = None
    has_credits: bool = False
    credits_balance: float | None = None

    @property
    def primary_used_percent(self) -> int:
        return self.primary_window.used_percent

    @property
    def secondary_used_percent(self) -> int | None:
        return None if self.secondary_window is None else self.secondary_window.used_percent

    @property
    def quota_check_percent(self) -> int:
        return self.secondary_used_percent if self.secondary_used_percent is not None else self.primary_used_percent

    @property
    def quota_check_label(self) -> str:
        return "week" if self.secondary_used_percent is not None else "5h"


@dataclass(slots=True)
class MaintainerStats:
    total: int = 0
    alive: int = 0
    dead: int = 0
    disabled: int = 0
    enabled: int = 0
    refreshed: int = 0
    skipped: int = 0
    network_error: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "alive": self.alive,
            "dead": self.dead,
            "disabled": self.disabled,
            "enabled": self.enabled,
            "refreshed": self.refreshed,
            "skipped": self.skipped,
            "network_error": self.network_error,
        }


@dataclass(slots=True)
class RequestResult:
    status_code: int | None
    body: str = ""
    brief: str = ""
    json_data: dict[str, Any] | None = None
    error: str | None = None

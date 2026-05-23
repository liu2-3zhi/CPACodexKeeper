import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_FILL_INTERVAL_SECONDS = 10
DEFAULT_QUOTA_THRESHOLD = 100
DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS = 18000
DEFAULT_EXPIRY_THRESHOLD_DAYS = 3
DEFAULT_USAGE_TIMEOUT_SECONDS = 15
DEFAULT_CPA_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_FULL_SCAN_MIN_INTERVAL_SECONDS = 10
DEFAULT_FULL_SCAN_MAX_INTERVAL_SECONDS = 60
DEFAULT_ENABLE_REFRESH = True
DEFAULT_ALLOW_DELETE = True
DEFAULT_FORCE_REFRESH_ON_EXPIRY = False
DEFAULT_LOG_ARCHIVE_MAX_SIZE_MB = 500   # 日志归档最大大小，单位为MB
DEFAULT_DISABLED_STATE_LOCK_TIMEOUT_SECONDS = 10.0
DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS = 0.2
DEFAULT_ENABLE_VERIFY_DELAY_SECONDS = 5
DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS = 3
PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class SettingsError(ValueError):
    pass


@dataclass(slots=True)
class Settings:
    cpa_endpoint: str
    cpa_token: str
    proxy: str | None = None
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    fill_interval_seconds: int = DEFAULT_FILL_INTERVAL_SECONDS
    quota_threshold: int = DEFAULT_QUOTA_THRESHOLD
    quota_reset_none_recheck_seconds: int = DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS
    expiry_threshold_days: int = DEFAULT_EXPIRY_THRESHOLD_DAYS
    usage_timeout_seconds: int = DEFAULT_USAGE_TIMEOUT_SECONDS
    cpa_timeout_seconds: int = DEFAULT_CPA_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    full_scan_min_interval_seconds: int = DEFAULT_FULL_SCAN_MIN_INTERVAL_SECONDS
    full_scan_max_interval_seconds: int = DEFAULT_FULL_SCAN_MAX_INTERVAL_SECONDS
    enable_refresh: bool = DEFAULT_ENABLE_REFRESH
    allow_delete: bool = DEFAULT_ALLOW_DELETE
    force_refresh_on_expiry: bool = DEFAULT_FORCE_REFRESH_ON_EXPIRY
    log_archive_max_size_mb: int = DEFAULT_LOG_ARCHIVE_MAX_SIZE_MB
    disabled_state_lock_timeout_seconds: float = DEFAULT_DISABLED_STATE_LOCK_TIMEOUT_SECONDS
    disabled_state_lock_retry_interval_seconds: float = DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS
    enable_verify_delay_seconds: int = DEFAULT_ENABLE_VERIFY_DELAY_SECONDS
    enable_verify_max_attempts: int = DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS


def _read_project_env_file(env_file: Path | None = None) -> dict[str, str]:
    target = env_file or PROJECT_ENV_FILE
    if not target.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _get_config_value(name: str, env_values: dict[str, str]) -> str | None:
    env_value = os.getenv(name)
    if env_value not in (None, ""):
        return env_value
    return env_values.get(name)


def _read_int(name: str, default: int, env_values: dict[str, str], *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = _get_config_value(name, env_values)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be <= {maximum}")
    return value


def _read_float(name: str, default: float, env_values: dict[str, str], *, minimum: float = 0.0) -> float:
    raw = _get_config_value(name, env_values)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if value <= minimum:
        raise SettingsError(f"{name} must be > {minimum}")
    return value


def _read_bool(name: str, default: bool, env_values: dict[str, str]) -> bool:
    raw = _get_config_value(name, env_values)
    if raw in (None, ""):
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean")


def load_settings(env_file: Path | None = None) -> Settings:
    env_values = _read_project_env_file(env_file)
    endpoint = (_get_config_value("CPA_ENDPOINT", env_values) or "").strip().rstrip("/")
    token = (_get_config_value("CPA_TOKEN", env_values) or "").strip()
    proxy = (_get_config_value("CPA_PROXY", env_values) or "").strip() or None

    if not endpoint:
        raise SettingsError("CPA_ENDPOINT is required")
    if not token:
        raise SettingsError("CPA_TOKEN is required")
    if not endpoint.startswith(("http://", "https://")):
        raise SettingsError("CPA_ENDPOINT must start with http:// or https://")

    full_scan_min_interval_seconds = _read_int(
        "CPA_FULL_SCAN_MIN_INTERVAL_SECONDS",
        DEFAULT_FULL_SCAN_MIN_INTERVAL_SECONDS,
        env_values,
        minimum=0,
        maximum=60,
    )
    full_scan_max_interval_seconds = _read_int(
        "CPA_FULL_SCAN_MAX_INTERVAL_SECONDS",
        DEFAULT_FULL_SCAN_MAX_INTERVAL_SECONDS,
        env_values,
        minimum=0,
        maximum=60,
    )
    if full_scan_min_interval_seconds > full_scan_max_interval_seconds:
        raise SettingsError("CPA_FULL_SCAN_MIN_INTERVAL_SECONDS must be <= CPA_FULL_SCAN_MAX_INTERVAL_SECONDS")

    return Settings(
        cpa_endpoint=endpoint,
        cpa_token=token,
        proxy=proxy,
        interval_seconds=_read_int("CPA_INTERVAL", DEFAULT_INTERVAL_SECONDS, env_values, minimum=1),
        fill_interval_seconds=_read_int("CPA_FILL_INTERVAL", DEFAULT_FILL_INTERVAL_SECONDS, env_values, minimum=-2147483648),
        quota_threshold=_read_int("CPA_QUOTA_THRESHOLD", DEFAULT_QUOTA_THRESHOLD, env_values, minimum=0, maximum=100),
        quota_reset_none_recheck_seconds=_read_int(
            "CPA_QUOTA_RESET_NONE_RECHECK_SECONDS",
            DEFAULT_QUOTA_RESET_NONE_RECHECK_SECONDS,
            env_values,
            minimum=1,
        ),
        expiry_threshold_days=_read_int("CPA_EXPIRY_THRESHOLD_DAYS", DEFAULT_EXPIRY_THRESHOLD_DAYS, env_values, minimum=0),
        usage_timeout_seconds=_read_int("CPA_USAGE_TIMEOUT", DEFAULT_USAGE_TIMEOUT_SECONDS, env_values, minimum=1),
        cpa_timeout_seconds=_read_int("CPA_HTTP_TIMEOUT", DEFAULT_CPA_TIMEOUT_SECONDS, env_values, minimum=1),
        max_retries=_read_int("CPA_MAX_RETRIES", DEFAULT_MAX_RETRIES, env_values, minimum=0, maximum=5),
        full_scan_min_interval_seconds=full_scan_min_interval_seconds,
        full_scan_max_interval_seconds=full_scan_max_interval_seconds,
        enable_refresh=_read_bool("CPA_ENABLE_REFRESH", DEFAULT_ENABLE_REFRESH, env_values),
        allow_delete=_read_bool("CPA_ALLOW_DELETE", DEFAULT_ALLOW_DELETE, env_values),
        force_refresh_on_expiry=_read_bool(
            "CPA_FORCE_REFRESH_ON_EXPIRY",
            DEFAULT_FORCE_REFRESH_ON_EXPIRY,
            env_values,
        ),
        log_archive_max_size_mb=_read_int(
            "CPA_LOG_ARCHIVE_MAX_SIZE_MB",
            DEFAULT_LOG_ARCHIVE_MAX_SIZE_MB,
            env_values,
            minimum=1,
        ),
        disabled_state_lock_timeout_seconds=_read_float(
            "CPA_DISABLED_STATE_LOCK_TIMEOUT_SECONDS",
            DEFAULT_DISABLED_STATE_LOCK_TIMEOUT_SECONDS,
            env_values,
        ),
        disabled_state_lock_retry_interval_seconds=_read_float(
            "CPA_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS",
            DEFAULT_DISABLED_STATE_LOCK_RETRY_INTERVAL_SECONDS,
            env_values,
        ),
        enable_verify_delay_seconds=_read_int(
            "CPA_ENABLE_VERIFY_DELAY_SECONDS",
            DEFAULT_ENABLE_VERIFY_DELAY_SECONDS,
            env_values,
            minimum=1,
        ),
        enable_verify_max_attempts=_read_int(
            "CPA_ENABLE_VERIFY_MAX_ATTEMPTS",
            DEFAULT_ENABLE_VERIFY_MAX_ATTEMPTS,
            env_values,
            minimum=1,
        ),
    )

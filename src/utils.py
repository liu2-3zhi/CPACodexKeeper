import json
import time
from datetime import datetime, timezone


def decode_jwt_segment(seg: str) -> dict:
    raw = (seg or "").strip()
    if not raw:
        return {}
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    try:
        import base64
        return json.loads(base64.urlsafe_b64decode((raw + pad).encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def get_token_remaining_seconds(access_token: str) -> float:
    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            payload = decode_jwt_segment(parts[1])
            exp = payload.get("exp")
            if exp:
                return exp - time.time()
    except (AttributeError, TypeError, ValueError):
        return -1
    return -1


def format_seconds(seconds: float) -> str:
    if seconds < 0:
        return "已过期"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}天{hours}小时"
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def parse_expired_time(expired_str: str) -> float:
    if not expired_str:
        return -1
    try:
        expired_str = expired_str.strip()
        if "T" in expired_str:
            if expired_str.endswith("Z"):
                expired_str = expired_str[:-1] + "+00:00"
            for fmt in [
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S",
            ]:
                try:
                    if fmt.endswith("Z") and "+" not in expired_str:
                        dt = datetime.strptime(expired_str.replace("+00:00", ""), fmt)
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = datetime.strptime(expired_str, fmt)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    return (dt - now).total_seconds()
                except ValueError:
                    continue
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
            try:
                dt = datetime.strptime(expired_str, fmt)
                dt = dt.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                return (dt - now).total_seconds()
            except ValueError:
                continue
        return -1
    except Exception:
        return -1


def get_expired_remaining(token_data: dict) -> tuple[str, float]:
    expired_str = token_data.get("expired", "")
    remaining = parse_expired_time(expired_str)
    if remaining < 0:
        access_token = token_data.get("access_token", "")
        if access_token:
            remaining = get_token_remaining_seconds(access_token)
    return expired_str, remaining


def brief_response_text(resp, limit=160) -> str:
    try:
        text = (resp.text or "").strip().replace("\n", " ")
        if not text:
            return ""
        return text[:limit] + ("..." if len(text) > limit else "")
    except Exception:
        return ""

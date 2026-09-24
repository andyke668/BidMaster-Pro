"""统一时间口径。

库里所有 created_at 都是 naive UTC（见 models._naive_utcnow 的部署补丁：列为
TIMESTAMP WITHOUT TIME ZONE，asyncpg 拒绝 tz-aware 值），而使用者在 Asia/Shanghai。
日活 / 今日用量 / 日报表如果直接按 UTC 日期分组会整体差 8 小时——北京时间 08:00
之前的行为会被算进「昨天」。所有涉及「自然日」与「对外展示」的换算都收在这里。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # Windows 无 tzdata 包时 ZoneInfo 会抛错，回落到固定 +08:00
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

_FALLBACK_TZ = timezone(timedelta(hours=8), name="UTC+8")

# 前端可选的时间范围；"today" 走本地自然日，其余是滚动窗口，"all" 不加时间条件
RANGE_PRESETS: dict[str, timedelta | None] = {
    "today": timedelta(0),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "all": None,
}
DEFAULT_RANGE = "7d"


def utcnow_naive() -> datetime:
    """与 models._naive_utcnow 一致的当前时刻，可直接与库里的列比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_tz(name: str | None = None):
    from core.settings import get_settings

    zone_name = name or get_settings().report_timezone or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(zone_name)
        except Exception:
            pass
    return _FALLBACK_TZ


def normalize_range(range_key: str | None) -> str:
    key = (range_key or DEFAULT_RANGE).strip().lower()
    return key if key in RANGE_PRESETS else DEFAULT_RANGE


def to_local(dt: datetime | None, tz=None) -> datetime | None:
    """naive UTC -> 带时区的本地时间。"""
    if dt is None:
        return None
    tz = tz or get_tz()
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz)


def to_local_iso(dt: datetime | None, tz=None) -> str | None:
    """对外统一输出带偏移的 ISO 串（2026-09-24T15:30:00+08:00），前端 new Date() 可直接用。"""
    local = to_local(dt, tz)
    return local.isoformat(timespec="seconds") if local else None


def local_date(dt: datetime | None, tz=None) -> str | None:
    local = to_local(dt, tz)
    return local.strftime("%Y-%m-%d") if local else None


def local_day_start_utc(tz=None) -> datetime:
    """本地自然日 00:00 对应的 naive UTC 时刻，用于「今日」类查询下界与配额计数。"""
    tz = tz or get_tz()
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def range_start_utc(range_key: str | None, tz=None) -> datetime | None:
    """把 today/24h/7d/all 换算成 naive UTC 下界；all 返回 None 表示不加时间条件。"""
    key = normalize_range(range_key)
    if key == "today":
        return local_day_start_utc(tz)
    delta = RANGE_PRESETS.get(key)
    if delta is None:
        return None
    return utcnow_naive() - delta


def range_days(range_key: str | None) -> int:
    """区间覆盖的本地自然日天数，用于趋势图横轴与「活跃天数」口径。"""
    key = normalize_range(range_key)
    if key == "today":
        return 1
    if key == "24h":
        return 2
    delta = RANGE_PRESETS.get(key)
    if delta is None:
        return 30
    return max(1, delta.days + 1)


def local_day_series(range_key: str | None, tz=None) -> list[str]:
    """趋势图横轴：按本地自然日列出区间内的日期，最多 120 天。"""
    tz = tz or get_tz()
    today = datetime.now(tz).date()
    days = min(range_days(range_key), 120)
    return [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days - 1, -1, -1)
    ]

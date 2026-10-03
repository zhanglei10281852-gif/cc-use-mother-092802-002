"""时区与时间区间工具。

所有持久化的时间一律为 UTC ISO8601 字符串；对外接收的时间可以带偏移量，
也可以是 naive 时间（此时按调用方提供的 IANA 时区解释）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def parse_instant(value, default_tz: str | None = None) -> datetime:
    """把 ISO8601 字符串/datetime 解析为 UTC 时间。

    naive 时间按 ``default_tz``（IANA 名称）解释；缺少时区且未给默认时区时报错。
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        if default_tz is None:
            raise ValueError(f"时间 {value!r} 缺少时区，且未提供默认时区")
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return dt.astimezone(UTC)


def ensure_timezone(name: str) -> str:
    """校验 IANA 时区名称，非法时抛 ValueError。"""
    ZoneInfo(name)
    return name


def iso(dt: datetime) -> str:
    """规范化存储格式（UTC，带 +00:00 偏移）。"""
    return dt.astimezone(UTC).isoformat()


def parse_stored(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def now_utc() -> datetime:
    return datetime.now(UTC)


def make_buckets(start: datetime, end: datetime, bucket_seconds: int | None):
    """把 [start, end) 切成固定长度桶；bucket_seconds 为 None 时整个周期一个桶。

    桶按 UTC 绝对秒数切分，因此夏令时切换日会出现 23/25 个桶，
    这正是跨时区边界需要保留的行为。
    """
    if end <= start:
        raise ValueError("区间结束必须晚于开始")
    if not bucket_seconds:
        return [(start, end)]
    buckets = []
    cursor = start
    step = timedelta(seconds=bucket_seconds)
    while cursor < end:
        nxt = min(cursor + step, end)
        buckets.append((cursor, nxt))
        cursor = nxt
    return buckets


def overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds())

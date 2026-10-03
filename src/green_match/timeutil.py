"""时间区间工具：时区归一、UTC 分桶与跨时段拆分。

所有进入系统的时间都必须携带时区，并立即归一到 UTC；
分桶与拆分只发生在 UTC 轴上，因此不受各地夏令时切换影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterator

UTC = timezone.utc

GRANULARITY_STEP = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}


def parse_instant(value: str) -> datetime:
    """解析 ISO-8601 时间，必须带时区；返回归一到 UTC 的时间。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"时间必须携带时区: {value!r}")
    return parsed.astimezone(UTC)


def format_instant(moment: datetime) -> str:
    """输出规范 UTC ISO-8601 字符串。

    同一时刻总是得到同一字符串，且字符串字典序与时间序一致，
    因此可以直接作为 SQLite 文本列比较与去重。
    """
    if moment.tzinfo is None:
        raise ValueError("内部错误：朴素时间不允许输出")
    return moment.astimezone(UTC).isoformat()


def floor_to_bucket(moment: datetime, granularity: str) -> datetime:
    """向下取整到 UTC 桶边界。"""
    moment = moment.astimezone(UTC)
    if granularity == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"未知分桶粒度: {granularity}")


def iter_buckets(start: datetime, end: datetime, granularity: str) -> Iterator[datetime]:
    """枚举与 [start, end) 相交的所有 UTC 桶的起点（含首尾部分重叠的桶）。"""
    step = GRANULARITY_STEP[granularity]
    cursor = floor_to_bucket(start, granularity)
    while cursor < end:
        yield cursor
        cursor += step


def _seconds(delta: timedelta) -> Decimal:
    """精确秒数（含微秒），不走 float，保证拆分比例精确。"""
    return (
        Decimal(delta.days * 86400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )


def split_interval(
    start: datetime, end: datetime, kwh: Decimal, granularity: str
) -> list[tuple[datetime, Decimal]]:
    """把区间电量按各桶重叠时长占比拆分到 UTC 桶。

    返回 (桶起点, 分摊电量) 列表；各份之和恒等于输入电量（Decimal 精度内）。
    """
    if end <= start:
        raise ValueError("区间结束必须晚于开始")
    total = _seconds(end - start)
    step = GRANULARITY_STEP[granularity]
    pieces: list[tuple[datetime, Decimal]] = []
    for bucket_start in iter_buckets(start, end, granularity):
        bucket_end = bucket_start + step
        overlap_start = max(start, bucket_start)
        overlap_end = min(end, bucket_end)
        if overlap_end <= overlap_start:
            continue
        share = _seconds(overlap_end - overlap_start) / total
        pieces.append((bucket_start, kwh * share))
    return pieces

"""电量数值的解析与序列化。

全系统统一使用 Decimal 表示电量，避免二进制浮点误差进入核算结果；
序列化时使用定点小数字符串，保证 JSON 输出与输入摘要（digest）稳定。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def parse_kwh(value: Any) -> Decimal:
    """把外部输入解析为 Decimal 电量，拒绝非数值与非有限值。"""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"非法电量数值: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"电量必须是有限数值: {value!r}")
    return number


def dec_str(value: Decimal) -> str:
    """定点小数字符串，避免科学计数法，保证 JSON 与摘要稳定。"""
    return format(value, "f")

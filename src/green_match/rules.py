"""核算口径（caliber）与规则版本注册表。

口径决定匹配的时空严格程度，企业在创建报告时选择；
规则版本对应匹配算法本身，每次核算运行都会记录，
重放历史运行时按其保存的版本分发，保证旧结果永远可以复算对照。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Caliber:
    name: str
    granularity: str  # hour | day —— 用电与供给对齐的时间桶
    region_match: str  # strict（仅同区域） | grid（允许跨区凭证）
    cert_window: str  # bucket（凭证须覆盖同一桶） | period（报告期内池化）
    description: str


_CALIBER_LIST = [
    Caliber(
        "hourly_same_region",
        "hour",
        "strict",
        "bucket",
        "逐小时同区匹配：每个小时的用电只能由同区域、覆盖该小时的自发绿电与凭证匹配",
    ),
    Caliber(
        "daily_same_region",
        "day",
        "strict",
        "bucket",
        "逐日同区匹配：按 UTC 自然日对齐，凭证须覆盖同一日",
    ),
    Caliber(
        "period_pool_same_region",
        "hour",
        "strict",
        "period",
        "报告期内同区池化：同区域凭证在整个报告期内池化，可匹配任意小时",
    ),
    Caliber(
        "period_pool_grid",
        "hour",
        "grid",
        "period",
        "报告期内全网池化：允许跨区凭证，在整个报告期内池化匹配",
    ),
]

CALIBERS: dict[str, Caliber] = {c.name: c for c in _CALIBER_LIST}

RULE_VERSION_V1 = "v1"
SUPPORTED_RULE_VERSIONS = (RULE_VERSION_V1,)
DEFAULT_RULE_VERSION = RULE_VERSION_V1

"""核算口径（规则集）定义。

规则集一旦发布即不可变：版本号唯一标识一份配置，报告在创建时选定口径，
封存后始终按该口径解释。新增口径只能追加新版本，不能改写旧版本。
"""

from __future__ import annotations

# 内置口径。config 字段说明：
#   bucket_seconds:      匹配粒度（秒）。None 表示整个报告周期一个桶（净额法）。
#   use_self_generation: 自发电是否参与匹配。
#   allocation_order:    凭证在同一桶内的核销顺序（expiry_first = 先到期先核销）。
RULE_SETS = [
    {
        "version": "hourly-match@1",
        "description": "逐小时匹配：凭证须覆盖用电发生的同一小时桶，自发电优先抵扣，凭证按先到期先核销。",
        "config": {
            "bucket_seconds": 3600,
            "use_self_generation": True,
            "allocation_order": "expiry_first",
        },
    },
    {
        "version": "monthly-net@1",
        "description": "周期净额：整个报告周期内凭证总量与用电总量对抵，不逐小时核对。",
        "config": {
            "bucket_seconds": None,
            "use_self_generation": True,
            "allocation_order": "expiry_first",
        },
    },
]

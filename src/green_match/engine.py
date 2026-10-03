"""核算引擎：在同一时间桶内匹配用电、自发绿电与属性凭证。

引擎为纯函数实现：不访问数据库，输入由服务层解析装配，
输出为可 JSON 序列化的字典，便于持久化、计算输入摘要与前后差异对比。

匹配顺序（规则 v1）：
1. 每个时间桶内，企业同区域自发自用绿电（generation）优先抵扣用电；
2. 剩余用电由属性凭证按批次起始时间、批次号顺序抵扣；
3. 凭证余量需先扣除已被其他封存报告占用的部分（防止同一凭证被重复申报）；
4. 仍无法匹配的用电按桶记录原因码，汇总进结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from .quantities import dec_str
from .rules import RULE_VERSION_V1, Caliber
from .timeutil import GRANULARITY_STEP, format_instant, iter_buckets, split_interval

ZERO = Decimal("0")

# 未匹配原因码
REASON_NO_CERTIFICATE = "no_certificate_covering_period"
REASON_REGION_MISMATCH = "region_mismatch"
REASON_CERT_EXHAUSTED = "certificate_exhausted"
REASON_CERT_CLAIMED_BY_SEALED = "certificate_claimed_by_sealed_report"


@dataclass(frozen=True)
class EffectiveInterval:
    """修订消解后的有效计量区间（同一区间键只保留最高 revision）。"""

    meter_id: str
    kind: str  # consumption | generation
    region: str
    start: datetime
    end: datetime
    kwh: Decimal
    revision: int


@dataclass(frozen=True)
class LotSupply:
    """属性凭证批次（不可变，内容变更须换新 lot_id）。"""

    lot_id: str
    region: str
    start: datetime
    end: datetime
    available_kwh: Decimal
    source_digest: str


@dataclass(frozen=True)
class EngineInput:
    """一次核算的完整输入快照。

    sealed_cert_use: 已被其他封存报告占用的凭证量，(lot_id, 桶起点) -> kwh，
                     已按本次核算的分桶粒度折算。
    sealed_use_reports: lot_id -> 占用该凭证的封存报告 id，用于争用提示。
    """

    rule_version: str
    caliber: Caliber
    region: str
    period_start: datetime
    period_end: datetime
    consumption: tuple[EffectiveInterval, ...]
    generation: tuple[EffectiveInterval, ...]
    lots: tuple[LotSupply, ...]
    sealed_cert_use: Mapping[tuple[str, datetime], Decimal]
    sealed_use_reports: Mapping[str, tuple[str, ...]]


def run_engine(engine_input: EngineInput) -> dict:
    """按规则版本分发执行；旧版本结果因此始终可以按原规则重放。"""
    if engine_input.rule_version == RULE_VERSION_V1:
        return _run_v1(engine_input)
    raise ValueError(f"不支持的规则版本: {engine_input.rule_version}")


def _accumulate(
    intervals: tuple[EffectiveInterval, ...], granularity: str
) -> dict[datetime, Decimal]:
    """把若干计量区间拆分到桶并逐桶求和。"""
    totals: dict[datetime, Decimal] = {}
    for item in intervals:
        for bucket_start, share in split_interval(
            item.start, item.end, item.kwh, granularity
        ):
            totals[bucket_start] = totals.get(bucket_start, ZERO) + share
    return totals


def _allocation(lot_id: str, bucket_start: datetime, kwh: Decimal) -> dict:
    return {
        "lot_id": lot_id,
        "bucket_start": format_instant(bucket_start),
        "kwh": dec_str(kwh),
    }


def _run_v1(inp: EngineInput) -> dict:
    caliber = inp.caliber
    granularity = caliber.granularity
    step = GRANULARITY_STEP[granularity]
    buckets = list(iter_buckets(inp.period_start, inp.period_end, granularity))

    demand = _accumulate(inp.consumption, granularity)
    own_gen = _accumulate(inp.generation, granularity)

    strict_region = caliber.region_match == "strict"
    eligible = [lot for lot in inp.lots if not strict_region or lot.region == inp.region]
    eligible.sort(key=lambda lot: (lot.start, lot.lot_id))
    other_region = [lot for lot in inp.lots if lot.region != inp.region]

    # 凭证按桶拆分后的名义份额（bucket 窗口口径下使用）
    lot_share: dict[str, dict[datetime, Decimal]] = {}
    for lot in eligible:
        shares: dict[datetime, Decimal] = {}
        for bucket_start, share in split_interval(
            lot.start, lot.end, lot.available_kwh, granularity
        ):
            shares[bucket_start] = shares.get(bucket_start, ZERO) + share
        lot_share[lot.lot_id] = shares

    # 已被封存报告占用的量：按批次汇总（period 窗口）与按桶（bucket 窗口）两种视角
    sealed_by_lot: dict[str, Decimal] = {}
    for (lot_id, _bucket), kwh in inp.sealed_cert_use.items():
        sealed_by_lot[lot_id] = sealed_by_lot.get(lot_id, ZERO) + kwh

    pool_mode = caliber.cert_window == "period"
    pool_remaining: dict[str, Decimal] = (
        {lot.lot_id: lot.available_kwh - sealed_by_lot.get(lot.lot_id, ZERO) for lot in eligible}
        if pool_mode
        else {}
    )

    period_covering = [
        lot for lot in eligible if lot.start < inp.period_end and lot.end > inp.period_start
    ]
    period_covering_other = [
        lot
        for lot in other_region
        if lot.start < inp.period_end and lot.end > inp.period_start
    ]

    def explain(
        bucket_start: datetime, bucket_end: datetime, covering_same: list[LotSupply]
    ) -> str:
        """为某个仍有缺口的桶判定未匹配原因。"""
        if pool_mode:
            same = period_covering
            sealed_same = sum((sealed_by_lot.get(lot.lot_id, ZERO) for lot in same), ZERO)
            other = period_covering_other
        else:
            same = covering_same
            sealed_same = sum(
                (inp.sealed_cert_use.get((lot.lot_id, bucket_start), ZERO) for lot in same),
                ZERO,
            )
            other = [
                lot
                for lot in other_region
                if lot.start < bucket_end and lot.end > bucket_start
            ]
        if not same:
            if strict_region and other:
                return REASON_REGION_MISMATCH
            return REASON_NO_CERTIFICATE
        if sealed_same > ZERO:
            return REASON_CERT_CLAIMED_BY_SEALED
        return REASON_CERT_EXHAUSTED

    allocations: list[dict] = []
    generation_matches: list[dict] = []
    unmatched_buckets: list[dict] = []
    unmatched_by_reason: dict[str, Decimal] = {}
    total_consumption = ZERO
    total_generation = ZERO
    total_gen_matched = ZERO
    total_cert_matched = ZERO

    for bucket_start in buckets:
        bucket_end = bucket_start + step
        produced = own_gen.get(bucket_start, ZERO)
        total_generation += produced
        needed = demand.get(bucket_start, ZERO)
        if needed <= ZERO:
            continue
        total_consumption += needed

        gen_take = min(needed, produced)
        if gen_take > ZERO:
            total_gen_matched += gen_take
            generation_matches.append(
                {"bucket_start": format_instant(bucket_start), "kwh": dec_str(gen_take)}
            )
        remaining = needed - gen_take
        if remaining <= ZERO:
            continue

        covering = [
            lot for lot in eligible if lot.start < bucket_end and lot.end > bucket_start
        ]
        if pool_mode:
            # 报告期池化：按批次顺序从池中扣减，缺口记在当前桶
            for lot in period_covering:
                if remaining <= ZERO:
                    break
                avail = pool_remaining[lot.lot_id]
                if avail <= ZERO:
                    continue
                take = min(remaining, avail)
                pool_remaining[lot.lot_id] = avail - take
                allocations.append(_allocation(lot.lot_id, bucket_start, take))
                total_cert_matched += take
                remaining -= take
        else:
            # 同桶窗口：只有覆盖该桶的凭证份额可用
            for lot in covering:
                if remaining <= ZERO:
                    break
                share = lot_share[lot.lot_id].get(bucket_start, ZERO)
                avail = share - inp.sealed_cert_use.get((lot.lot_id, bucket_start), ZERO)
                if avail <= ZERO:
                    continue
                take = min(remaining, avail)
                allocations.append(_allocation(lot.lot_id, bucket_start, take))
                total_cert_matched += take
                remaining -= take

        if remaining > ZERO:
            reason = explain(bucket_start, bucket_end, covering)
            unmatched_buckets.append(
                {
                    "bucket_start": format_instant(bucket_start),
                    "kwh": dec_str(remaining),
                    "reason": reason,
                }
            )
            unmatched_by_reason[reason] = unmatched_by_reason.get(reason, ZERO) + remaining

    # 争用提示：本报告可用范围内、已被其他封存报告占用的凭证
    contested = [
        {"lot_id": lot.lot_id, "sealed_by_report_ids": list(inp.sealed_use_reports[lot.lot_id])}
        for lot in period_covering
        if inp.sealed_use_reports.get(lot.lot_id)
    ]

    unmatched_total = total_consumption - total_gen_matched - total_cert_matched
    return {
        "rule_version": inp.rule_version,
        "caliber": caliber.name,
        "region": inp.region,
        "period": {
            "start": format_instant(inp.period_start),
            "end": format_instant(inp.period_end),
        },
        "totals": {
            "consumption_kwh": dec_str(total_consumption),
            "generation_kwh": dec_str(total_generation),
            "generation_matched_kwh": dec_str(total_gen_matched),
            "generation_unused_kwh": dec_str(total_generation - total_gen_matched),
            "certificate_matched_kwh": dec_str(total_cert_matched),
            "matched_kwh": dec_str(total_gen_matched + total_cert_matched),
            "unmatched_kwh": dec_str(unmatched_total),
        },
        "unmatched_by_reason": {
            reason: dec_str(kwh) for reason, kwh in sorted(unmatched_by_reason.items())
        },
        "unmatched_buckets": unmatched_buckets,
        "generation_matches": generation_matches,
        "allocations": allocations,
        "contested_lots": contested,
    }

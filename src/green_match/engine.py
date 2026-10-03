"""核算引擎：把用电/自发电与属性凭证在分时段桶内匹配。

引擎是无状态的纯函数式实现：从数据库读出“当前有效”的输入，
按报告周期切桶、匹配、归因，返回结果、输入快照与输入摘要（digest）。
是否持久化、是否封存由 service 层决定，因此重放与首次核算走同一条代码路径。
"""

from __future__ import annotations

import hashlib
import json

from .timeutil import iso, make_buckets, overlap_seconds, parse_stored

EPS = 1e-9

# 未匹配原因
REASON_NO_SUPPLY = "no_supply_in_region"
REASON_NOT_COVERING = "certificate_not_covering_interval"
REASON_LOCKED_BY_OTHER = "certificate_locked_by_other_report"
REASON_EXHAUSTED = "certificate_exhausted"

_REASON_DETAIL = {
    REASON_NO_SUPPLY: "该区域该时段没有任何有效凭证或自发电",
    REASON_NOT_COVERING: "区域内有凭证，但其有效时段未覆盖该用电桶",
    REASON_LOCKED_BY_OTHER: "凭证额度已被其他封存报告核销锁定",
    REASON_EXHAUSTED: "覆盖该时段的凭证额度已核销完毕",
}


def _r(x: float) -> float:
    return round(x, 6)


def _fmt(x: float) -> str:
    return f"{x:.6f}"


def execute(conn, *, report_id: str, org_id: str, region: str, period_start_utc: str,
            period_end_utc: str, rule_version: str, rule_config: dict, kind: str):
    """执行一次核算，返回 (result, inputs_snapshot, input_digest)。

    kind: initial | seal | replay —— 仅记录在结果中，逻辑完全一致。
    用电/发电按报告归属企业过滤（org_id 为空的记录视为区域共享数据）；
    凭证是区域级市场工具，不按企业过滤，争用通过封存锁解决。
    """
    p_start = parse_stored(period_start_utc)
    p_end = parse_stored(period_end_utc)
    bucket_seconds = rule_config.get("bucket_seconds")
    buckets = make_buckets(p_start, p_end, bucket_seconds)

    # ---- 读取有效输入 -----------------------------------------------------
    records = conn.execute(
        "SELECT * FROM energy_records"
        " WHERE status='active' AND region=? AND (org_id IS NULL OR org_id=?)"
        " AND end_utc>? AND start_utc<?"
        " ORDER BY kind, meter_id, start_utc",
        (region, org_id, period_start_utc, period_end_utc),
    ).fetchall()

    certs = conn.execute(
        "SELECT * FROM certificates"
        " WHERE status='active' AND region=? AND end_utc>? AND start_utc<?"
        " ORDER BY end_utc, cert_no",
        (region, period_start_utc, period_end_utc),
    ).fetchall()

    # 其他封存报告对本区域凭证的锁定额度（本报告自己的锁不影响自己）
    locks = {
        row["certificate_id"]: row["locked"]
        for row in conn.execute(
            "SELECT certificate_id, SUM(quantity_kwh) AS locked FROM cert_locks"
            " WHERE report_id != ? GROUP BY certificate_id",
            (report_id,),
        ).fetchall()
    }
    lock_rows = conn.execute(
        "SELECT cl.certificate_id, cl.report_id, c.cert_no, SUM(cl.quantity_kwh) AS locked"
        " FROM cert_locks cl JOIN certificates c ON c.id = cl.certificate_id"
        " WHERE cl.report_id != ? GROUP BY cl.certificate_id, cl.report_id",
        (report_id,),
    ).fetchall()

    out_of_region = conn.execute(
        "SELECT COALESCE(SUM(quantity_kwh), 0) AS q FROM certificates"
        " WHERE status='active' AND region != ? AND end_utc>? AND start_utc<?",
        (region, period_start_utc, period_end_utc),
    ).fetchone()["q"]

    # ---- 计量区间按秒比例拆分到桶 ----------------------------------------
    consumption = [0.0] * len(buckets)
    generation = [0.0] * len(buckets)
    for rec in records:
        r_start, r_end = parse_stored(rec["start_utc"]), parse_stored(rec["end_utc"])
        duration = (r_end - r_start).total_seconds()
        if duration <= 0:
            continue
        target = consumption if rec["kind"] == "consumption" else generation
        for i, (b_start, b_end) in enumerate(buckets):
            ov = overlap_seconds(r_start, r_end, b_start, b_end)
            if ov > 0:
                target[i] += rec["quantity_kwh"] * ov / duration

    # ---- 凭证匹配 ---------------------------------------------------------
    remaining = {c["id"]: max(0.0, c["quantity_kwh"] - locks.get(c["id"], 0.0)) for c in certs}
    locked_by_cert = {c["id"]: locks.get(c["id"], 0.0) for c in certs}
    cert_intervals = {c["id"]: (parse_stored(c["start_utc"]), parse_stored(c["end_utc"])) for c in certs}
    # 先到期先核销，到期相同按凭证号保证确定性
    cert_order = sorted(certs, key=lambda c: (c["end_utc"], c["cert_no"]))

    use_self_gen = bool(rule_config.get("use_self_generation"))
    allocations = []
    bucket_rows = []
    unmatched_rows = []

    for i, (b_start, b_end) in enumerate(buckets):
        need = consumption[i]
        self_matched = min(need, generation[i]) if use_self_gen else 0.0
        need -= self_matched

        covering = [
            c for c in cert_order
            if overlap_seconds(*cert_intervals[c["id"]], b_start, b_end) > 0
        ]
        cert_matched = 0.0
        for cert in covering:
            if need <= EPS:
                break
            take = min(remaining[cert["id"]], need)
            if take > EPS:
                remaining[cert["id"]] -= take
                need -= take
                cert_matched += take
                allocations.append({
                    "certificate_id": cert["id"],
                    "cert_no": cert["cert_no"],
                    "bucket_start_utc": iso(b_start),
                    "quantity_kwh": _r(take),
                })

        row = {
            "bucket_start_utc": iso(b_start),
            "bucket_end_utc": iso(b_end),
            "consumption_kwh": _r(consumption[i]),
            "self_generation_kwh": _r(generation[i]),
            "self_generation_matched_kwh": _r(self_matched),
            "certificate_matched_kwh": _r(cert_matched),
            "unmatched_kwh": _r(max(need, 0.0)),
        }
        if need > EPS:
            reason = _unmatched_reason(certs, covering, locked_by_cert)
            row["unmatched_reason"] = reason
            unmatched_rows.append({
                "bucket_start_utc": iso(b_start),
                "bucket_end_utc": iso(b_end),
                "quantity_kwh": _r(need),
                "reason": reason,
                "detail": _REASON_DETAIL[reason],
            })
        bucket_rows.append(row)

    totals = {
        "consumption_kwh": _r(sum(consumption)),
        "self_generation_kwh": _r(sum(generation)),
        "self_generation_matched_kwh": _r(sum(b["self_generation_matched_kwh"] for b in bucket_rows)),
        "certificate_matched_kwh": _r(sum(b["certificate_matched_kwh"] for b in bucket_rows)),
        "unmatched_kwh": _r(sum(b["unmatched_kwh"] for b in bucket_rows)),
    }
    totals["matched_kwh"] = _r(totals["self_generation_matched_kwh"] + totals["certificate_matched_kwh"])
    totals["match_ratio"] = (
        _r(totals["matched_kwh"] / totals["consumption_kwh"])
        if totals["consumption_kwh"] > EPS else None
    )

    result = {
        "kind": kind,
        "rule_version": rule_version,
        "region": region,
        "org_id": org_id,
        "period": {
            "start_utc": period_start_utc,
            "end_utc": period_end_utc,
            "bucket_seconds": bucket_seconds,
            "bucket_count": len(buckets),
        },
        "totals": totals,
        "buckets": bucket_rows,
        "allocations": allocations,
        "unmatched": unmatched_rows,
        "notes": {
            "out_of_region_certificate_kwh": _r(out_of_region),
            "out_of_region_hint": (
                "存在其他区域的凭证，按当前口径不能跨区核销" if out_of_region > EPS else None
            ),
        },
    }

    # ---- 输入快照与摘要 ---------------------------------------------------
    inputs = []
    for rec in records:
        inputs.append({
            "source_type": "energy_record",
            "source_id": rec["id"],
            "natural_key": f"{rec['kind']}:{rec['meter_id']}:{rec['start_utc']}:{rec['end_utc']}",
            "revision": rec["revision"],
            "quantity_kwh": rec["quantity_kwh"],
        })
    for cert in certs:
        inputs.append({
            "source_type": "certificate",
            "source_id": cert["id"],
            "natural_key": cert["cert_no"],
            "revision": cert["revision"],
            "quantity_kwh": cert["quantity_kwh"],
        })
    for lock in lock_rows:
        inputs.append({
            "source_type": "external_lock",
            "source_id": f"{lock['certificate_id']}:{lock['report_id']}",
            "natural_key": f"lock:{lock['cert_no']}@{lock['report_id']}",
            "revision": None,
            "quantity_kwh": lock["locked"],
        })

    digest_payload = {
        "rule_version": rule_version,
        "region": region,
        "org_id": org_id,
        "period_start_utc": period_start_utc,
        "period_end_utc": period_end_utc,
        "bucket_seconds": bucket_seconds,
        "inputs": sorted(
            [i["source_type"], i["natural_key"], i["revision"], _fmt(i["quantity_kwh"])]
            for i in inputs
        ),
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return result, inputs, digest


def _unmatched_reason(certs_in_region, covering, locked_by_cert) -> str:
    if not certs_in_region:
        return REASON_NO_SUPPLY
    if not covering:
        return REASON_NOT_COVERING
    if sum(locked_by_cert[c["id"]] for c in covering) > EPS:
        return REASON_LOCKED_BY_OTHER
    return REASON_EXHAUSTED


def diff_results(base_result: dict, base_inputs: list, new_result: dict, new_inputs: list) -> dict:
    """比较两次核算，产出面向运维的差异说明。"""
    total_keys = [
        "consumption_kwh", "self_generation_matched_kwh", "certificate_matched_kwh",
        "matched_kwh", "unmatched_kwh",
    ]
    totals_delta = {
        k: _r(new_result["totals"][k] - base_result["totals"][k])
        for k in total_keys
    }

    def ukey(u):
        return (u["bucket_start_utc"], u["reason"], round(u["quantity_kwh"], 3))

    base_u = {ukey(u): u for u in base_result["unmatched"]}
    new_u = {ukey(u): u for u in new_result["unmatched"]}
    unmatched_added = [new_u[k] for k in sorted(new_u.keys() - base_u.keys())]
    unmatched_resolved = [base_u[k] for k in sorted(base_u.keys() - new_u.keys())]

    def ikey(i):
        return (i["source_type"], i["natural_key"], i["revision"])

    base_i = {ikey(i): i for i in base_inputs}
    new_i = {ikey(i): i for i in new_inputs}
    added = [new_i[k] for k in sorted(new_i.keys() - base_i.keys())]
    removed = [base_i[k] for k in sorted(base_i.keys() - new_i.keys())]
    changed = [
        {"before": base_i[k], "after": new_i[k]}
        for k in sorted(base_i.keys() & new_i.keys())
        if abs(base_i[k]["quantity_kwh"] - new_i[k]["quantity_kwh"]) > EPS
    ]

    return {
        "totals_delta": totals_delta,
        "unmatched_added": unmatched_added,
        "unmatched_resolved": unmatched_resolved,
        "input_changes": {
            "added": added,
            "removed": removed,
            "quantity_changed": changed,
        },
    }

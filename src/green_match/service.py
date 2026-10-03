"""应用服务层：导入、核算、封存、重放与审计查询的编排。

关键语义：
- 计量修订：同一区间键只认最高 revision，旧修订保留在库中供审计；
- 输入摘要：每次核算把规则版本、口径、有效输入与封存占用快照做规范化哈希，
  摘要变化即可解释"报告为什么变了"；
- 封存不可变：封存后的运行结果永不改写，新数据只影响新运行；
- 凭证争用：封存报告占用凭证，草稿核算可见剩余量；封存时复核余量，
  不足则拒绝封存并要求重新核算；
- 重放：按原规则版本对当前数据复算，输出前后差异，不触碰原运行。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from .engine import EngineInput, EffectiveInterval, LotSupply, run_engine
from .errors import DomainError
from .quantities import dec_str, parse_kwh
from .rules import CALIBERS, DEFAULT_RULE_VERSION, SUPPORTED_RULE_VERSIONS
from .storage import Storage
from .timeutil import GRANULARITY_STEP, format_instant, parse_instant, split_interval

UTC = timezone.utc
ZERO = Decimal("0")


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _interval_json(iv: EffectiveInterval) -> dict:
    return {
        "meter_id": iv.meter_id,
        "kind": iv.kind,
        "region": iv.region,
        "starts_at": format_instant(iv.start),
        "ends_at": format_instant(iv.end),
        "kwh": dec_str(iv.kwh),
        "revision": iv.revision,
    }


def _lot_json(lot: LotSupply) -> dict:
    return {
        "lot_id": lot.lot_id,
        "region": lot.region,
        "starts_at": format_instant(lot.start),
        "ends_at": format_instant(lot.end),
        "available_kwh": dec_str(lot.available_kwh),
        "source_digest": lot.source_digest,
    }


def _diff_maps(before: dict, after: dict) -> dict:
    """数值字典的逐项差异（仅列出发生变化的项）。"""
    out = {}
    for key in sorted(set(before) | set(after)):
        b = Decimal(str(before.get(key, "0")))
        a = Decimal(str(after.get(key, "0")))
        if a != b:
            out[key] = {"before": dec_str(b), "after": dec_str(a), "delta": dec_str(a - b)}
    return out


def _diff_allocations(before: list, after: list) -> list:
    def keyed(items: list) -> dict:
        return {(i["lot_id"], i["bucket_start"]): Decimal(i["kwh"]) for i in items}

    b_map, a_map = keyed(before), keyed(after)
    changes = []
    for key in sorted(set(b_map) | set(a_map)):
        b = b_map.get(key, ZERO)
        a = a_map.get(key, ZERO)
        if a != b:
            changes.append(
                {
                    "lot_id": key[0],
                    "bucket_start": key[1],
                    "before": dec_str(b),
                    "after": dec_str(a),
                    "delta": dec_str(a - b),
                }
            )
    return changes


class GreenMatchService:
    def __init__(self, db_path: str):
        self.storage = Storage(db_path)

    # ---------------- 输入校验与规范化 ----------------

    @staticmethod
    def _normalize_interval(item: dict) -> dict:
        try:
            meter_id = str(item["meter_id"]).strip()
            kind = str(item["kind"]).strip()
            region = str(item["region"]).strip()
            starts_at = item["starts_at"]
            ends_at = item["ends_at"]
            kwh = parse_kwh(item["kwh"])
            revision = int(item.get("revision", 1))
            source = item.get("source")
        except KeyError as exc:
            raise DomainError(422, "invalid_interval", f"计量区间缺少字段: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise DomainError(422, "invalid_interval", f"计量区间字段非法: {exc}") from exc
        if not meter_id or not region:
            raise DomainError(422, "invalid_interval", "meter_id 与 region 不能为空")
        if kind not in ("consumption", "generation"):
            raise DomainError(422, "invalid_interval", f"未知计量类型: {kind}")
        if not isinstance(starts_at, datetime) or not isinstance(ends_at, datetime):
            raise DomainError(422, "invalid_interval", "时间必须是带时区的 datetime")
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise DomainError(422, "invalid_interval", "时间必须携带时区信息")
        starts_at = starts_at.astimezone(UTC)
        ends_at = ends_at.astimezone(UTC)
        if ends_at <= starts_at:
            raise DomainError(422, "invalid_interval", "区间结束必须晚于开始")
        if kwh < ZERO:
            raise DomainError(422, "invalid_interval", "电量不能为负")
        if revision < 1:
            raise DomainError(422, "invalid_interval", "revision 必须 >= 1")
        return {
            "meter_id": meter_id,
            "kind": kind,
            "region": region,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "kwh": kwh,
            "revision": revision,
            "source": source,
        }

    @staticmethod
    def _normalize_lot(item: dict) -> dict:
        try:
            lot_id = str(item["lot_id"]).strip()
            region = str(item["region"]).strip()
            starts_at = item["starts_at"]
            ends_at = item["ends_at"]
            available = parse_kwh(item["available_kwh"])
            source_digest = str(item["source_digest"]).strip()
        except KeyError as exc:
            raise DomainError(422, "invalid_lot", f"凭证批次缺少字段: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise DomainError(422, "invalid_lot", f"凭证批次字段非法: {exc}") from exc
        if not lot_id or not region or not source_digest:
            raise DomainError(422, "invalid_lot", "lot_id、region、source_digest 不能为空")
        if not isinstance(starts_at, datetime) or not isinstance(ends_at, datetime):
            raise DomainError(422, "invalid_lot", "时间必须是带时区的 datetime")
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise DomainError(422, "invalid_lot", "时间必须携带时区信息")
        starts_at = starts_at.astimezone(UTC)
        ends_at = ends_at.astimezone(UTC)
        if ends_at <= starts_at:
            raise DomainError(422, "invalid_lot", "区间结束必须晚于开始")
        if available <= ZERO:
            raise DomainError(422, "invalid_lot", "凭证可用电量必须为正")
        return {
            "lot_id": lot_id,
            "region": region,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "available_kwh": available,
            "source_digest": source_digest,
        }

    # ---------------- 幂等执行 ----------------

    def _idempotent(
        self,
        endpoint: str,
        key: Optional[str],
        payload: Any,
        op: Callable[[], dict],
    ) -> tuple[dict, bool]:
        """携带幂等键的写操作：同键同体重放返回首个响应，同键异体报 409。"""
        if key is None:
            return op(), False
        request_hash = hashlib.sha256(_canonical(payload)).hexdigest()
        existing = self.storage.get_idempotency(endpoint, key)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise DomainError(
                    409,
                    "idempotency_key_reused",
                    "同一幂等键提交了不同的请求体",
                    {"endpoint": endpoint},
                )
            return json.loads(existing["response_json"]), True
        response = op()
        self.storage.put_idempotency(
            endpoint, key, request_hash, json.dumps(response, ensure_ascii=False),
            format_instant(_now()),
        )
        return response, False

    # ---------------- 导入 ----------------

    def import_meter_intervals(
        self, items: list[dict], idempotency_key: Optional[str] = None
    ) -> tuple[dict, bool]:
        normalized = [self._normalize_interval(i) for i in items]
        payload = [
            {
                **{k: v for k, v in i.items() if k in ("meter_id", "kind", "region", "revision", "source")},
                "starts_at": format_instant(i["starts_at"]),
                "ends_at": format_instant(i["ends_at"]),
                "kwh": dec_str(i["kwh"]),
            }
            for i in normalized
        ]

        def op() -> dict:
            rows = [
                {
                    **i,
                    "starts_at": format_instant(i["starts_at"]),
                    "ends_at": format_instant(i["ends_at"]),
                    "kwh": dec_str(i["kwh"]),
                    "created_at": format_instant(_now()),
                }
                for i in normalized
            ]
            return self.storage.add_meter_intervals(rows)

        return self._idempotent("POST /imports/meter-intervals", idempotency_key, payload, op)

    def import_attribute_lots(
        self, items: list[dict], idempotency_key: Optional[str] = None
    ) -> tuple[dict, bool]:
        normalized = [self._normalize_lot(i) for i in items]
        payload = [
            {
                "lot_id": i["lot_id"],
                "region": i["region"],
                "starts_at": format_instant(i["starts_at"]),
                "ends_at": format_instant(i["ends_at"]),
                "available_kwh": dec_str(i["available_kwh"]),
                "source_digest": i["source_digest"],
            }
            for i in normalized
        ]

        def op() -> dict:
            rows = [
                {
                    **i,
                    "starts_at": format_instant(i["starts_at"]),
                    "ends_at": format_instant(i["ends_at"]),
                    "available_kwh": dec_str(i["available_kwh"]),
                    "created_at": format_instant(_now()),
                }
                for i in normalized
            ]
            return self.storage.add_lots(rows)

        return self._idempotent("POST /imports/attribute-lots", idempotency_key, payload, op)

    # ---------------- 报告生命周期 ----------------

    def create_report(
        self,
        enterprise_id: str,
        region: str,
        period_start: datetime,
        period_end: datetime,
        caliber: str,
        idempotency_key: Optional[str] = None,
    ) -> tuple[dict, bool]:
        enterprise_id = str(enterprise_id).strip()
        region = str(region).strip()
        if not enterprise_id or not region:
            raise DomainError(422, "invalid_report", "enterprise_id 与 region 不能为空")
        if caliber not in CALIBERS:
            raise DomainError(
                422,
                "unknown_caliber",
                f"未知核算口径: {caliber}",
                {"available_calibers": sorted(CALIBERS)},
            )
        if period_start.tzinfo is None or period_end.tzinfo is None:
            raise DomainError(422, "invalid_report", "报告周期必须携带时区信息")
        period_start = period_start.astimezone(UTC)
        period_end = period_end.astimezone(UTC)
        if period_end <= period_start:
            raise DomainError(422, "invalid_report", "报告周期结束必须晚于开始")

        payload = {
            "enterprise_id": enterprise_id,
            "region": region,
            "period_start": format_instant(period_start),
            "period_end": format_instant(period_end),
            "caliber": caliber,
        }

        def op() -> dict:
            scope_reports = self.storage.find_scope_reports(
                enterprise_id, region, payload["period_start"], payload["period_end"], caliber
            )
            drafts = [r for r in scope_reports if r["status"] == "draft"]
            if drafts:
                raise DomainError(
                    409,
                    "draft_exists",
                    "相同企业、区域、周期与口径的草稿报告已存在，请先封存或作废",
                    {"existing_report_ids": [r["report_id"] for r in drafts]},
                )
            revision_no = max((r["revision_no"] for r in scope_reports), default=0) + 1
            report_id = "rep_" + uuid.uuid4().hex
            self.storage.insert_report(
                {
                    "report_id": report_id,
                    "enterprise_id": enterprise_id,
                    "region": region,
                    "period_start": payload["period_start"],
                    "period_end": payload["period_end"],
                    "caliber": caliber,
                    "revision_no": revision_no,
                    "status": "draft",
                    "sealed_run_id": None,
                    "created_at": format_instant(_now()),
                    "sealed_at": None,
                }
            )
            return self.get_report(report_id)

        return self._idempotent("POST /reports", idempotency_key, payload, op)

    def run_accounting(
        self, report_id: str, idempotency_key: Optional[str] = None
    ) -> tuple[dict, bool]:
        report = self._require_report(report_id)
        if report["status"] != "draft":
            raise DomainError(
                409,
                "report_not_draft",
                f"报告状态为 {report['status']}，不能执行新核算；"
                "封存报告的结果不可改写，请创建更正报告或对历史运行重放",
                {"report_id": report_id, "status": report["status"]},
            )

        def op() -> dict:
            run_id = "run_" + uuid.uuid4().hex
            self._execute_and_store(report, run_id, DEFAULT_RULE_VERSION, replay_of=None)
            return self.get_run(run_id)

        return self._idempotent(
            f"POST /reports/{report_id}/runs", idempotency_key, {"report_id": report_id}, op
        )

    def seal_report(self, report_id: str, run_id: Optional[str] = None) -> dict:
        report = self._require_report(report_id)
        if report["status"] == "sealed":
            if run_id is None or report["sealed_run_id"] == run_id:
                return self.get_report(report_id)  # 重复封存同一运行：幂等返回现状
            raise DomainError(
                409,
                "already_sealed",
                "报告已封存，不能改封其他运行",
                {"sealed_run_id": report["sealed_run_id"]},
            )
        if report["status"] != "draft":
            raise DomainError(
                409, "report_not_draft", f"报告状态为 {report['status']}，不能封存"
            )
        run = self.storage.get_run(run_id) if run_id else self.storage.latest_run(report_id)
        if run is None or run["report_id"] != report_id:
            raise DomainError(404, "run_not_found", "报告下不存在可封存的核算运行")

        # 凭证占用复核：本运行占用 + 其他封存报告占用 <= 批次总量。
        # 同口径旧版本报告封存后将被取代，其占用不计入。
        use_by_lot: dict[str, Decimal] = {}
        for alloc in self.storage.run_allocations(run["run_id"]):
            use_by_lot[alloc["lot_id"]] = use_by_lot.get(alloc["lot_id"], ZERO) + Decimal(
                alloc["kwh"]
            )
        same_scope = {
            r["report_id"]
            for r in self.storage.find_scope_reports(
                report["enterprise_id"], report["region"],
                report["period_start"], report["period_end"], report["caliber"],
            )
            if r["report_id"] != report_id
        }
        sealed_by_lot: dict[str, Decimal] = {}
        holders: dict[str, set] = {}
        for row in self.storage.sealed_certificate_use():
            if row["report_id"] in same_scope:
                continue
            sealed_by_lot[row["lot_id"]] = sealed_by_lot.get(row["lot_id"], ZERO) + Decimal(
                row["kwh"]
            )
            holders.setdefault(row["lot_id"], set()).add(row["report_id"])
        overdrawn = []
        for lot_id, used in sorted(use_by_lot.items()):
            lot = self.storage.get_lot(lot_id)
            if lot is None:
                overdrawn.append({"lot_id": lot_id, "problem": "lot_missing"})
                continue
            available = Decimal(lot["available_kwh"])
            others = sealed_by_lot.get(lot_id, ZERO)
            if used + others > available:
                overdrawn.append(
                    {
                        "lot_id": lot_id,
                        "available_kwh": dec_str(available),
                        "sealed_by_others_kwh": dec_str(others),
                        "this_run_kwh": dec_str(used),
                        "held_by_report_ids": sorted(holders.get(lot_id, set())),
                    }
                )
        if overdrawn:
            raise DomainError(
                409,
                "certificate_overdrawn",
                "凭证余量不足，封存被拒绝；该凭证可能已被其他报告封存占用，请重新核算",
                {"overdrawn": overdrawn},
            )
        self.storage.seal_report(report_id, run["run_id"], format_instant(_now()))
        self.storage.supersede_scope_reports(report, except_report_id=report_id)
        return self.get_report(report_id)

    def void_report(self, report_id: str) -> dict:
        report = self._require_report(report_id)
        if report["status"] == "void":
            return self.get_report(report_id)
        if report["status"] != "draft":
            raise DomainError(
                409, "report_not_draft", f"报告状态为 {report['status']}，不能作废"
            )
        self.storage.void_report(report_id)
        return self.get_report(report_id)

    # ---------------- 重放与差异 ----------------

    def replay_run(
        self, run_id: str, idempotency_key: Optional[str] = None
    ) -> tuple[dict, bool]:
        original = self.storage.get_run(run_id)
        if original is None:
            raise DomainError(404, "run_not_found", f"核算运行不存在: {run_id}")
        if original["rule_version"] not in SUPPORTED_RULE_VERSIONS:
            raise DomainError(
                422,
                "rule_version_unsupported",
                f"规则版本不再支持重放: {original['rule_version']}",
            )
        report = self._require_report(original["report_id"])

        def op() -> dict:
            new_run_id = "run_" + uuid.uuid4().hex
            # 排除被重放运行自身的凭证占用，以"原运行视角 + 当前数据"复算
            self._execute_and_store(
                report, new_run_id, original["rule_version"],
                replay_of=run_id, exclude_run_ids={run_id},
            )
            replayed = self.storage.get_run(new_run_id)
            before = json.loads(original["result_json"])
            after = json.loads(replayed["result_json"])
            return {
                "original_run_id": run_id,
                "replay_run_id": new_run_id,
                "report_id": report["report_id"],
                "inputs_changed": original["input_digest"] != replayed["input_digest"],
                "input_digest_before": original["input_digest"],
                "input_digest_after": replayed["input_digest"],
                "totals_diff": _diff_maps(before["totals"], after["totals"]),
                "unmatched_by_reason_diff": _diff_maps(
                    before["unmatched_by_reason"], after["unmatched_by_reason"]
                ),
                "allocation_changes": _diff_allocations(
                    before["allocations"], after["allocations"]
                ),
                "replay_run": self.get_run(new_run_id),
            }

        return self._idempotent(f"POST /runs/{run_id}/replay", idempotency_key, {"run_id": run_id}, op)

    # ---------------- 查询 ----------------

    def get_report(self, report_id: str) -> dict:
        report = self._require_report(report_id)
        runs = [
            {
                "run_id": r["run_id"],
                "created_at": r["created_at"],
                "rule_version": r["rule_version"],
                "input_digest": r["input_digest"],
                "replay_of": r["replay_of"],
                "totals": json.loads(r["result_json"])["totals"],
            }
            for r in self.storage.list_runs(report_id)
        ]
        return {
            "report_id": report["report_id"],
            "enterprise_id": report["enterprise_id"],
            "region": report["region"],
            "period": {"start": report["period_start"], "end": report["period_end"]},
            "caliber": report["caliber"],
            "revision_no": report["revision_no"],
            "status": report["status"],
            "sealed_run_id": report["sealed_run_id"],
            "sealed_at": report["sealed_at"],
            "created_at": report["created_at"],
            "runs": runs,
        }

    def get_run(self, run_id: str) -> dict:
        run = self.storage.get_run(run_id)
        if run is None:
            raise DomainError(404, "run_not_found", f"核算运行不存在: {run_id}")
        return {
            "run_id": run["run_id"],
            "report_id": run["report_id"],
            "rule_version": run["rule_version"],
            "caliber": run["caliber"],
            "input_digest": run["input_digest"],
            "replay_of": run["replay_of"],
            "created_at": run["created_at"],
            "result": json.loads(run["result_json"]),
        }

    def get_lot_usage(self, lot_id: str) -> dict:
        lot = self.storage.get_lot(lot_id)
        if lot is None:
            raise DomainError(404, "lot_not_found", f"凭证批次不存在: {lot_id}")
        per_report: dict[str, Decimal] = {}
        for row in self.storage.sealed_certificate_use():
            if row["lot_id"] == lot_id:
                per_report[row["report_id"]] = per_report.get(
                    row["report_id"], ZERO
                ) + Decimal(row["kwh"])
        consumed = sum(per_report.values(), ZERO)
        available = Decimal(lot["available_kwh"])
        return {
            "lot_id": lot_id,
            "region": lot["region"],
            "available_kwh": dec_str(available),
            "sealed_consumption": [
                {"report_id": rid, "kwh": dec_str(kwh)}
                for rid, kwh in sorted(per_report.items())
            ],
            "remaining_kwh": dec_str(available - consumed),
        }

    # ---------------- 内部：核算执行 ----------------

    def _require_report(self, report_id: str) -> dict:
        report = self.storage.get_report(report_id)
        if report is None:
            raise DomainError(404, "report_not_found", f"报告不存在: {report_id}")
        return report

    def _execute_and_store(
        self,
        report: dict,
        run_id: str,
        rule_version: str,
        replay_of: Optional[str],
        exclude_run_ids: frozenset | set = frozenset(),
    ) -> None:
        caliber = CALIBERS[report["caliber"]]
        granularity = caliber.granularity
        period_start = parse_instant(report["period_start"])
        period_end = parse_instant(report["period_end"])

        consumption = tuple(
            self._to_interval(r) for r in self.storage.effective_intervals(
                "consumption", report["region"], report["period_start"], report["period_end"]
            )
        )
        generation = tuple(
            self._to_interval(r) for r in self.storage.effective_intervals(
                "generation", report["region"], report["period_start"], report["period_end"]
            )
        )
        lots = tuple(
            self._to_lot(r)
            for r in self.storage.lots_overlapping(
                report["period_start"], report["period_end"]
            )
        )

        # 封存占用快照：排除被重放的运行，以及同口径下将被本报告取代的旧版本报告
        superseded_scope = {
            r["report_id"]
            for r in self.storage.find_scope_reports(
                report["enterprise_id"], report["region"],
                report["period_start"], report["period_end"], report["caliber"],
            )
            if r["report_id"] != report["report_id"]
        }
        sealed_cert_use: dict[tuple[str, datetime], Decimal] = {}
        sealed_use_reports: dict[str, set] = {}
        for row in self.storage.sealed_certificate_use():
            if row["run_id"] in exclude_run_ids:
                continue
            if row["report_id"] in superseded_scope:
                continue
            kwh = Decimal(row["kwh"])
            # 占用记录按当时口径的桶存储，折算到本次核算的桶粒度（按重叠时长比例）
            for bucket_start, share in split_interval(
                parse_instant(row["bucket_start"]),
                parse_instant(row["bucket_end"]),
                kwh,
                granularity,
            ):
                key = (row["lot_id"], bucket_start)
                sealed_cert_use[key] = sealed_cert_use.get(key, ZERO) + share
            sealed_use_reports.setdefault(row["lot_id"], set()).add(row["report_id"])

        engine_input = EngineInput(
            rule_version=rule_version,
            caliber=caliber,
            region=report["region"],
            period_start=period_start,
            period_end=period_end,
            consumption=consumption,
            generation=generation,
            lots=lots,
            sealed_cert_use=sealed_cert_use,
            sealed_use_reports={k: tuple(sorted(v)) for k, v in sealed_use_reports.items()},
        )
        input_digest = _digest(self._digest_payload(engine_input))
        result = run_engine(engine_input)

        self.storage.insert_run(
            {
                "run_id": run_id,
                "report_id": report["report_id"],
                "rule_version": rule_version,
                "caliber": caliber.name,
                "input_digest": input_digest,
                "result_json": json.dumps(result, ensure_ascii=False),
                "replay_of": replay_of,
                "created_at": format_instant(_now()),
            }
        )
        step = GRANULARITY_STEP[granularity]
        self.storage.insert_allocations(
            [
                {
                    "run_id": run_id,
                    "lot_id": alloc["lot_id"],
                    "bucket_start": alloc["bucket_start"],
                    "bucket_end": format_instant(parse_instant(alloc["bucket_start"]) + step),
                    "kwh": alloc["kwh"],
                }
                for alloc in result["allocations"]
            ]
        )

    @staticmethod
    def _digest_payload(inp: EngineInput) -> dict:
        return {
            "rule_version": inp.rule_version,
            "caliber": inp.caliber.name,
            "region": inp.region,
            "period_start": format_instant(inp.period_start),
            "period_end": format_instant(inp.period_end),
            "consumption": [
                _interval_json(i)
                for i in sorted(inp.consumption, key=lambda x: (x.meter_id, x.start, x.revision))
            ],
            "generation": [
                _interval_json(i)
                for i in sorted(inp.generation, key=lambda x: (x.meter_id, x.start, x.revision))
            ],
            "lots": [_lot_json(lot) for lot in sorted(inp.lots, key=lambda x: x.lot_id)],
            "sealed_cert_use": [
                {"lot_id": lot_id, "bucket_start": format_instant(bucket), "kwh": dec_str(kwh)}
                for (lot_id, bucket), kwh in sorted(
                    inp.sealed_cert_use.items(), key=lambda kv: (kv[0][0], kv[0][1])
                )
            ],
        }

    @staticmethod
    def _to_interval(row: dict) -> EffectiveInterval:
        return EffectiveInterval(
            meter_id=row["meter_id"],
            kind=row["kind"],
            region=row["region"],
            start=parse_instant(row["starts_at"]),
            end=parse_instant(row["ends_at"]),
            kwh=Decimal(row["kwh"]),
            revision=row["revision"],
        )

    @staticmethod
    def _to_lot(row: dict) -> LotSupply:
        return LotSupply(
            lot_id=row["lot_id"],
            region=row["region"],
            start=parse_instant(row["starts_at"]),
            end=parse_instant(row["ends_at"]),
            available_kwh=Decimal(row["available_kwh"]),
            source_digest=row["source_digest"],
        )

"""业务编排层：导入、报告、封存、重放。

所有写操作都在锁 + 事务内完成；对外抛出 ServiceError 由 API 层映射为 HTTP。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid

from . import db as dbmod
from . import engine
from .contracts import AttributeLot, MeterInterval
from .timeutil import ensure_timezone, iso, now_utc, parse_instant

ENERGY_KINDS = ("consumption", "generation")


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _hash(payload) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class GreenMatchService:
    def __init__(self, db_path: str = "green_match.db"):
        self.db_path = db_path
        self._conn = dbmod.connect(db_path)
        self._lock = threading.RLock()

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------ 导入
    def ingest(self, kind: str, payload: dict) -> dict:
        if kind not in (*ENERGY_KINDS, "certificate"):
            raise ServiceError(422, "invalid_kind", f"未知批次类型: {kind}")
        if not isinstance(payload, dict):
            raise ServiceError(422, "validation_error", "请求体必须是 JSON 对象")
        key = payload.get("idempotency_key")
        if not key:
            raise ServiceError(422, "validation_error", "缺少 idempotency_key")
        region = payload.get("region")
        if not region:
            raise ServiceError(422, "validation_error", "缺少 region")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ServiceError(422, "validation_error", "records 必须是非空数组")

        with self._lock:
            tz = self._resolve_region_timezone(region, payload.get("timezone"))
            try:
                normalized = [self._normalize_record(kind, region, r, tz) for r in records]
            except ValueError as exc:
                raise ServiceError(422, "validation_error", str(exc)) from exc

            content_hash = _hash({"kind": kind, "region": region, "records": normalized})
            existing = self._conn.execute(
                "SELECT * FROM batches WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["content_hash"] != content_hash:
                    raise ServiceError(
                        409, "idempotency_conflict",
                        "幂等键已被不同内容的批次使用",
                    )
                result = json.loads(existing["result_json"])
                result["deduplicated"] = True
                return result

            batch_id = uuid.uuid4().hex
            outcomes = []
            with self._conn:
                # 先落批次行（记录行有外键引用），处理完再回填逐条结果
                self._conn.execute(
                    "INSERT INTO batches (id, idempotency_key, kind, region, content_hash,"
                    " record_count, result_json, created_at) VALUES (?,?,?,?,?,0,'{}',?)",
                    (batch_id, key, kind, region, content_hash, iso(now_utc())),
                )
                for rec in normalized:
                    if kind == "certificate":
                        outcomes.append(self._apply_certificate(batch_id, rec))
                    else:
                        outcomes.append(self._apply_energy_record(batch_id, kind, rec))
                result = {
                    "batch_id": batch_id,
                    "kind": kind,
                    "region": region,
                    "deduplicated": False,
                    "records": outcomes,
                }
                self._conn.execute(
                    "UPDATE batches SET record_count=?, result_json=? WHERE id=?",
                    (len(outcomes), json.dumps(result, ensure_ascii=False), batch_id),
                )
            return result

    def _resolve_region_timezone(self, region: str, tz_name: str | None) -> str:
        row = self._conn.execute("SELECT * FROM regions WHERE code=?", (region,)).fetchone()
        if row:
            if tz_name:
                ensure_timezone(tz_name)
            return tz_name or row["timezone"]
        if not tz_name:
            raise ServiceError(
                422, "validation_error",
                f"区域 {region!r} 首次出现，必须提供 timezone（IANA 名称）",
            )
        ensure_timezone(tz_name)
        with self._conn:
            self._conn.execute(
                "INSERT INTO regions (code, timezone, created_at) VALUES (?,?,?)",
                (region, tz_name, iso(now_utc())),
            )
        return tz_name

    def _normalize_record(self, kind: str, region: str, raw: dict, tz: str) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("记录必须是 JSON 对象")
        try:
            start = parse_instant(raw["start"], tz)
            end = parse_instant(raw["end"], tz)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"时间解析失败: {exc}") from exc
        if end <= start:
            raise ValueError("记录结束时间必须晚于开始时间")
        revision = int(raw.get("revision", 1))
        if revision < 1:
            raise ValueError("revision 必须 >= 1")

        if kind == "certificate":
            cert_no = raw.get("cert_no")
            if not cert_no:
                raise ValueError("凭证缺少 cert_no")
            revoke = bool(raw.get("revoke", False))
            quantity = 0.0 if revoke else self._quantity(raw)
            lot = AttributeLot(
                lot_id=str(cert_no), region=region, starts_at=start, ends_at=end,
                available_kwh=quantity, source_digest=str(raw.get("source_digest") or ""),
            )
            return {
                "region": region,
                "cert_no": lot.lot_id, "start_utc": iso(lot.starts_at), "end_utc": iso(lot.ends_at),
                "quantity_kwh": float(lot.available_kwh), "revision": revision,
                "technology": raw.get("technology"), "revoke": revoke,
                "source_digest": lot.source_digest or None,
            }
        meter_id = raw.get("meter_id")
        if not meter_id:
            raise ValueError("计量记录缺少 meter_id")
        interval = MeterInterval(
            meter_id=str(meter_id), region=region, starts_at=start, ends_at=end,
            kilowatt_hours=self._quantity(raw), revision=revision,
        )
        return {
            "region": region,
            "org_id": str(raw["org_id"]) if raw.get("org_id") else None,
            "meter_id": interval.meter_id, "start_utc": iso(interval.starts_at),
            "end_utc": iso(interval.ends_at), "quantity_kwh": float(interval.kilowatt_hours),
            "revision": interval.revision,
        }

    @staticmethod
    def _quantity(raw: dict) -> float:
        try:
            quantity = float(raw["quantity_kwh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("quantity_kwh 必须是数字") from exc
        if quantity <= 0:
            raise ValueError("quantity_kwh 必须为正数")
        return quantity

    def _apply_energy_record(self, batch_id: str, kind: str, rec: dict) -> dict:
        natural = (kind, rec["meter_id"], rec["start_utc"], rec["end_utc"])
        current = self._conn.execute(
            "SELECT * FROM energy_records WHERE kind=? AND meter_id=? AND start_utc=? AND end_utc=?"
            " AND status='active'",
            (kind, rec["meter_id"], rec["start_utc"], rec["end_utc"]),
        ).fetchone()
        base = {"natural_key": ":".join(str(x) for x in natural), "revision": rec["revision"]}
        if current:
            if current["revision"] == rec["revision"]:
                if abs(current["quantity_kwh"] - rec["quantity_kwh"]) < engine.EPS:
                    return {**base, "outcome": "duplicate", "record_id": current["id"]}
                raise ServiceError(
                    409, "revision_conflict",
                    f"计量区间 {base['natural_key']} 已存在相同 revision 的不同数值，"
                    "请提高 revision 提交修订",
                )
            if current["revision"] > rec["revision"]:
                raise ServiceError(
                    409, "stale_revision",
                    f"计量区间 {base['natural_key']} 当前 revision={current['revision']}，"
                    f"拒绝更旧的 revision={rec['revision']}",
                )
            new_id = uuid.uuid4().hex
            self._conn.execute(
                "UPDATE energy_records SET status='superseded', superseded_by=? WHERE id=?",
                (new_id, current["id"]),
            )
            self._insert_energy(batch_id, new_id, kind, rec)
            return {
                **base, "outcome": "superseded_previous", "record_id": new_id,
                "superseded_record_id": current["id"],
                "previous_quantity_kwh": current["quantity_kwh"],
            }
        new_id = uuid.uuid4().hex
        self._insert_energy(batch_id, new_id, kind, rec)
        return {**base, "outcome": "inserted", "record_id": new_id}

    def _insert_energy(self, batch_id: str, record_id: str, kind: str, rec: dict) -> None:
        self._conn.execute(
            "INSERT INTO energy_records (id, batch_id, kind, region, org_id, meter_id, start_utc,"
            " end_utc, quantity_kwh, revision, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'active',?)",
            (record_id, batch_id, kind, rec["region"], rec.get("org_id"),
             rec["meter_id"], rec["start_utc"], rec["end_utc"], rec["quantity_kwh"],
             rec["revision"], iso(now_utc())),
        )

    def _apply_certificate(self, batch_id: str, rec: dict) -> dict:
        current = self._conn.execute(
            "SELECT * FROM certificates WHERE cert_no=? AND status='active' ORDER BY revision DESC",
            (rec["cert_no"],),
        ).fetchone()
        base = {"natural_key": rec["cert_no"], "revision": rec["revision"]}
        if rec["revoke"]:
            if not current:
                raise ServiceError(422, "unknown_certificate",
                                   f"凭证 {rec['cert_no']} 不存在，无法注销")
            if current["revision"] >= rec["revision"]:
                raise ServiceError(409, "stale_revision",
                                   f"凭证 {rec['cert_no']} 注销需要更高的 revision")
            self._supersede_cert(current)
            new_id = self._insert_cert(batch_id, rec, status="revoked")
            return {**base, "outcome": "revoked", "record_id": new_id,
                    "superseded_record_id": current["id"]}
        if current:
            if current["revision"] == rec["revision"]:
                same = (
                    abs(current["quantity_kwh"] - rec["quantity_kwh"]) < engine.EPS
                    and current["start_utc"] == rec["start_utc"]
                    and current["end_utc"] == rec["end_utc"]
                )
                if same:
                    return {**base, "outcome": "duplicate", "record_id": current["id"]}
                raise ServiceError(
                    409, "revision_conflict",
                    f"凭证 {rec['cert_no']} 已存在相同 revision 的不同内容，请提高 revision",
                )
            if current["revision"] > rec["revision"]:
                raise ServiceError(409, "stale_revision",
                                   f"凭证 {rec['cert_no']} 当前 revision={current['revision']}，"
                                   f"拒绝更旧的 revision={rec['revision']}")
            self._supersede_cert(current)
            new_id = self._insert_cert(batch_id, rec)
            return {**base, "outcome": "superseded_previous", "record_id": new_id,
                    "superseded_record_id": current["id"],
                    "previous_quantity_kwh": current["quantity_kwh"]}
        new_id = self._insert_cert(batch_id, rec)
        return {**base, "outcome": "inserted", "record_id": new_id}

    def _supersede_cert(self, current) -> None:
        self._conn.execute("UPDATE certificates SET status='superseded' WHERE id=?",
                           (current["id"],))

    def _insert_cert(self, batch_id: str, rec: dict, status: str = "active") -> str:
        new_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO certificates (id, batch_id, cert_no, region, technology, start_utc,"
            " end_utc, quantity_kwh, revision, status, source_digest, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, batch_id, rec["cert_no"], rec["region"],
             rec.get("technology"), rec["start_utc"], rec["end_utc"], rec["quantity_kwh"],
             rec["revision"], status, rec.get("source_digest"), iso(now_utc())),
        )
        return new_id

    # ------------------------------------------------------------------ 报告
    def create_report(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError(422, "validation_error", "请求体必须是 JSON 对象")
        missing = [f for f in ("idempotency_key", "org_id", "region",
                               "period_start", "period_end", "rule_version")
                   if not payload.get(f)]
        if missing:
            raise ServiceError(422, "validation_error", f"缺少字段: {', '.join(missing)}")

        with self._lock:
            region_row = self._conn.execute(
                "SELECT * FROM regions WHERE code=?", (payload["region"],)
            ).fetchone()
            if not region_row:
                raise ServiceError(422, "unknown_region",
                                   f"区域 {payload['region']!r} 尚未通过数据导入注册")
            rule = self._conn.execute(
                "SELECT * FROM rule_sets WHERE version=?", (payload["rule_version"],)
            ).fetchone()
            if not rule:
                raise ServiceError(422, "unknown_rule_version",
                                   f"未知核算口径 {payload['rule_version']!r}，"
                                   "可用口径见 GET /api/rules")
            try:
                p_start = parse_instant(payload["period_start"], region_row["timezone"])
                p_end = parse_instant(payload["period_end"], region_row["timezone"])
            except ValueError as exc:
                raise ServiceError(422, "validation_error", f"周期解析失败: {exc}") from exc
            if p_end <= p_start:
                raise ServiceError(422, "validation_error", "period_end 必须晚于 period_start")

            param_hash = _hash({
                "org_id": payload["org_id"], "region": payload["region"],
                "period_start_utc": iso(p_start), "period_end_utc": iso(p_end),
                "rule_version": payload["rule_version"],
            })
            existing = self._conn.execute(
                "SELECT * FROM reports WHERE idempotency_key=?", (payload["idempotency_key"],)
            ).fetchone()
            if existing:
                if existing["param_hash"] != param_hash:
                    raise ServiceError(409, "idempotency_conflict",
                                       "幂等键已被不同参数的报告使用")
                return {**self.get_report(existing["id"]), "deduplicated": True}

            report_id = uuid.uuid4().hex
            with self._conn:
                self._conn.execute(
                    "INSERT INTO reports (id, idempotency_key, param_hash, org_id, region,"
                    " period_start_utc, period_end_utc, rule_version, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,'draft',?)",
                    (report_id, payload["idempotency_key"], param_hash, payload["org_id"],
                     payload["region"], iso(p_start), iso(p_end), payload["rule_version"],
                     iso(now_utc())),
                )
            self._execute_and_store(report_id, "initial")
            return {**self.get_report(report_id), "deduplicated": False}

    def seal_report(self, report_id: str) -> dict:
        with self._lock:
            report = self._require_report(report_id)
            if report["status"] == "sealed":
                # 封存是幂等操作：重复封存返回已封存结果，不产生新核算
                return {**self.get_report(report_id), "already_sealed": True}
            run_id, result, _ = self._execute_and_store(report_id, "seal")
            with self._conn:
                for alloc in result["allocations"]:
                    self._conn.execute(
                        "INSERT INTO cert_locks (certificate_id, report_id, bucket_start_utc,"
                        " quantity_kwh, run_id) VALUES (?,?,?,?,?)",
                        (alloc["certificate_id"], report_id, alloc["bucket_start_utc"],
                         alloc["quantity_kwh"], run_id),
                    )
                self._conn.execute(
                    "UPDATE reports SET status='sealed', sealed_run_id=?, sealed_at=? WHERE id=?",
                    (run_id, iso(now_utc()), report_id),
                )
            return {**self.get_report(report_id), "already_sealed": False}

    def replay_report(self, report_id: str) -> dict:
        """按报告原定口径重放核算，并与基准（封存结果，否则首次结果）对照。"""
        with self._lock:
            report = self._require_report(report_id)
            baseline = self._baseline_run(report)
            run_id, result, digest = self._execute_and_store(report_id, "replay")
            base_inputs = self._run_inputs(baseline["id"])
            new_inputs = self._run_inputs(run_id)
            base_result = json.loads(baseline["result_json"])
            diff = engine.diff_results(base_result, base_inputs, result, new_inputs)
            diff["digest_equal"] = baseline["input_digest"] == digest
            diff["explanation"] = self._explain(diff)
            return {
                "report_id": report_id,
                "baseline_run_id": baseline["id"],
                "replay_run_id": run_id,
                "sealed_result_untouched": report["status"] == "sealed",
                "diff": diff,
                "replay_totals": result["totals"],
            }

    @staticmethod
    def _explain(diff: dict) -> str:
        if diff["digest_equal"] and not any(diff["totals_delta"].values()):
            return "输入摘要与核算结果均未变化"
        parts = []
        changes = diff["input_changes"]
        if changes["added"]:
            parts.append(f"新增输入 {len(changes['added'])} 条")
        if changes["removed"]:
            parts.append(f"失效/被修订输入 {len(changes['removed'])} 条")
        if changes["quantity_changed"]:
            parts.append(f"数值变化 {len(changes['quantity_changed'])} 条")
        if diff["unmatched_added"]:
            parts.append(f"新增未匹配 {len(diff['unmatched_added'])} 桶")
        if diff["unmatched_resolved"]:
            parts.append(f"消除未匹配 {len(diff['unmatched_resolved'])} 桶")
        return "；".join(parts) or "输入摘要变化，但总量结果一致"

    # ------------------------------------------------------------------ 查询
    def get_report(self, report_id: str) -> dict:
        report = self._require_report(report_id)
        runs = self._conn.execute(
            "SELECT id, kind, rule_version, input_digest, result_json, created_at"
            " FROM runs WHERE report_id=? ORDER BY created_at, rowid",
            (report_id,),
        ).fetchall()
        run_summaries = [
            {
                "run_id": r["id"], "kind": r["kind"], "rule_version": r["rule_version"],
                "input_digest": r["input_digest"], "created_at": r["created_at"],
                "totals": json.loads(r["result_json"])["totals"],
            }
            for r in runs
        ]
        return {
            "report": {
                "report_id": report["id"],
                "org_id": report["org_id"],
                "region": report["region"],
                "period_start_utc": report["period_start_utc"],
                "period_end_utc": report["period_end_utc"],
                "rule_version": report["rule_version"],
                "status": report["status"],
                "created_at": report["created_at"],
                "sealed_at": report["sealed_at"],
                "sealed_run_id": report["sealed_run_id"],
            },
            "runs": run_summaries,
        }

    def get_run(self, run_id: str) -> dict:
        run = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ServiceError(404, "run_not_found", f"核算运行 {run_id} 不存在")
        return {
            "run_id": run["id"],
            "report_id": run["report_id"],
            "kind": run["kind"],
            "rule_version": run["rule_version"],
            "input_digest": run["input_digest"],
            "created_at": run["created_at"],
            "result": json.loads(run["result_json"]),
            "inputs": self._run_inputs(run_id),
        }

    def list_rules(self) -> list:
        rows = self._conn.execute(
            "SELECT version, description, config_json, created_at FROM rule_sets ORDER BY version"
        ).fetchall()
        return [
            {"version": r["version"], "description": r["description"],
             "config": json.loads(r["config_json"]), "created_at": r["created_at"]}
            for r in rows
        ]

    # ------------------------------------------------------------------ 内部
    def _require_report(self, report_id: str):
        report = self._conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report:
            raise ServiceError(404, "report_not_found", f"报告 {report_id} 不存在")
        return report

    def _baseline_run(self, report):
        if report["sealed_run_id"]:
            return self._conn.execute(
                "SELECT * FROM runs WHERE id=?", (report["sealed_run_id"],)
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM runs WHERE report_id=? ORDER BY created_at, rowid LIMIT 1",
            (report["id"],),
        ).fetchone()

    def _execute_and_store(self, report_id: str, kind: str):
        report = self._require_report(report_id)
        rule = self._conn.execute(
            "SELECT * FROM rule_sets WHERE version=?", (report["rule_version"],)
        ).fetchone()
        result, inputs, digest = engine.execute(
            self._conn,
            report_id=report_id,
            org_id=report["org_id"],
            region=report["region"],
            period_start_utc=report["period_start_utc"],
            period_end_utc=report["period_end_utc"],
            rule_version=report["rule_version"],
            rule_config=json.loads(rule["config_json"]),
            kind=kind,
        )
        run_id = uuid.uuid4().hex
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (id, report_id, kind, rule_version, input_digest,"
                " result_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, report_id, kind, report["rule_version"], digest,
                 json.dumps(result, ensure_ascii=False), iso(now_utc())),
            )
            self._conn.executemany(
                "INSERT INTO run_inputs (run_id, source_type, source_id, natural_key,"
                " revision, quantity_kwh) VALUES (?,?,?,?,?,?)",
                [(run_id, i["source_type"], i["source_id"], i["natural_key"],
                  i["revision"], i["quantity_kwh"]) for i in inputs],
            )
        return run_id, result, digest

    def _run_inputs(self, run_id: str) -> list:
        rows = self._conn.execute(
            "SELECT source_type, source_id, natural_key, revision, quantity_kwh"
            " FROM run_inputs WHERE run_id=? ORDER BY source_type, natural_key",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

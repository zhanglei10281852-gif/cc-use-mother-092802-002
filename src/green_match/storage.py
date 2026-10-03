"""SQLite 持久化层。

所有领域状态（计量区间全部修订、凭证批次、报告、核算运行、凭证占用、
幂等键）都写入同一个 SQLite 数据库；进程重启后重新打开文件即可完整恢复。
本层只做存取与约束检查，业务编排位于 service 层。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator, Optional

from .errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meter_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('consumption', 'generation')),
    region TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    kwh TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (meter_id, kind, starts_at, ends_at, revision)
);
CREATE TABLE IF NOT EXISTS attribute_lots (
    lot_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    available_kwh TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL,
    region TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    caliber TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'sealed', 'superseded', 'void')),
    sealed_run_id TEXT,
    created_at TEXT NOT NULL,
    sealed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_scope
    ON reports (enterprise_id, region, period_start, period_end, caliber);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports (report_id),
    rule_version TEXT NOT NULL,
    caliber TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    replay_of TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_report ON runs (report_id, created_at);
CREATE TABLE IF NOT EXISTS allocations (
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    lot_id TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    bucket_end TEXT NOT NULL,
    kwh TEXT NOT NULL,
    PRIMARY KEY (run_id, lot_id, bucket_start)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    endpoint TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (endpoint, idem_key)
);
"""

_EFFECTIVE_SQL = """
SELECT m.* FROM meter_intervals m
JOIN (
    SELECT meter_id, kind, starts_at, ends_at, MAX(revision) AS max_rev
    FROM meter_intervals
    GROUP BY meter_id, kind, starts_at, ends_at
) latest
  ON m.meter_id = latest.meter_id AND m.kind = latest.kind
 AND m.starts_at = latest.starts_at AND m.ends_at = latest.ends_at
 AND m.revision = latest.max_rev
WHERE m.kind = ? AND m.region = ? AND m.ends_at > ? AND m.starts_at < ?
ORDER BY m.meter_id, m.starts_at
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    # ---------------- 计量区间 ----------------

    def add_meter_intervals(self, rows: list[dict]) -> dict:
        """批量写入计量区间；整批原子：完全相同的行去重，同键不同值则整批回滚。"""
        inserted = 0
        duplicates = 0
        conflicts = []
        with self._connect() as conn:
            for row in rows:
                try:
                    conn.execute(
                        """INSERT INTO meter_intervals
                           (meter_id, kind, region, starts_at, ends_at, kwh, revision, source, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["meter_id"], row["kind"], row["region"],
                            row["starts_at"], row["ends_at"], row["kwh"],
                            row["revision"], row.get("source"), row["created_at"],
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        """SELECT region, kwh FROM meter_intervals
                           WHERE meter_id=? AND kind=? AND starts_at=? AND ends_at=? AND revision=?""",
                        (row["meter_id"], row["kind"], row["starts_at"], row["ends_at"], row["revision"]),
                    ).fetchone()
                    if existing is not None and (
                        existing["region"] != row["region"]
                        or Decimal(existing["kwh"]) != Decimal(row["kwh"])
                    ):
                        conflicts.append(
                            {
                                "meter_id": row["meter_id"],
                                "kind": row["kind"],
                                "starts_at": row["starts_at"],
                                "ends_at": row["ends_at"],
                                "revision": row["revision"],
                                "stored_kwh": existing["kwh"],
                                "submitted_kwh": row["kwh"],
                            }
                        )
                    else:
                        duplicates += 1
            if conflicts:
                raise DomainError(
                    409,
                    "meter_revision_conflict",
                    "同一计量区间以相同 revision 提交了不同数值；更正计量数据必须递增 revision",
                    {"conflicts": conflicts},
                )
        return {"inserted": inserted, "duplicates": duplicates}

    def effective_intervals(
        self, kind: str, region: str, period_start: str, period_end: str
    ) -> list[dict]:
        """取某区域与周期相交、每个区间键最高 revision 的有效计量区间。"""
        with self._connect() as conn:
            rows = conn.execute(
                _EFFECTIVE_SQL, (kind, region, period_start, period_end)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 凭证批次 ----------------

    def add_lots(self, rows: list[dict]) -> dict:
        """批量写入凭证批次；批次不可变，同 lot_id 不同内容则整批回滚。"""
        inserted = 0
        duplicates = 0
        conflicts = []
        with self._connect() as conn:
            for row in rows:
                try:
                    conn.execute(
                        """INSERT INTO attribute_lots
                           (lot_id, region, starts_at, ends_at, available_kwh, source_digest, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["lot_id"], row["region"], row["starts_at"], row["ends_at"],
                            row["available_kwh"], row["source_digest"], row["created_at"],
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT * FROM attribute_lots WHERE lot_id = ?", (row["lot_id"],)
                    ).fetchone()
                    same = (
                        existing is not None
                        and existing["region"] == row["region"]
                        and existing["starts_at"] == row["starts_at"]
                        and existing["ends_at"] == row["ends_at"]
                        and Decimal(existing["available_kwh"]) == Decimal(row["available_kwh"])
                        and existing["source_digest"] == row["source_digest"]
                    )
                    if same:
                        duplicates += 1
                    else:
                        conflicts.append({"lot_id": row["lot_id"]})
            if conflicts:
                raise DomainError(
                    409,
                    "lot_conflict",
                    "凭证批次不可变：lot_id 已存在且内容不同，请更换 lot_id 重新导入",
                    {"conflicts": conflicts},
                )
        return {"inserted": inserted, "duplicates": duplicates}

    def lots_overlapping(self, period_start: str, period_end: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM attribute_lots
                   WHERE ends_at > ? AND starts_at < ? ORDER BY lot_id""",
                (period_start, period_end),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_lot(self, lot_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attribute_lots WHERE lot_id = ?", (lot_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------- 报告 ----------------

    def insert_report(self, record: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO reports
                   (report_id, enterprise_id, region, period_start, period_end,
                    caliber, revision_no, status, sealed_run_id, created_at, sealed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["report_id"], record["enterprise_id"], record["region"],
                    record["period_start"], record["period_end"], record["caliber"],
                    record["revision_no"], record["status"], record["sealed_run_id"],
                    record["created_at"], record["sealed_at"],
                ),
            )

    def get_report(self, report_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_scope_reports(
        self,
        enterprise_id: str,
        region: str,
        period_start: str,
        period_end: str,
        caliber: str,
    ) -> list[dict]:
        """同企业、同区域、同周期、同口径的全部报告版本。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM reports
                   WHERE enterprise_id=? AND region=? AND period_start=?
                         AND period_end=? AND caliber=?
                   ORDER BY revision_no""",
                (enterprise_id, region, period_start, period_end, caliber),
            ).fetchall()
        return [dict(r) for r in rows]

    def seal_report(self, report_id: str, run_id: str, sealed_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reports SET status='sealed', sealed_run_id=?, sealed_at=? WHERE report_id=?",
                (run_id, sealed_at, report_id),
            )

    def void_report(self, report_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reports SET status='void' WHERE report_id=?", (report_id,)
            )

    def supersede_scope_reports(self, report: dict, except_report_id: str) -> None:
        """同口径下新报告封存后，旧封存报告标记为被取代（结果保留可查，凭证占用释放）。"""
        with self._connect() as conn:
            conn.execute(
                """UPDATE reports SET status='superseded'
                   WHERE status='sealed' AND enterprise_id=? AND region=?
                         AND period_start=? AND period_end=? AND caliber=?
                         AND report_id != ?""",
                (
                    report["enterprise_id"], report["region"], report["period_start"],
                    report["period_end"], report["caliber"], except_report_id,
                ),
            )

    # ---------------- 核算运行与凭证占用 ----------------

    def insert_run(self, record: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO runs
                   (run_id, report_id, rule_version, caliber, input_digest,
                    result_json, replay_of, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["run_id"], record["report_id"], record["rule_version"],
                    record["caliber"], record["input_digest"], record["result_json"],
                    record["replay_of"], record["created_at"],
                ),
            )

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run(self, report_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE report_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (report_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, report_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE report_id = ? ORDER BY created_at, rowid",
                (report_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_allocations(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO allocations (run_id, lot_id, bucket_start, bucket_end, kwh)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (r["run_id"], r["lot_id"], r["bucket_start"], r["bucket_end"], r["kwh"])
                    for r in rows
                ],
            )

    def run_allocations(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM allocations WHERE run_id = ?", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def sealed_certificate_use(self) -> list[dict]:
        """当前处于 sealed 状态报告的封存运行所占用的全部凭证量。

        被取代（superseded）的报告不再占用凭证，但其运行与占用记录仍保留可查。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.run_id, a.lot_id, a.bucket_start, a.bucket_end, a.kwh, r.report_id
                   FROM allocations a
                   JOIN reports r ON r.sealed_run_id = a.run_id
                   WHERE r.status = 'sealed'"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 幂等键 ----------------

    def get_idempotency(self, endpoint: str, key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE endpoint = ? AND idem_key = ?",
                (endpoint, key),
            ).fetchone()
        return dict(row) if row else None

    def put_idempotency(
        self, endpoint: str, key: str, request_hash: str, response_json: str, created_at: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO idempotency_keys
                   (endpoint, idem_key, request_hash, response_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (endpoint, key, request_hash, response_json, created_at),
            )

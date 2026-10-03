"""SQLite 持久化层。

设计原则：
- 只增不改（append-only）：计量修订通过“新版本取代旧版本”实现，旧行保留
  并标记 superseded；封存（sealed）的核算结果永不被后续导入改写。
- 幂等：批次与报告都携带调用方幂等键，重复请求返回首次结果。
- 可恢复：全部状态在单个 SQLite 文件中，进程重启后 schema 幂等重建、
  规则集按版本种子化，业务数据原样恢复。
"""

from __future__ import annotations

import sqlite3

from .rules import RULE_SETS
from .timeutil import iso, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    code        TEXT PRIMARY KEY,
    timezone    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_sets (
    version      TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,              -- consumption | generation | certificate
    region          TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    record_count    INTEGER NOT NULL,
    result_json     TEXT NOT NULL,              -- 首次导入的逐条处理结果，重复请求原样返回
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS energy_records (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    kind          TEXT NOT NULL,                -- consumption | generation
    region        TEXT NOT NULL,
    org_id        TEXT,                         -- 归属企业；NULL 表示区域共享数据
    meter_id      TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    quantity_kwh  REAL NOT NULL,
    revision      INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | superseded
    superseded_by TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (kind, region, meter_id, start_utc, end_utc, revision)
);
CREATE INDEX IF NOT EXISTS idx_energy_active
    ON energy_records (kind, region, status, start_utc, end_utc);

CREATE TABLE IF NOT EXISTS certificates (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    cert_no       TEXT NOT NULL,
    region        TEXT NOT NULL,
    technology    TEXT,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    quantity_kwh  REAL NOT NULL,
    revision      INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | superseded | revoked
    source_digest TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (cert_no, revision)
);
CREATE INDEX IF NOT EXISTS idx_cert_active
    ON certificates (region, status, start_utc, end_utc);

CREATE TABLE IF NOT EXISTS reports (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    param_hash      TEXT NOT NULL,
    org_id          TEXT NOT NULL,
    region          TEXT NOT NULL,
    period_start_utc TEXT NOT NULL,
    period_end_utc   TEXT NOT NULL,
    rule_version    TEXT NOT NULL REFERENCES rule_sets(version),
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft | sealed
    sealed_run_id   TEXT,
    created_at      TEXT NOT NULL,
    sealed_at       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    report_id    TEXT NOT NULL REFERENCES reports(id),
    kind         TEXT NOT NULL,               -- initial | seal | replay
    rule_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_report ON runs (report_id, created_at);

-- 每次核算看到的输入快照（含他方封存锁），用于重放时解释差异来源。
CREATE TABLE IF NOT EXISTS run_inputs (
    run_id       TEXT NOT NULL REFERENCES runs(id),
    source_type  TEXT NOT NULL,               -- energy_record | certificate | external_lock
    source_id    TEXT NOT NULL,
    natural_key  TEXT NOT NULL,
    revision     INTEGER,
    quantity_kwh REAL NOT NULL,
    PRIMARY KEY (run_id, source_type, source_id)
);

-- 封存报告对凭证的核销锁：其他报告核算时必须让出这部分额度。
CREATE TABLE IF NOT EXISTS cert_locks (
    certificate_id  TEXT NOT NULL,
    report_id       TEXT NOT NULL,
    bucket_start_utc TEXT NOT NULL,
    quantity_kwh    REAL NOT NULL,
    run_id          TEXT NOT NULL,
    PRIMARY KEY (certificate_id, report_id, bucket_start_utc)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    seed_rule_sets(conn)
    return conn


def seed_rule_sets(conn: sqlite3.Connection) -> None:
    """内置口径按版本种子化；已存在的版本绝不覆盖（口径不可变）。"""
    import json

    now = iso(now_utc())
    with conn:
        for rule in RULE_SETS:
            conn.execute(
                "INSERT OR IGNORE INTO rule_sets (version, description, config_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (rule["version"], rule["description"], json.dumps(rule["config"], sort_keys=True), now),
            )

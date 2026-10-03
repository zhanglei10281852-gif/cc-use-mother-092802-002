# 分时绿电核算

本项目面向企业能源核算人员，描述用电区间、发电属性和凭证来源。数据修订与核算口径变化需要能够区分。

领域对象表达分时计量区间、区域和属性凭证批次，供用电与清洁电力属性对账使用。

## 服务概述

`green_match` 是一个绿电属性核算后端：按区域和时间区间接收用电、发电与凭证批次，
把计量区间拆分到分时段桶后与凭证匹配，产出带未匹配原因归因的核算结果。
每次核算都记录规则版本与输入摘要（SHA-256），封存（seal）后的结果不随新数据改写；
运维可通过重放（replay）接口按原口径重新核算并对照前后差异。

### 核心语义

- **时区边界**：记录可带偏移量或使用 naive 时间（按批次/区域 IANA 时区解释），
  一律归一化为 UTC 存储；报告周期按区域时区换算，夏令时切换日自然得到 23/25 个小时桶。
- **跨时段拆分**：跨报告周期边界的计量区间按秒比例拆分到各桶，不丢不重。
- **计量修订**：同一计量区间（kind+meter+区间）以 `revision` 递增取代旧值，
  旧行保留为 `superseded`；同 revision 不同数值、或更旧 revision 均被拒绝（409）。
  凭证同理，并支持 `revoke` 注销。
- **凭证争用**：封存时对凭证建立核销锁（cert_locks）。后封存的报告只能使用剩余额度，
  未匹配原因会明确标为 `certificate_locked_by_other_report`。
- **核算口径**：报告创建时选定规则版本（`hourly-match@1` 逐小时匹配 /
  `monthly-net@1` 周期净额），口径随报告固化，规则集只增不改。
- **封存不可改写**：seal 是幂等的一次性操作；之后的导入、修订、注销都不触碰封存结果，
  只能通过 replay 产生新的运行记录并输出差异说明。
- **幂等**：批次与报告都按 `idempotency_key` 去重——同键同内容返回首次结果
  （`deduplicated: true`），同键不同内容返回 409；记录级自然键去重保证换键重导也安全。
- **可恢复**：全部状态在单个 SQLite 文件（WAL），进程重启后 schema 幂等重建、
  规则集按版本种子化，业务数据原样恢复。

### 未匹配原因

| reason | 含义 |
| --- | --- |
| `no_supply_in_region` | 该区域该时段没有任何有效凭证或自发电 |
| `certificate_not_covering_interval` | 区域内有凭证，但有效时段未覆盖该用电桶 |
| `certificate_exhausted` | 覆盖该时段的凭证额度已核销完毕 |
| `certificate_locked_by_other_report` | 凭证额度已被其他封存报告核销锁定 |

## 运行

```bash
pip install -r requirements.txt        # 或 python -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=src GREEN_MATCH_DB=green_match.db python -m green_match   # 默认 127.0.0.1:8000
```

运行测试：`python -m unittest discover -s tests -v`
（HTTP 层测试需要 fastapi/httpx；未安装时自动跳过，核心逻辑测试仅依赖标准库）

编译检查：`python -m compileall -q src tests`

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/rules` | 列出可选核算口径（规则版本） |
| POST | `/api/ingest/{kind}` | 导入批次，kind ∈ `consumption` / `generation` / `certificates` |
| POST | `/api/reports` | 创建报告（选择口径），立即产出 initial 核算预览 |
| GET | `/api/reports/{id}` | 报告详情与历次核算摘要 |
| POST | `/api/reports/{id}/seal` | 封存报告并建立凭证核销锁（幂等） |
| POST | `/api/reports/{id}/replay` | 按原口径重放，输出与基准（封存/首次）结果的差异 |
| GET | `/api/runs/{id}` | 单次核算完整结果、输入快照与输入摘要 |

### 导入批次 `POST /api/ingest/{kind}`

```json
{
  "idempotency_key": "batch-2026-03-01-a",
  "region": "cn-north",
  "timezone": "Asia/Shanghai",
  "records": [
    {"meter_id": "M1", "start": "2026-03-01T00:00", "end": "2026-03-01T01:00",
     "quantity_kwh": 120.5, "revision": 1, "org_id": "org-a"}
  ]
}
```

- 计量修订：同一区间以更高 `revision` 重新提交即可取代旧值。
- 凭证批次记录字段为 `cert_no`（自然键）、`technology`、`source_digest`；
  注销凭证提交 `{"cert_no": "...", "revoke": true, "revision": <更大值>}`。
- `org_id` 缺省为空表示区域共享数据；报告只统计本企业记录与共享记录。

### 创建报告 `POST /api/reports`

```json
{
  "idempotency_key": "report-2026-03-org-a",
  "org_id": "org-a",
  "region": "cn-north",
  "period_start": "2026-03-01",
  "period_end": "2026-04-01",
  "rule_version": "hourly-match@1"
}
```

naive 的周期边界按区域注册时区解释。响应包含报告与 initial 运行摘要。

### 重放差异 `POST /api/reports/{id}/replay`

```json
{
  "baseline_run_id": "...", "replay_run_id": "...",
  "sealed_result_untouched": true,
  "diff": {
    "digest_equal": false,
    "totals_delta": {"consumption_kwh": -40.0, ...},
    "unmatched_added": [...], "unmatched_resolved": [...],
    "input_changes": {"added": [...], "removed": [...], "quantity_changed": [...]},
    "explanation": "新增输入 1 条；失效/被修订输入 1 条"
  }
}
```

## 代码结构

```
src/green_match/
  contracts.py   # 领域数据契约（计量区间、凭证批次）
  timeutil.py    # 时区解析、UTC 归一化、切桶
  rules.py       # 内置核算口径（只增不改）
  db.py          # SQLite schema 与种子化
  engine.py      # 无状态匹配引擎 + 结果 diff
  service.py     # 业务编排：导入/报告/封存/重放，幂等与事务
  api.py         # FastAPI 协议层
  __main__.py    # 启动入口
tests/
  test_contracts.py  # 契约测试
  test_service.py    # 全链路业务测试（纯标准库）
  test_api.py        # HTTP 层测试
```

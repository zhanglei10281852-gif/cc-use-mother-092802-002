# 分时绿电核算

面向企业能源核算人员的绿电属性核算服务：按区域和时间区间接收用电、发电与凭证批次，
在同一 UTC 时间桶内完成清洁电力属性匹配，并为每次核算保存规则版本与输入摘要，
支撑"月度采购量够不代表每个时段都有绿电"的分时核对场景。

## 核心语义

- **时区与分桶**：所有时间必须携带时区，入库即归一到 UTC；分桶（小时/日）只在
  UTC 轴上进行，跨时段区间按各桶重叠时长比例拆分，不受夏令时影响。
- **计量修订**：同一 `(meter_id, kind, starts_at, ends_at)` 区间键只认最高
  `revision`；同 revision 不同数值的重复提交返回 409，全部修订历史保留在库中。
- **核算口径（caliber）**：企业创建报告时选择：
  - `hourly_same_region`：逐小时、同区域、凭证须覆盖同一小时；
  - `daily_same_region`：逐 UTC 日、同区域；
  - `period_pool_same_region`：同区域凭证在报告期内池化；
  - `period_pool_grid`：允许跨区凭证、报告期内池化。
- **规则版本**：每次核算运行记录 `rule_version` 与输入摘要
  （`sha256:` 前缀的规范化输入哈希）。输入任何变化（新数据、修订、封存占用变化）
  都会改变摘要，旧报告因此总能解释"为什么变了"。
- **封存不可变**：报告封存后其结果永不改写；新数据只影响新运行。
  同口径更正需创建新版本报告（`revision_no` 递增），封存新版本后旧报告标记为
  `superseded`，其结果与核算历史仍完整可查。
- **凭证争用**：凭证批次不可变；封存报告占用凭证量，草稿核算只看到剩余量；
  封存时复核余量，被其他报告抢先封存导致超用时返回 409
  （`certificate_overdrawn`），需重新核算后再封存。
- **未匹配原因**：结果按桶给出未匹配电量及原因码——
  `no_certificate_covering_period`（无凭证覆盖）、`region_mismatch`（口径要求
  同区但只有跨区凭证）、`certificate_exhausted`（凭证耗尽）、
  `certificate_claimed_by_sealed_report`（已被其他封存报告占用）。
- **幂等**：写接口支持 `Idempotency-Key` 请求头，重放返回首个响应并带
  `X-Idempotent-Replay: true`；同键不同请求体返回 409。即使不带键，
  完全相同的导入行也会按唯一约束去重。
- **持久化**：全部状态（含幂等键）存储于 SQLite（WAL 模式），
  进程重启后打开同一数据库文件即可完整恢复。

## 运行

```bash
pip install fastapi uvicorn
PYTHONPATH=src GREEN_MATCH_DB=green_match.db python -m green_match
# 监听 127.0.0.1:8000，可用 HOST/PORT 环境变量调整
```

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/calibers` | 可用口径与规则版本 |
| POST | `/imports/meter-intervals` | 导入用电/发电计量区间（`kind` 区分） |
| POST | `/imports/attribute-lots` | 导入属性凭证批次（不可变） |
| POST | `/reports` | 创建报告（选择口径，草稿） |
| GET | `/reports/{id}` | 报告详情与全部核算历史（含每次运行的输入摘要） |
| POST | `/reports/{id}/runs` | 对当前有效数据执行一次核算 |
| POST | `/reports/{id}/seal` | 封存最新（或指定）运行，复核凭证余量 |
| POST | `/reports/{id}/void` | 作废草稿 |
| GET | `/runs/{id}` | 单次核算完整结果（含未匹配原因） |
| POST | `/runs/{id}/replay` | 按原规则版本对当前数据重放，返回前后差异 |
| GET | `/lots/{id}/usage` | 凭证批次被哪些封存报告占用、剩余多少 |

错误响应统一为 `{"error": {"code", "message", "details"}}`。

## 测试与检查

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

## 代码结构

```
src/green_match/
  contracts.py   计量区间与凭证批次的数据契约
  timeutil.py    时区归一、UTC 分桶、跨时段拆分
  rules.py       核算口径与规则版本注册表
  engine.py      纯函数匹配引擎（按规则版本分发，可重放）
  storage.py     SQLite 持久化（计量、凭证、报告、运行、占用、幂等键）
  service.py     编排层：导入、核算、封存、重放、审计查询
  api.py         FastAPI HTTP 接口
```

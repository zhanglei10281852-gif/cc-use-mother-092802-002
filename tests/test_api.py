"""HTTP API 测试：协议层行为（状态码、幂等重放、错误映射）。

需要 fastapi/httpx；未安装时整模块跳过（核心逻辑由 test_service.py 覆盖）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
    from green_match.api import create_app
except ImportError:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 未安装")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "api.db")
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        self._tmp.cleanup()

    def test_health_and_rules(self):
        self.assertEqual({"status": "ok"}, self.client.get("/api/health").json())
        rules = self.client.get("/api/rules").json()["rules"]
        versions = {r["version"] for r in rules}
        self.assertIn("hourly-match@1", versions)
        self.assertIn("monthly-net@1", versions)

    def test_full_flow_over_http(self):
        # 导入用电与凭证
        resp = self.client.post("/api/ingest/consumption", json={
            "idempotency_key": "c-1", "region": "north", "timezone": "UTC",
            "records": [{"meter_id": "M1", "start": "2026-03-01T00:00:00+00:00",
                         "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100}],
        })
        self.assertEqual(200, resp.status_code)
        self.assertEqual("inserted", resp.json()["records"][0]["outcome"])
        # 重复导入同一批次 → 幂等返回
        again = self.client.post("/api/ingest/consumption", json={
            "idempotency_key": "c-1", "region": "north", "timezone": "UTC",
            "records": [{"meter_id": "M1", "start": "2026-03-01T00:00:00+00:00",
                         "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100}],
        })
        self.assertTrue(again.json()["deduplicated"])

        self.client.post("/api/ingest/certificates", json={
            "idempotency_key": "cert-1", "region": "north", "timezone": "UTC",
            "records": [{"cert_no": "C1", "start": "2026-03-01T00:00:00+00:00",
                         "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100}],
        })

        # 创建报告（选择口径）→ 201 + 初次核算
        report = self.client.post("/api/reports", json={
            "idempotency_key": "r-1", "org_id": "org-a", "region": "north",
            "period_start": "2026-03-01T00:00:00+00:00",
            "period_end": "2026-03-01T01:00:00+00:00",
            "rule_version": "hourly-match@1",
        })
        self.assertEqual(201, report.status_code)
        report_id = report.json()["report"]["report_id"]
        self.assertEqual(100, report.json()["runs"][-1]["totals"]["certificate_matched_kwh"])

        # 封存 → 重复封存幂等
        sealed = self.client.post(f"/api/reports/{report_id}/seal")
        self.assertEqual(200, sealed.status_code)
        self.assertFalse(sealed.json()["already_sealed"])
        resealed = self.client.post(f"/api/reports/{report_id}/seal")
        self.assertTrue(resealed.json()["already_sealed"])

        # 重放 → 无新数据时差异为零
        replay = self.client.post(f"/api/reports/{report_id}/replay")
        self.assertEqual(200, replay.status_code)
        self.assertTrue(replay.json()["diff"]["digest_equal"])
        self.assertTrue(replay.json()["sealed_result_untouched"])

        # 核算详情含输入快照与摘要
        run_id = replay.json()["replay_run_id"]
        run = self.client.get(f"/api/runs/{run_id}").json()
        self.assertTrue(run["input_digest"].startswith("sha256:"))
        self.assertEqual("replay", run["kind"])
        self.assertTrue(any(i["source_type"] == "certificate" for i in run["inputs"]))

    def test_error_mapping(self):
        self.assertEqual(404, self.client.get("/api/reports/nope").status_code)
        self.assertEqual(404, self.client.post("/api/reports/nope/seal").status_code)
        bad = self.client.post("/api/reports", json={"org_id": "x"})
        self.assertEqual(422, bad.status_code)
        self.assertEqual("validation_error", bad.json()["error"]["code"])
        unknown_rule = self.client.post("/api/ingest/consumption", json={
            "idempotency_key": "c-9", "region": "north", "timezone": "UTC",
            "records": [{"meter_id": "M1", "start": "2026-03-01T00:00:00+00:00",
                         "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 1}],
        })
        self.assertEqual(200, unknown_rule.status_code)
        resp = self.client.post("/api/reports", json={
            "idempotency_key": "r-9", "org_id": "org-a", "region": "north",
            "period_start": "2026-03-01", "period_end": "2026-03-02",
            "rule_version": "no-such-rule@9",
        })
        self.assertEqual(422, resp.status_code)
        self.assertEqual("unknown_rule_version", resp.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()

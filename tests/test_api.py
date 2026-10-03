import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from fastapi.testclient import TestClient

from green_match.api import create_app


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(create_app(os.path.join(self.tmp.name, "api.db")))

    def import_consumption(self, meter="M1", kwh="100", start="2026-01-01T00:00:00+08:00",
                           end="2026-01-01T01:00:00+08:00", key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return self.client.post(
            "/imports/meter-intervals",
            headers=headers,
            json={
                "intervals": [
                    {
                        "meter_id": meter,
                        "kind": "consumption",
                        "region": "north",
                        "starts_at": start,
                        "ends_at": end,
                        "kwh": kwh,
                    }
                ]
            },
        )

    def import_lot(self, lot_id="L-1", kwh="500"):
        return self.client.post(
            "/imports/attribute-lots",
            json={
                "lots": [
                    {
                        "lot_id": lot_id,
                        "region": "north",
                        "starts_at": "2026-01-01T00:00:00+08:00",
                        "ends_at": "2026-01-01T01:00:00+08:00",
                        "available_kwh": kwh,
                        "source_digest": "sha256:registry-batch-1",
                    }
                ]
            },
        )

    def create_report(self, enterprise="ENT-1", caliber="hourly_same_region"):
        return self.client.post(
            "/reports",
            json={
                "enterprise_id": enterprise,
                "region": "north",
                "period_start": "2026-01-01T00:00:00+08:00",
                "period_end": "2026-01-02T00:00:00+08:00",
                "caliber": caliber,
            },
        )


class BasicApiTests(ApiTestBase):
    def test_health_and_calibers(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        calibers = self.client.get("/calibers").json()
        names = {c["name"] for c in calibers["calibers"]}
        self.assertIn("hourly_same_region", names)
        self.assertIn("period_pool_grid", names)
        self.assertEqual(calibers["default_rule_version"], "v1")

    def test_naive_datetime_rejected(self):
        response = self.client.post(
            "/imports/meter-intervals",
            json={
                "intervals": [
                    {
                        "meter_id": "M1",
                        "kind": "consumption",
                        "region": "north",
                        "starts_at": "2026-01-01T00:00:00",
                        "ends_at": "2026-01-01T01:00:00",
                        "kwh": "100",
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_unknown_report_is_404(self):
        response = self.client.get("/reports/rep_missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "report_not_found")

    def test_unknown_caliber_is_422(self):
        response = self.create_report(caliber="bogus")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unknown_caliber")


class EndToEndApiTests(ApiTestBase):
    def test_report_lifecycle_over_http(self):
        # 导入（带幂等键），重复请求命中重放
        first = self.import_consumption(key="import-1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["inserted"], 1)
        replay = self.import_consumption(key="import-1")
        self.assertEqual(replay.headers.get("X-Idempotent-Replay"), "true")
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.import_lot().json()["inserted"], 1)

        # 创建报告并核算：+08:00 的 1 月 1 日对应 UTC 跨天，小时桶按 UTC 对齐
        created = self.create_report()
        self.assertEqual(created.status_code, 201)
        report_id = created.json()["report_id"]
        self.assertEqual(created.json()["status"], "draft")

        run = self.client.post(f"/reports/{report_id}/runs")
        self.assertEqual(run.status_code, 201)
        run_body = run.json()
        self.assertEqual(run_body["result"]["totals"]["certificate_matched_kwh"], "100")
        self.assertEqual(run_body["rule_version"], "v1")
        run_id = run_body["run_id"]

        # 封存后结果冻结；重复封存幂等
        sealed = self.client.post(f"/reports/{report_id}/seal", json={})
        self.assertEqual(sealed.json()["status"], "sealed")
        again = self.client.post(f"/reports/{report_id}/seal", json={})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["sealed_run_id"], run_id)

        # 新数据不改写封存结果，重放能对照出差异
        self.import_consumption(meter="M2", kwh="40")
        frozen = self.client.get(f"/runs/{run_id}").json()
        self.assertEqual(frozen["result"]["totals"]["consumption_kwh"], "100")
        replayed = self.client.post(f"/runs/{run_id}/replay")
        self.assertEqual(replayed.status_code, 201)
        diff = replayed.json()
        self.assertTrue(diff["inputs_changed"])
        self.assertEqual(
            diff["totals_diff"]["consumption_kwh"],
            {"before": "100", "after": "140", "delta": "40"},
        )

    def test_unmatched_reason_visible_over_http(self):
        self.import_consumption()
        report_id = self.create_report().json()["report_id"]
        run = self.client.post(f"/reports/{report_id}/runs").json()
        self.assertEqual(
            run["result"]["unmatched_by_reason"],
            {"no_certificate_covering_period": "100"},
        )

    def test_revision_conflict_over_http(self):
        self.import_consumption()
        conflict = self.client.post(
            "/imports/meter-intervals",
            json={
                "intervals": [
                    {
                        "meter_id": "M1",
                        "kind": "consumption",
                        "region": "north",
                        "starts_at": "2026-01-01T00:00:00+08:00",
                        "ends_at": "2026-01-01T01:00:00+08:00",
                        "kwh": "999",
                    }
                ]
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "meter_revision_conflict")


if __name__ == "__main__":
    unittest.main()

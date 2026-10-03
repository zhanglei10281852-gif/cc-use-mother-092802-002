"""服务层全链路测试：幂等导入、计量修订、时区边界、跨时段拆分、
凭证争用、口径选择、封存不可改写、重放差异、重启恢复。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from green_match.service import GreenMatchService, ServiceError  # noqa: E402

HOURLY = "hourly-match@1"
MONTHLY = "monthly-net@1"


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        self.svc = GreenMatchService(self.db_path)

    def tearDown(self):
        self.svc.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------ 辅助
    def ingest_consumption(self, records, region="north", tz="UTC", key="c-1", org=None):
        for r in records:
            if org:
                r.setdefault("org_id", org)
        return self.svc.ingest("consumption", {
            "idempotency_key": key, "region": region, "timezone": tz, "records": records,
        })

    def ingest_certificates(self, records, region="north", tz="UTC", key="cert-1"):
        return self.svc.ingest("certificate", {
            "idempotency_key": key, "region": region, "timezone": tz, "records": records,
        })

    def create_report(self, org="org-a", region="north", start="2026-03-01T00:00+00:00",
                      end="2026-03-02T00:00+00:00", rule=HOURLY, key="r-1"):
        return self.svc.create_report({
            "idempotency_key": key, "org_id": org, "region": region,
            "period_start": start, "period_end": end, "rule_version": rule,
        })

    def latest_run(self, report):
        return self.svc.get_run(report["runs"][-1]["run_id"])

    @staticmethod
    def hour_record(meter, day, hour, kwh, **extra):
        rec = {
            "meter_id": meter,
            "start": f"2026-03-{day:02d}T{hour:02d}:00:00+00:00",
            "end": f"2026-03-{day:02d}T{hour + 1:02d}:00:00+00:00",
            "quantity_kwh": kwh,
        }
        rec.update(extra)
        return rec


class IngestTests(ServiceTestCase):
    def test_duplicate_batch_is_idempotent(self):
        payload = [self.hour_record("M1", 1, 0, 100)]
        first = self.ingest_consumption(payload)
        second = self.ingest_consumption(payload)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        # 重复导入不会让电量翻倍
        report = self.create_report()
        totals = report["runs"][-1]["totals"]
        self.assertEqual(100, totals["consumption_kwh"])

    def test_idempotency_key_conflict_rejected(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)])
        with self.assertRaises(ServiceError) as ctx:
            self.ingest_consumption([self.hour_record("M1", 1, 0, 200)])
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual("idempotency_conflict", ctx.exception.code)

    def test_reimport_same_records_under_new_key_dedupes_per_record(self):
        rec = self.hour_record("M1", 1, 0, 100)
        self.ingest_consumption([rec], key="c-1")
        again = self.ingest_consumption([rec], key="c-2")
        self.assertEqual("duplicate", again["records"][0]["outcome"])
        report = self.create_report()
        self.assertEqual(100, report["runs"][-1]["totals"]["consumption_kwh"])

    def test_meter_revision_supersedes(self):
        rec = self.hour_record("M1", 1, 0, 100)
        self.ingest_consumption([rec], key="c-1")
        revised = self.ingest_consumption(
            [self.hour_record("M1", 1, 0, 60, revision=2)], key="c-2")
        self.assertEqual("superseded_previous", revised["records"][0]["outcome"])
        report = self.create_report()
        self.assertEqual(60, report["runs"][-1]["totals"]["consumption_kwh"])

    def test_same_revision_different_value_conflicts(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)], key="c-1")
        with self.assertRaises(ServiceError) as ctx:
            self.ingest_consumption([self.hour_record("M1", 1, 0, 80)], key="c-2")
        self.assertEqual("revision_conflict", ctx.exception.code)

    def test_stale_revision_rejected(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100, revision=2)], key="c-1")
        with self.assertRaises(ServiceError) as ctx:
            self.ingest_consumption([self.hour_record("M1", 1, 0, 90, revision=1)], key="c-2")
        self.assertEqual("stale_revision", ctx.exception.code)

    def test_new_region_requires_timezone(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.ingest("consumption", {
                "idempotency_key": "x", "region": "nowhere",
                "records": [self.hour_record("M1", 1, 0, 1)],
            })
        self.assertEqual(422, ctx.exception.status)


class TimezoneAndSplitTests(ServiceTestCase):
    def test_naive_local_time_interpreted_in_region_timezone(self):
        # 上海 3 月 1 日 00:00-04:00（naive）= UTC 2 月 28 日 16:00-20:00
        self.ingest_consumption(
            [{"meter_id": "M1", "start": "2026-03-01T00:00", "end": "2026-03-01T04:00",
              "quantity_kwh": 40}],
            region="cn", tz="Asia/Shanghai",
        )
        report = self.create_report(region="cn", start="2026-03-01", end="2026-03-02")
        run = self.latest_run(report)
        self.assertEqual(40, run["result"]["totals"]["consumption_kwh"])
        # 报告周期按区域时区换算：本地 3 月 1 日 = UTC 2 月 28 日 16:00 起
        self.assertEqual("2026-02-28T16:00:00+00:00",
                         run["result"]["period"]["start_utc"])
        self.assertEqual(24, run["result"]["period"]["bucket_count"])

    def test_dst_spring_forward_day_has_23_buckets(self):
        self.ingest_consumption(
            [{"meter_id": "M1", "start": "2026-03-08T00:00", "end": "2026-03-09T00:00",
              "quantity_kwh": 23}],
            region="us-east", tz="America/New_York",
        )
        report = self.create_report(region="us-east", start="2026-03-08", end="2026-03-09")
        run = self.latest_run(report)
        # 夏令时开始日只有 23 个小时桶，23 度电均匀落桶不丢不重
        self.assertEqual(23, run["result"]["period"]["bucket_count"])
        self.assertAlmostEqual(23, run["result"]["totals"]["consumption_kwh"], places=6)

    def test_record_spanning_period_boundary_is_split(self):
        # 10:00-12:00 的 100 度电，被两个相邻报告周期各分走一半
        self.ingest_consumption([{
            "meter_id": "M1",
            "start": "2026-03-01T10:00:00+00:00",
            "end": "2026-03-01T12:00:00+00:00",
            "quantity_kwh": 100,
        }])
        first = self.create_report(start="2026-03-01T00:00+00:00",
                                   end="2026-03-01T11:00+00:00", key="r-a")
        second = self.create_report(start="2026-03-01T11:00+00:00",
                                    end="2026-03-01T13:00+00:00", key="r-b")
        self.assertEqual(50, first["runs"][-1]["totals"]["consumption_kwh"])
        self.assertEqual(50, second["runs"][-1]["totals"]["consumption_kwh"])


class MatchingTests(ServiceTestCase):
    def _cert(self, cert_no, day, hour_from, hour_to, kwh, **extra):
        rec = {
            "cert_no": cert_no,
            "start": f"2026-03-{day:02d}T{hour_from:02d}:00:00+00:00",
            "end": f"2026-03-{day:02d}T{hour_to:02d}:00:00+00:00",
            "quantity_kwh": kwh,
        }
        rec.update(extra)
        return rec

    def test_hourly_matching_and_unmatched_reason_no_supply(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)])
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        run = self.latest_run(report)
        unmatched = run["result"]["unmatched"]
        self.assertEqual(100, unmatched[0]["quantity_kwh"])
        self.assertEqual("no_supply_in_region", unmatched[0]["reason"])

    def test_unmatched_reason_not_covering_interval(self):
        # 凭证落在报告周期内（5 点），但不覆盖 0 点的用电桶
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)])
        self.ingest_certificates([self._cert("C1", 1, 5, 6, 100)])
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T06:00+00:00")
        run = self.latest_run(report)
        unmatched = run["result"]["unmatched"]
        self.assertEqual(1, len(unmatched))
        self.assertEqual("certificate_not_covering_interval", unmatched[0]["reason"])

    def test_unmatched_reason_certificate_exhausted(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)])
        self.ingest_certificates([self._cert("C1", 1, 0, 1, 30)])
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        run = self.latest_run(report)
        self.assertEqual(30, run["result"]["totals"]["certificate_matched_kwh"])
        self.assertEqual(70, run["result"]["unmatched"][0]["quantity_kwh"])
        self.assertEqual("certificate_exhausted", run["result"]["unmatched"][0]["reason"])

    def test_out_of_region_certificate_noted_but_not_matched(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)], region="north")
        self.ingest_certificates([self._cert("C1", 1, 0, 1, 100)], region="south")
        report = self.create_report(region="north", start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        run = self.latest_run(report)
        self.assertEqual(0, run["result"]["totals"]["certificate_matched_kwh"])
        self.assertEqual(100, run["result"]["notes"]["out_of_region_certificate_kwh"])

    def test_caliber_choice_changes_result(self):
        # 用电在 0 点，凭证只覆盖 5 点：逐小时口径不匹配，周期净额口径可抵
        self.ingest_consumption([self.hour_record("M1", 1, 0, 10)])
        self.ingest_certificates([self._cert("C1", 1, 5, 6, 10)])
        hourly = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T06:00+00:00", rule=HOURLY, key="r-h")
        monthly = self.create_report(start="2026-03-01T00:00+00:00",
                                     end="2026-03-01T06:00+00:00", rule=MONTHLY, key="r-m")
        self.assertEqual(10, hourly["runs"][-1]["totals"]["unmatched_kwh"])
        self.assertEqual(0, monthly["runs"][-1]["totals"]["unmatched_kwh"])
        # 口径随报告固化
        self.assertEqual(HOURLY, hourly["report"]["rule_version"])
        self.assertEqual(MONTHLY, monthly["report"]["rule_version"])

    def test_self_generation_matches_before_certificates(self):
        self.svc.ingest("generation", {
            "idempotency_key": "g-1", "region": "north", "timezone": "UTC",
            "records": [self.hour_record("PV-1", 1, 0, 40)],
        })
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)])
        self.ingest_certificates([self._cert("C1", 1, 0, 1, 100)])
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        totals = report["runs"][-1]["totals"]
        self.assertEqual(40, totals["self_generation_matched_kwh"])
        self.assertEqual(60, totals["certificate_matched_kwh"])
        self.assertEqual(0, totals["unmatched_kwh"])


class SealAndContentionTests(ServiceTestCase):
    def _setup_two_orgs_one_cert(self):
        self.ingest_consumption([self.hour_record("M-A", 1, 0, 80)], key="c-a", org="org-a")
        self.ingest_consumption([self.hour_record("M-B", 1, 0, 80)], key="c-b", org="org-b")
        self.ingest_certificates([{
            "cert_no": "C1", "start": "2026-03-01T00:00:00+00:00",
            "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100,
        }])

    def test_certificate_contention_resolved_by_seal_order(self):
        self._setup_two_orgs_one_cert()
        report_a = self.create_report(org="org-a", key="r-a",
                                      start="2026-03-01T00:00+00:00",
                                      end="2026-03-01T01:00+00:00")
        report_b = self.create_report(org="org-b", key="r-b",
                                      start="2026-03-01T00:00+00:00",
                                      end="2026-03-01T01:00+00:00")
        # 草稿阶段双方都能预览到足额凭证
        self.assertEqual(80, report_a["runs"][-1]["totals"]["certificate_matched_kwh"])
        self.assertEqual(80, report_b["runs"][-1]["totals"]["certificate_matched_kwh"])

        sealed_a = self.svc.seal_report(report_a["report"]["report_id"])
        self.assertEqual(80, sealed_a["runs"][-1]["totals"]["certificate_matched_kwh"])

        # B 封存时只剩 20 度额度，其余 60 度必须说明原因
        sealed_b = self.svc.seal_report(report_b["report"]["report_id"])
        totals_b = sealed_b["runs"][-1]["totals"]
        self.assertEqual(20, totals_b["certificate_matched_kwh"])
        self.assertEqual(60, totals_b["unmatched_kwh"])
        run_b = self.svc.get_run(sealed_b["report"]["sealed_run_id"])
        self.assertEqual("certificate_locked_by_other_report",
                         run_b["result"]["unmatched"][0]["reason"])

        # A 的封存结果不受 B 影响：重放总量不变，差异仅体现为出现了他方封存锁
        replay_a = self.svc.replay_report(report_a["report"]["report_id"])
        self.assertEqual(0, replay_a["diff"]["totals_delta"]["certificate_matched_kwh"])
        self.assertEqual(0, replay_a["diff"]["totals_delta"]["unmatched_kwh"])
        lock_inputs = [i for i in replay_a["diff"]["input_changes"]["added"]
                       if i["source_type"] == "external_lock"]
        self.assertEqual(1, len(lock_inputs))

    def test_seal_is_idempotent(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 10)])
        report = self.create_report()
        report_id = report["report"]["report_id"]
        first = self.svc.seal_report(report_id)
        run_count = len(first["runs"])
        second = self.svc.seal_report(report_id)
        self.assertTrue(second["already_sealed"])
        self.assertEqual(run_count, len(second["runs"]))
        self.assertEqual(first["report"]["sealed_run_id"], second["report"]["sealed_run_id"])

    def test_report_creation_idempotent(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 10)])
        first = self.create_report()
        second = self.create_report()
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["report"]["report_id"], second["report"]["report_id"])
        self.assertEqual(1, len(second["runs"]))
        with self.assertRaises(ServiceError) as ctx:
            self.create_report(end="2026-03-03T00:00:00+00:00")
        self.assertEqual("idempotency_conflict", ctx.exception.code)


class RevisionAndReplayTests(ServiceTestCase):
    def test_sealed_result_survives_meter_revision_and_replay_explains(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)], key="c-1")
        self.ingest_certificates([{
            "cert_no": "C1", "start": "2026-03-01T00:00:00+00:00",
            "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100,
        }])
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        report_id = report["report"]["report_id"]
        sealed = self.svc.seal_report(report_id)
        sealed_run_id = sealed["report"]["sealed_run_id"]

        # 事后更正：补录电表把 0 点电量从 100 改为 60
        self.ingest_consumption([self.hour_record("M1", 1, 0, 60, revision=2)], key="c-2")

        # 封存结果原样保留，审计依据不丢
        sealed_run = self.svc.get_run(sealed_run_id)
        self.assertEqual(100, sealed_run["result"]["totals"]["certificate_matched_kwh"])
        report_after = self.svc.get_report(report_id)
        self.assertEqual("sealed", report_after["report"]["status"])
        self.assertEqual(sealed_run_id, report_after["report"]["sealed_run_id"])

        # 重放解释差异：摘要变化、总量变化、输入变化都指向那条修订
        replay = self.svc.replay_report(report_id)
        self.assertTrue(replay["sealed_result_untouched"])
        diff = replay["diff"]
        self.assertFalse(diff["digest_equal"])
        self.assertEqual(-40, diff["totals_delta"]["consumption_kwh"])
        self.assertEqual(-40, diff["totals_delta"]["certificate_matched_kwh"])
        removed_keys = [i["natural_key"] for i in diff["input_changes"]["removed"]]
        added_keys = [i["natural_key"] for i in diff["input_changes"]["added"]]
        self.assertTrue(any("M1" in k for k in removed_keys))
        self.assertTrue(any("M1" in k for k in added_keys))
        self.assertIn("修订", replay["diff"]["explanation"])

    def test_certificate_revocation_after_seal_visible_in_replay(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)], key="c-1")
        self.ingest_certificates([{
            "cert_no": "C1", "start": "2026-03-01T00:00:00+00:00",
            "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100,
        }], key="cert-1")
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        report_id = report["report"]["report_id"]
        self.svc.seal_report(report_id)

        revoked = self.ingest_certificates([{
            "cert_no": "C1", "start": "2026-03-01T00:00:00+00:00",
            "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100,
            "revision": 2, "revoke": True,
        }], key="cert-2")
        self.assertEqual("revoked", revoked["records"][0]["outcome"])

        # 封存不变，重放才看到凭证失效
        sealed_run_id = self.svc.get_report(report_id)["report"]["sealed_run_id"]
        sealed_run = self.svc.get_run(sealed_run_id)
        self.assertEqual(100, sealed_run["result"]["totals"]["certificate_matched_kwh"])
        replay = self.svc.replay_report(report_id)
        self.assertEqual(100, replay["replay_totals"]["unmatched_kwh"])
        self.assertFalse(replay["diff"]["digest_equal"])

    def test_replay_without_changes_is_stable(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 10)])
        report = self.create_report()
        report_id = report["report"]["report_id"]
        self.svc.seal_report(report_id)
        replay = self.svc.replay_report(report_id)
        self.assertTrue(replay["diff"]["digest_equal"])
        self.assertTrue(all(v == 0 for v in replay["diff"]["totals_delta"].values()))
        self.assertEqual("输入摘要与核算结果均未变化", replay["diff"]["explanation"])


class RecoveryTests(ServiceTestCase):
    def test_restart_recovers_state_from_sqlite(self):
        self.ingest_consumption([self.hour_record("M1", 1, 0, 100)], key="c-1")
        self.ingest_certificates([{
            "cert_no": "C1", "start": "2026-03-01T00:00:00+00:00",
            "end": "2026-03-01T01:00:00+00:00", "quantity_kwh": 100,
        }], key="cert-1")
        report = self.create_report(start="2026-03-01T00:00+00:00",
                                    end="2026-03-01T01:00+00:00")
        report_id = report["report"]["report_id"]
        self.svc.seal_report(report_id)
        self.svc.close()

        # 模拟进程重启：同一数据库文件上新建服务实例
        reopened = GreenMatchService(self.db_path)
        try:
            restored = reopened.get_report(report_id)
            self.assertEqual("sealed", restored["report"]["status"])
            self.assertEqual(100, restored["runs"][-1]["totals"]["certificate_matched_kwh"])
            # 重启后重放仍然确定：无新数据则结果一致
            replay = reopened.replay_report(report_id)
            self.assertTrue(replay["diff"]["digest_equal"])
            # 重复导入在重启后依然幂等
            again = reopened.ingest("consumption", {
                "idempotency_key": "c-1", "region": "north", "timezone": "UTC",
                "records": [self.hour_record("M1", 1, 0, 100)],
            })
            self.assertTrue(again["deduplicated"])
            self.assertEqual(2, len(reopened.list_rules()))
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from green_match.engine import REASON_CERT_CLAIMED_BY_SEALED
from green_match.errors import DomainError
from green_match.service import GreenMatchService

UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def consumption(meter, region, start_h, end_h, kwh, revision=1):
    return {
        "meter_id": meter,
        "kind": "consumption",
        "region": region,
        "starts_at": BASE + timedelta(hours=start_h),
        "ends_at": BASE + timedelta(hours=end_h),
        "kwh": kwh,
        "revision": revision,
    }


def lot(lot_id, region, start_h, end_h, kwh):
    return {
        "lot_id": lot_id,
        "region": region,
        "starts_at": BASE + timedelta(hours=start_h),
        "ends_at": BASE + timedelta(hours=end_h),
        "available_kwh": kwh,
        "source_digest": f"sha256:{lot_id}",
    }


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "green_match.db")
        self.service = GreenMatchService(self.db_path)

    def make_report(self, enterprise="ENT-1", region="north", caliber="hourly_same_region"):
        report, _ = self.service.create_report(
            enterprise, region, BASE, BASE + timedelta(hours=24), caliber
        )
        return report

    def run_once(self, report_id):
        run, _ = self.service.run_accounting(report_id)
        return run


class ImportTests(ServiceTestBase):
    def test_duplicate_import_is_deduplicated(self):
        items = [consumption("M1", "north", 0, 1, "100")]
        first, _ = self.service.import_meter_intervals(items)
        second, _ = self.service.import_meter_intervals(items)
        self.assertEqual(first, {"inserted": 1, "duplicates": 0})
        self.assertEqual(second, {"inserted": 0, "duplicates": 1})

    def test_same_revision_different_value_conflicts(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        with self.assertRaises(DomainError) as ctx:
            self.service.import_meter_intervals(
                [consumption("M1", "north", 0, 1, "120")]
            )
        self.assertEqual(ctx.exception.code, "meter_revision_conflict")
        # 提升 revision 的更正被接受
        outcome, _ = self.service.import_meter_intervals(
            [consumption("M1", "north", 0, 1, "120", revision=2)]
        )
        self.assertEqual(outcome["inserted"], 1)

    def test_idempotency_key_replays_stored_response(self):
        items = [consumption("M1", "north", 0, 1, "100")]
        first, replayed1 = self.service.import_meter_intervals(items, "key-1")
        second, replayed2 = self.service.import_meter_intervals(items, "key-1")
        self.assertFalse(replayed1)
        self.assertTrue(replayed2)
        self.assertEqual(first, second)
        # 重放没有写入重复数据：不带键再导入同一批，全部判为重复
        third, _ = self.service.import_meter_intervals(items)
        self.assertEqual(third["duplicates"], 1)

    def test_idempotency_key_with_different_body_conflicts(self):
        self.service.import_meter_intervals(
            [consumption("M1", "north", 0, 1, "100")], "key-1"
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.import_meter_intervals(
                [consumption("M1", "north", 0, 1, "200")], "key-1"
            )
        self.assertEqual(ctx.exception.code, "idempotency_key_reused")

    def test_lot_is_immutable(self):
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "100")])
        outcome, _ = self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "100")])
        self.assertEqual(outcome["duplicates"], 1)
        with self.assertRaises(DomainError) as ctx:
            self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "200")])
        self.assertEqual(ctx.exception.code, "lot_conflict")


class AccountingFlowTests(ServiceTestBase):
    def test_full_flow_and_sealed_result_is_immutable(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "500")])
        report = self.make_report()
        run = self.run_once(report["report_id"])
        self.assertEqual(run["result"]["totals"]["certificate_matched_kwh"], "100")
        self.assertEqual(run["rule_version"], "v1")
        self.assertTrue(run["input_digest"].startswith("sha256:"))

        sealed = self.service.seal_report(report["report_id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["sealed_run_id"], run["run_id"])

        # 补录新电表数据后，已封存结果保持不变
        self.service.import_meter_intervals([consumption("M2", "north", 0, 1, "80")])
        frozen = self.service.get_run(run["run_id"])
        self.assertEqual(frozen["result"]["totals"]["consumption_kwh"], "100")
        # 封存报告不允许执行新核算
        with self.assertRaises(DomainError) as ctx:
            self.service.run_accounting(report["report_id"])
        self.assertEqual(ctx.exception.code, "report_not_draft")

    def test_replay_explains_change_after_meter_revision(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "500")])
        report = self.make_report()
        run = self.run_once(report["report_id"])

        # 事后更正：同一区间 revision 2 把电量改为 130
        self.service.import_meter_intervals(
            [consumption("M1", "north", 0, 1, "130", revision=2)]
        )
        replay, _ = self.service.replay_run(run["run_id"])
        self.assertTrue(replay["inputs_changed"])
        self.assertEqual(
            replay["totals_diff"]["consumption_kwh"],
            {"before": "100", "after": "130", "delta": "30"},
        )
        self.assertEqual(
            replay["totals_diff"]["certificate_matched_kwh"],
            {"before": "100", "after": "130", "delta": "30"},
        )
        # 原运行结果未被改写
        original = self.service.get_run(run["run_id"])
        self.assertEqual(original["result"]["totals"]["consumption_kwh"], "100")

    def test_replay_without_changes_is_stable(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "500")])
        report = self.make_report()
        run = self.run_once(report["report_id"])
        replay, _ = self.service.replay_run(run["run_id"])
        self.assertFalse(replay["inputs_changed"])
        self.assertEqual(replay["totals_diff"], {})
        self.assertEqual(replay["allocation_changes"], [])

    def test_unmatched_reasons_present_in_result(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        report = self.make_report()
        run = self.run_once(report["report_id"])
        self.assertEqual(
            run["result"]["unmatched_by_reason"],
            {"no_certificate_covering_period": "100"},
        )
        self.assertEqual(len(run["result"]["unmatched_buckets"]), 1)

    def test_unknown_caliber_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.create_report(
                "ENT-1", "north", BASE, BASE + timedelta(hours=24), "no_such_caliber"
            )
        self.assertEqual(ctx.exception.code, "unknown_caliber")


class ContentionTests(ServiceTestBase):
    """同一凭证批次被多份报告争用：先封存者占用，后到者看到剩余量。"""

    def setUp(self):
        super().setUp()
        # 凭证 100 kWh 覆盖第 0 小时；区域用电 140 kWh 也在第 0 小时
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "100")])
        self.service.import_meter_intervals(
            [
                consumption("MA", "north", 0, 1, "60"),
                consumption("MB", "north", 0, 1, "40"),
                consumption("MD", "north", 0, 1, "40"),
            ]
        )

    def make_enterprise_report(self, enterprise):
        report, _ = self.service.create_report(
            enterprise, "north", BASE, BASE + timedelta(hours=24), "hourly_same_region"
        )
        return report

    def test_certificate_contention_first_seal_wins(self):
        # 企业 A 的核算把 100 全部匹配并封存
        report_a = self.make_enterprise_report("ENT-A")
        run_a = self.run_once(report_a["report_id"])
        self.assertEqual(run_a["result"]["totals"]["certificate_matched_kwh"], "100")
        self.service.seal_report(report_a["report_id"])

        # 企业 B 的草稿核算：凭证已被 A 封存耗尽，只能看到未匹配与原因
        report_b = self.make_enterprise_report("ENT-B")
        run_b = self.run_once(report_b["report_id"])
        self.assertEqual(run_b["result"]["totals"]["certificate_matched_kwh"], "0")
        self.assertEqual(
            run_b["result"]["unmatched_by_reason"],
            {REASON_CERT_CLAIMED_BY_SEALED: "140"},
        )
        self.assertEqual(run_b["result"]["contested_lots"][0]["lot_id"], "L-1")

        # 凭证用量接口能解释占用去向
        usage = self.service.get_lot_usage("L-1")
        self.assertEqual(usage["remaining_kwh"], "0")
        self.assertEqual(
            usage["sealed_consumption"],
            [{"report_id": report_a["report_id"], "kwh": "100"}],
        )

    def test_seal_rejects_overdrawn_certificate(self):
        # 两个草稿同时核算（各自看到余量 100），D 先封存，B 的封存必须被拒绝
        report_b = self.make_enterprise_report("ENT-B")
        report_d = self.make_enterprise_report("ENT-D")
        self.run_once(report_b["report_id"])
        self.run_once(report_d["report_id"])
        self.service.seal_report(report_d["report_id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.seal_report(report_b["report_id"])
        self.assertEqual(ctx.exception.code, "certificate_overdrawn")
        self.assertEqual(ctx.exception.details["overdrawn"][0]["lot_id"], "L-1")
        # B 重新核算后结果反映最新占用，可正常封存
        run_b2 = self.run_once(report_b["report_id"])
        self.assertEqual(run_b2["result"]["totals"]["certificate_matched_kwh"], "0")
        sealed = self.service.seal_report(report_b["report_id"])
        self.assertEqual(sealed["status"], "sealed")


class CorrectionTests(ServiceTestBase):
    def test_superseding_report_replaces_sealed_one_and_keeps_audit(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "500")])
        report_v1 = self.make_report()
        run_v1 = self.run_once(report_v1["report_id"])
        self.service.seal_report(report_v1["report_id"])

        # 事后更正数据，创建同口径更正报告
        self.service.import_meter_intervals(
            [consumption("M1", "north", 0, 1, "130", revision=2)]
        )
        report_v2, _ = self.service.create_report(
            "ENT-1", "north", BASE, BASE + timedelta(hours=24), "hourly_same_region"
        )
        self.assertEqual(report_v2["revision_no"], 2)
        run_v2 = self.run_once(report_v2["report_id"])
        # 更正报告核算时不受旧封存占用影响（封存后旧报告被取代）
        self.assertEqual(run_v2["result"]["totals"]["certificate_matched_kwh"], "130")
        self.service.seal_report(report_v2["report_id"])

        old = self.service.get_report(report_v1["report_id"])
        self.assertEqual(old["status"], "superseded")
        # 旧报告的结果与核算历史仍然完整可查
        old_run = self.service.get_run(run_v1["run_id"])
        self.assertEqual(old_run["result"]["totals"]["consumption_kwh"], "100")
        self.assertEqual(len(old["runs"]), 1)

    def test_draft_blocks_new_report_same_scope(self):
        report = self.make_report()
        with self.assertRaises(DomainError) as ctx:
            self.make_report()
        self.assertEqual(ctx.exception.code, "draft_exists")
        # 作废后可以重新创建
        self.service.void_report(report["report_id"])
        again = self.make_report()
        self.assertEqual(again["revision_no"], 2)


class RestartRecoveryTests(ServiceTestBase):
    def test_state_survives_restart(self):
        self.service.import_meter_intervals([consumption("M1", "north", 0, 1, "100")])
        self.service.import_attribute_lots([lot("L-1", "north", 0, 1, "500")])
        report = self.make_report()
        run = self.run_once(report["report_id"])
        self.service.seal_report(report["report_id"])
        self.service.import_meter_intervals([consumption("M2", "north", 0, 1, "80")])

        # 模拟进程重启：同一数据库文件重新打开
        reopened = GreenMatchService(self.db_path)
        restored = reopened.get_report(report["report_id"])
        self.assertEqual(restored["status"], "sealed")
        self.assertEqual(restored["sealed_run_id"], run["run_id"])
        frozen = reopened.get_run(run["run_id"])
        self.assertEqual(frozen["result"]["totals"]["consumption_kwh"], "100")
        # 重放在重启后仍可用，并能识别新导入的数据
        replay, _ = reopened.replay_run(run["run_id"])
        self.assertTrue(replay["inputs_changed"])
        self.assertEqual(
            replay["totals_diff"]["consumption_kwh"],
            {"before": "100", "after": "180", "delta": "80"},
        )
        # 幂等键同样持久化
        _, replayed = reopened.import_meter_intervals(
            [consumption("M3", "north", 0, 1, "5")], "boot-key"
        )
        self.assertFalse(replayed)
        _, replayed = GreenMatchService(self.db_path).import_meter_intervals(
            [consumption("M3", "north", 0, 1, "5")], "boot-key"
        )
        self.assertTrue(replayed)


if __name__ == "__main__":
    unittest.main()

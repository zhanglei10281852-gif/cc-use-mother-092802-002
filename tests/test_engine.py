import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from green_match.engine import (
    EngineInput,
    EffectiveInterval,
    LotSupply,
    REASON_CERT_CLAIMED_BY_SEALED,
    REASON_CERT_EXHAUSTED,
    REASON_NO_CERTIFICATE,
    REASON_REGION_MISMATCH,
    run_engine,
)
from green_match.rules import CALIBERS

UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def interval(meter, kind, region, start_h, end_h, kwh, revision=1):
    return EffectiveInterval(
        meter, kind, region,
        BASE + timedelta(hours=start_h), BASE + timedelta(hours=end_h),
        Decimal(str(kwh)), revision,
    )


def lot(lot_id, region, start_h, end_h, kwh):
    return LotSupply(
        lot_id, region,
        BASE + timedelta(hours=start_h), BASE + timedelta(hours=end_h),
        Decimal(str(kwh)), "sha256:test",
    )


def make_input(**overrides):
    defaults = dict(
        rule_version="v1",
        caliber=CALIBERS["hourly_same_region"],
        region="north",
        period_start=BASE,
        period_end=BASE + timedelta(hours=24),
        consumption=(),
        generation=(),
        lots=(),
        sealed_cert_use={},
        sealed_use_reports={},
    )
    defaults.update(overrides)
    return EngineInput(**defaults)


def total(result, key):
    return Decimal(result["totals"][key])


class EngineMatchTests(unittest.TestCase):
    def test_certificate_matches_demand_in_same_bucket(self):
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0, 1, 100),),
            lots=(lot("L-1", "north", 0, 1, 100),),
        ))
        self.assertEqual(total(result, "certificate_matched_kwh"), Decimal("100"))
        self.assertEqual(total(result, "unmatched_kwh"), Decimal("0"))
        self.assertEqual(result["allocations"][0]["lot_id"], "L-1")

    def test_generation_has_priority_over_certificates(self):
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0, 1, 100),),
            generation=(interval("G1", "generation", "north", 0, 1, 40),),
            lots=(lot("L-1", "north", 0, 1, 100),),
        ))
        self.assertEqual(total(result, "generation_matched_kwh"), Decimal("40"))
        self.assertEqual(total(result, "certificate_matched_kwh"), Decimal("60"))

    def test_unmatched_when_no_certificate(self):
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0, 1, 50),),
        ))
        self.assertEqual(total(result, "unmatched_kwh"), Decimal("50"))
        self.assertEqual(
            result["unmatched_by_reason"], {REASON_NO_CERTIFICATE: "50"}
        )

    def test_certificate_exhausted_reason(self):
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0, 1, 50),),
            lots=(lot("L-1", "north", 0, 1, 30),),
        ))
        self.assertEqual(total(result, "unmatched_kwh"), Decimal("20"))
        self.assertEqual(
            result["unmatched_by_reason"], {REASON_CERT_EXHAUSTED: "20"}
        )

    def test_region_mismatch_under_strict_caliber(self):
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0, 1, 50),),
            lots=(lot("L-1", "south", 0, 1, 50),),
        ))
        self.assertEqual(
            result["unmatched_by_reason"], {REASON_REGION_MISMATCH: "50"}
        )

    def test_grid_caliber_accepts_cross_region_lot(self):
        result = run_engine(make_input(
            caliber=CALIBERS["period_pool_grid"],
            consumption=(interval("M1", "consumption", "north", 0, 1, 50),),
            lots=(lot("L-1", "south", 0, 1, 50),),
        ))
        self.assertEqual(total(result, "certificate_matched_kwh"), Decimal("50"))

    def test_bucket_window_does_not_leak_across_hours(self):
        # 凭证只覆盖第 0 小时，不能匹配第 1 小时的用电
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 1, 2, 50),),
            lots=(lot("L-1", "north", 0, 1, 50),),
        ))
        self.assertEqual(total(result, "unmatched_kwh"), Decimal("50"))
        self.assertEqual(
            result["unmatched_by_reason"], {REASON_NO_CERTIFICATE: "50"}
        )

    def test_period_window_pools_across_hours(self):
        result = run_engine(make_input(
            caliber=CALIBERS["period_pool_same_region"],
            consumption=(interval("M1", "consumption", "north", 1, 2, 50),),
            lots=(lot("L-1", "north", 0, 1, 50),),
        ))
        self.assertEqual(total(result, "certificate_matched_kwh"), Decimal("50"))

    def test_sealed_consumption_reduces_availability(self):
        bucket0 = BASE
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0, 1, 50),),
            lots=(lot("L-1", "north", 0, 1, 100),),
            sealed_cert_use={("L-1", bucket0): Decimal("70")},
            sealed_use_reports={"L-1": ("rep_other",)},
        ))
        self.assertEqual(total(result, "certificate_matched_kwh"), Decimal("30"))
        self.assertEqual(
            result["unmatched_by_reason"],
            {REASON_CERT_CLAIMED_BY_SEALED: "20"},
        )
        self.assertEqual(
            result["contested_lots"],
            [{"lot_id": "L-1", "sealed_by_report_ids": ["rep_other"]}],
        )

    def test_cross_period_interval_split_proportionally(self):
        # 用电区间 00:30–01:30 共 100kWh，跨两个小时桶各 50
        result = run_engine(make_input(
            consumption=(interval("M1", "consumption", "north", 0.5, 1.5, 100),),
            lots=(lot("L-1", "north", 0, 1, 100),),
        ))
        self.assertEqual(total(result, "certificate_matched_kwh"), Decimal("50"))
        self.assertEqual(total(result, "unmatched_kwh"), Decimal("50"))

    def test_unknown_rule_version_rejected(self):
        with self.assertRaises(ValueError):
            run_engine(make_input(rule_version="v0"))


if __name__ == "__main__":
    unittest.main()

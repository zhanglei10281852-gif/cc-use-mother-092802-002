import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from green_match.timeutil import (
    format_instant,
    iter_buckets,
    parse_instant,
    split_interval,
)

UTC = timezone.utc


class ParseTests(unittest.TestCase):
    def test_offsets_and_z_denote_same_instant(self):
        a = parse_instant("2026-01-01T08:00:00+08:00")
        b = parse_instant("2026-01-01T00:00:00Z")
        self.assertEqual(a, b)
        self.assertEqual(a.utcoffset(), timedelta(0))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            parse_instant("2026-01-01T00:00:00")

    def test_format_is_canonical_and_roundtrips(self):
        moment = parse_instant("2026-06-01T08:00:00+08:00")
        text = format_instant(moment)
        self.assertEqual(text, "2026-06-01T00:00:00+00:00")
        self.assertEqual(parse_instant(text), moment)


class SplitTests(unittest.TestCase):
    def test_split_across_utc_midnight_with_local_timezone(self):
        # 北京时间 2026-01-01 06:30–08:30 == UTC 2025-12-31 22:30 – 2026-01-01 00:30
        start = parse_instant("2026-01-01T06:30:00+08:00")
        end = parse_instant("2026-01-01T08:30:00+08:00")
        pieces = split_interval(start, end, Decimal("120"), "hour")
        self.assertEqual(len(pieces), 3)
        self.assertEqual(pieces[0], (datetime(2025, 12, 31, 22, tzinfo=UTC), Decimal("30")))
        self.assertEqual(pieces[1], (datetime(2025, 12, 31, 23, tzinfo=UTC), Decimal("60")))
        self.assertEqual(pieces[2], (datetime(2026, 1, 1, 0, tzinfo=UTC), Decimal("30")))
        self.assertEqual(sum((p for _, p in pieces), Decimal(0)), Decimal("120"))

    def test_day_buckets_align_to_utc_not_local_midnight(self):
        # 北京时间自然日起点对应 UTC 前一日 16:00，日桶仍按 UTC 对齐
        start = parse_instant("2026-03-01T00:00:00+08:00")
        end = parse_instant("2026-03-02T00:00:00+08:00")
        buckets = list(iter_buckets(start, end, "day"))
        self.assertEqual(buckets[0], datetime(2026, 2, 28, tzinfo=UTC))
        self.assertEqual(buckets[-1], datetime(2026, 3, 1, tzinfo=UTC))
        pieces = split_interval(start, end, Decimal("48"), "day")
        self.assertEqual(pieces[0][1], Decimal("16"))  # 前 8 小时落在 2 月 28 日桶
        self.assertEqual(pieces[1][1], Decimal("32"))

    def test_invalid_interval_rejected(self):
        moment = parse_instant("2026-01-01T00:00:00Z")
        with self.assertRaises(ValueError):
            split_interval(moment, moment, Decimal("1"), "hour")


if __name__ == "__main__":
    unittest.main()

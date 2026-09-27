import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from green_match.contracts import AttributeLot, MeterInterval


class IntervalContractTests(unittest.TestCase):
    def test_interval_retains_timezone_and_revision(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        value = MeterInterval("M-9", "north", start, end, Decimal("4.25"), 2)
        self.assertEqual(value.starts_at.tzinfo, timezone.utc)
        self.assertEqual(value.revision, 2)

    def test_attribute_lot_has_independent_source_reference(self):
        value = AttributeLot("L-1", "north", datetime.now(timezone.utc), datetime.now(timezone.utc), Decimal("8"), "sha256:abc")
        self.assertEqual(value.source_digest, "sha256:abc")


if __name__ == "__main__":
    unittest.main()

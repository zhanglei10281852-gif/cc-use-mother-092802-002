"""计量区间与凭证批次的数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class MeterInterval:
    meter_id: str
    region: str
    starts_at: datetime
    ends_at: datetime
    kilowatt_hours: Decimal
    revision: int = 1


@dataclass(frozen=True)
class AttributeLot:
    lot_id: str
    region: str
    starts_at: datetime
    ends_at: datetime
    available_kwh: Decimal
    source_digest: str

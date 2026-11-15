"""The cycle -> statistics mapping, without Home Assistant running."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.peoples_gas.coordinator import (
    build_statistics,
    cycle_start,
    parse_balance,
    statistic_ids,
)
from custom_components.peoples_gas.vendor.energy_shopper_core.utilities.peoples_gas import (
    GasUsageData,
)

TZ = ZoneInfo("America/New_York")


def rec(to_date: str, mcf: float, bill: float | None = None, month=0, year=0) -> GasUsageData:
    return GasUsageData(
        date=to_date, usage_mcf=mcf, month=month, year=year, from_date="", to_date=to_date,
        meter_read_type="01", bill_amount=bill,
    )


def test_cycle_start_is_local_midnight_of_read_date() -> None:
    assert cycle_start(rec("5/1/2025", 1.0), TZ) == datetime(2025, 5, 1, tzinfo=TZ)
    assert cycle_start(rec("", 1.0, month=7, year=2024), TZ) == datetime(2024, 7, 1, tzinfo=TZ)
    assert cycle_start(rec("", 1.0), TZ) is None


def test_first_run_imports_everything_in_ccf() -> None:
    usage = [rec("2/1/2026", 8.0, 90.0), rec("1/1/2026", 9.0, 98.4), rec("3/1/2026", 6.0)]
    consumption, cost = build_statistics(usage, TZ, None, 0.0, None, 0.0)
    assert [(r["start"].month, r["state"], r["sum"]) for r in consumption] == [
        (1, 90.0, 90.0), (2, 80.0, 170.0), (3, 60.0, 230.0),
    ]
    # March has no bill yet: no cost row, so a later poll can still add it.
    assert [(r["start"].month, r["sum"]) for r in cost] == [(1, 98.4), (2, 188.4)]


def test_incremental_continues_sums_and_lets_late_bills_in() -> None:
    usage = [rec("1/1/2026", 9.0, 98.4), rec("2/1/2026", 8.0, 90.0), rec("3/1/2026", 6.0, 70.0)]
    feb = datetime(2026, 2, 1, tzinfo=TZ).timestamp()
    jan = datetime(2026, 1, 1, tzinfo=TZ).timestamp()
    consumption, cost = build_statistics(usage, TZ, feb, 170.0, jan, 98.4)
    assert [(r["start"].month, r["sum"]) for r in consumption] == [(3, 230.0)]
    assert [(r["start"].month, round(r["sum"], 2)) for r in cost] == [(2, 188.4), (3, 258.4)]


def test_duplicate_read_dates_keep_first() -> None:
    consumption, _ = build_statistics([rec("1/1/2026", 9.0), rec("1/1/2026", 4.0)], TZ, None, 0, None, 0)
    assert [r["state"] for r in consumption] == [90.0]


def test_parse_balance_and_ids() -> None:
    assert parse_balance({"d": {"ToTalBalance": "193.7600", "BillDue": "05/12/2026"}}) == (
        193.76,
        datetime(2026, 5, 12).date(),
    )
    assert parse_balance({"d": None}) == (None, None)
    assert statistic_ids("12-34 AB") == ("peoples_gas:12_34_ab_gas_consumption", "peoples_gas:12_34_ab_gas_cost")

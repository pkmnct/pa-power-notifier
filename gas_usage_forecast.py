#!/usr/bin/env python3
"""
Gas usage forecasting for comparing PA Gas Switch offers.

PA Gas Switch lists supplier prices in the utility's billing unit (Mcf for Peoples,
ccf or therm elsewhere); Peoples Gas portal reports usage in MCF.
We combine:
  - calendar-month profiling (captures heating season patterns), and
  - optional straight average (simple sanity check).

Env:
  THERMS_PER_MCF — heat content of your gas (default 10.37, typical for Pennsylvania;
                   your bill may list a BTU factor).
"""

from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from peoples_gas_analyzer import GasUsageData

DEFAULT_THERMS_PER_MCF = 10.37


def therms_per_mcf() -> float:
    """Therms per MCF used to align supplier $/therm with utility MCF usage."""
    try:
        return float(os.environ.get("THERMS_PER_MCF", str(DEFAULT_THERMS_PER_MCF)))
    except ValueError:
        return DEFAULT_THERMS_PER_MCF


def mcf_to_therms(mcf: float) -> float:
    return mcf * therms_per_mcf()


def usd_per_mcf_to_usd_per_therm(usd_per_mcf: float) -> float:
    t = therms_per_mcf()
    return usd_per_mcf / t if t else usd_per_mcf


GAS_PRICE_UNITS = ("mcf", "ccf", "therm")


def therms_per_gas_unit(unit: str) -> float:
    """Therms in one PA Gas Switch price unit ('mcf', 'ccf' = 0.1 Mcf, or 'therm')."""
    u = (unit or "").strip().lower()
    if u == "mcf":
        return therms_per_mcf()
    if u == "ccf":
        return therms_per_mcf() / 10.0
    if u == "therm":
        return 1.0
    raise ValueError(f"Unknown gas price unit {unit!r}; expected one of {GAS_PRICE_UNITS}")


def parse_money_field(value: Optional[str]) -> float:
    """Parse CSV 'Monthly Fee' style fields to dollars."""
    if value is None:
        return 0.0
    s = str(value).strip().lower()
    if not s or s in ("none", "n/a", "no", "$0", "0"):
        return 0.0
    m = re.search(r"\$?\s*(\d+\.?\d*)", s)
    if not m:
        return 0.0
    return float(m.group(1))


@dataclass
class ForecastResult:
    """Annual usage forecast with method metadata."""

    annual_mcf: float
    annual_therms: float
    method: str
    calendar_month_sample_counts: Dict[int, int] = field(default_factory=dict)
    calendar_month_mcf_typical: Dict[int, float] = field(default_factory=dict)

    trailing_twelve_months_mcf: Optional[float] = None


def _cycle_calendar_month(record: GasUsageData) -> int:
    """Calendar month index (1–12) for grouping usage cycles."""
    if record.to_date:
        try:
            return datetime.strptime(record.to_date, "%m/%d/%Y").month
        except ValueError:
            pass
    return record.month


def build_calendar_month_profile(
    records: List[GasUsageData],
    *,
    aggregator: str = "median",
) -> Tuple[Dict[int, float], Dict[int, int]]:
    """
    Group usage by **billing-cycle end calendar month**.
    Returns (month -> typical MCF, month -> sample count).
    """
    buckets: Dict[int, List[float]] = {m: [] for m in range(1, 13)}
    for r in records:
        if r.usage_mcf <= 0:
            continue
        buckets[_cycle_calendar_month(r)].append(r.usage_mcf)

    typical: Dict[int, float] = {}
    counts: Dict[int, int] = {}

    agg_fn = statistics.median if aggregator == "median" else statistics.mean

    for m in range(1, 13):
        vals = buckets[m]
        if not vals:
            continue
        typical[m] = float(agg_fn(vals))
        counts[m] = len(vals)

    return typical, counts


def _fill_calendar_gaps(typical: Dict[int, float]) -> Dict[int, float]:
    """Months without samples get the overall mean of months that exist."""
    if not typical:
        return {m: 0.0 for m in range(1, 13)}
    filler = statistics.mean(typical.values())
    return {m: typical.get(m, filler) for m in range(1, 13)}


def forecast_annual_from_calendar_profile(
    records: List[GasUsageData],
    *,
    aggregator: str = "median",
) -> ForecastResult:
    """
    Forward annual therm usage estimate: median (or mean) MCF per calendar month
    summed to 12 months.
    """
    typical, counts = build_calendar_month_profile(records, aggregator=aggregator)
    filled = _fill_calendar_gaps(typical)
    annual_mcf = sum(filled[m] for m in range(1, 13))
    t = therms_per_mcf()

    ttm_mcf = None
    if records:
        valid = sorted(
            (r for r in records if r.usage_mcf > 0),
            key=lambda r: (r.year, r.month, r.to_date or ""),
        )
        if len(valid) >= 12:
            ttm_mcf = sum(r.usage_mcf for r in valid[-12:])

    return ForecastResult(
        annual_mcf=annual_mcf,
        annual_therms=annual_mcf * t,
        method=f"calendar_month_{aggregator}",
        calendar_month_sample_counts=counts,
        calendar_month_mcf_typical=filled,
        trailing_twelve_months_mcf=ttm_mcf,
    )


def forecast_simple_average_annual_mcf(analysis_total_mcf: float, months: int) -> float:
    """Naive: (sum of months) / n × 12 — underweights seasonality if n < 12."""
    if months <= 0:
        return 0.0
    return (analysis_total_mcf / months) * 12.0

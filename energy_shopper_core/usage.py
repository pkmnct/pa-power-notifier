"""Electric usage window and annual projection from interval readings."""

from __future__ import annotations

import statistics
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Tuple

from .seasons import ELECTRIC_HIGH_USE_MONTHS


@dataclass
class UsageWindow:
    total_kwh: float
    span_days: float
    baseline_cost: float
    baseline_label: str
    annual_kwh: float = 0.0
    annual_method: str = ""
    month_kwh: Dict[int, float] = field(default_factory=dict)
    missing_high_use_months: List[int] = field(default_factory=list)


def span_days_from_hourly(records: List) -> float:
    if not records:
        return 1.0
    dts = []
    for r in records:
        try:
            dts.append(datetime.fromisoformat(r.dt.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            continue
    if len(dts) < 2:
        return 1.0
    dts.sort()
    return max(1.0, (dts[-1] - dts[0]).total_seconds() / 86400.0)


def project_annual_kwh_from_hourly(records: List) -> Tuple[float, str, Dict[int, float], List[int]]:
    """
    If 4+ calendar months have usage, typical-month × days-in-month, gap-fill like gas.
    Otherwise annualize the observed window.
    """
    by_month_days: Dict[int, set] = {m: set() for m in range(1, 13)}
    by_month_kwh: Dict[int, float] = {m: 0.0 for m in range(1, 13)}
    for r in records:
        if getattr(r, "kwh", 0) <= 0:
            continue
        try:
            dt = datetime.fromisoformat(r.dt.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        by_month_kwh[dt.month] += float(r.kwh)
        by_month_days[dt.month].add(dt.date())

    observed = [m for m in range(1, 13) if by_month_kwh[m] > 0]
    missing_high = sorted(m for m in ELECTRIC_HIGH_USE_MONTHS if m not in observed)

    if len(observed) >= 4:
        typical: Dict[int, float] = {}
        for m in observed:
            days_obs = max(1, len(by_month_days[m]))
            daily = by_month_kwh[m] / days_obs
            typical[m] = daily * monthrange(2025, m)[1]
        filler = statistics.mean(typical.values())
        filled = {m: typical.get(m, filler) for m in range(1, 13)}
        return sum(filled.values()), "calendar_month_typical_from_hourly", filled, missing_high

    total = sum(r.kwh for r in records if getattr(r, "kwh", 0) > 0)
    days = span_days_from_hourly(records)
    annual = total * (365.0 / days) if days > 0 else total
    return annual, f"annualized_{days:.0f}d_window", by_month_kwh, missing_high

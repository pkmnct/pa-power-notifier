#!/usr/bin/env python3
"""
Time-of-Use (TOU) Rate Analyzer for Duquesne Light residential customers.

Evaluates whether switching to the TOU Supply Rate Pilot would save money based on
actual hourly usage. (The hourly history file lives in usage_archive.HourlyUsageStore.)

TOU period definitions (Residential RS/RH/RA, from Duquesne Light Rider No. 8):
  Peak:           3:00 PM – 9:00 PM, Mon–Fri, excluding PJM holidays
  Super Off-Peak: 11:00 PM – 6:00 AM daily (including weekends and PJM holidays)
  Off-Peak:       All other hours

PJM Holidays: New Year's Day, Memorial Day, Independence Day,
              Labor Day, Thanksgiving Day, Christmas Day
"""

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PJM holiday computation
# ---------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth occurrence (1-indexed) of weekday in month. weekday: 0=Mon…6=Sun"""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last occurrence of weekday in month."""
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


def pjm_holidays_for_year(year: int) -> frozenset:
    """Return the set of PJM holidays that exclude Peak TOU hours for a year."""
    return frozenset({
        date(year, 1, 1),                        # New Year's Day
        _last_weekday(year, 5, 0),               # Memorial Day (last Monday of May)
        date(year, 7, 4),                        # Independence Day
        _nth_weekday(year, 9, 0, 1),             # Labor Day (1st Monday of September)
        _nth_weekday(year, 11, 3, 4),            # Thanksgiving (4th Thursday of November)
        date(year, 12, 25),                      # Christmas Day
    })


# Cache holidays per year so we're not recomputing on every hour
_holiday_cache: Dict[int, frozenset] = {}


def _holidays(year: int) -> frozenset:
    if year not in _holiday_cache:
        _holiday_cache[year] = pjm_holidays_for_year(year)
    return _holiday_cache[year]


# ---------------------------------------------------------------------------
# TOU period classification
# ---------------------------------------------------------------------------

def classify_tou_period(dt: datetime) -> str:
    """
    Return 'peak', 'super_off_peak', or 'off_peak' for a given datetime.

    The datetime should represent the *start* of the hour being classified.
    Timezone info is respected but not required.
    """
    d = dt.date()
    h = dt.hour

    # Super Off-Peak: 11:00 PM through 5:59 AM (hours 23, 0–5)
    if h >= 23 or h < 6:
        return 'super_off_peak'

    # Peak: 3:00 PM through 8:59 PM (hours 15–20), weekdays, non-holiday
    if 15 <= h < 21 and dt.weekday() < 5 and d not in _holidays(d.year):
        return 'peak'

    return 'off_peak'


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TOURates:
    """
    Duquesne Light TOU supply rates for a given application period.
    All rates are in $/kWh (converted from ¢/kWh in the tariff documents).
    """
    standard_rate: float         # Flat DSS supply rate $/kWh
    peak_rate: float             # TOU Peak $/kWh
    off_peak_rate: float         # TOU Off-Peak $/kWh
    super_off_peak_rate: float   # TOU Super Off-Peak $/kWh
    effective_start: str         # ISO date, e.g. "2025-12-01"
    effective_end: str           # ISO date, e.g. "2026-05-31"
    source: str = ''             # URL or description of source


@dataclass
class HourlyUsageRecord:
    dt: str     # ISO 8601 start-of-hour, e.g. "2025-10-27T14:00:00-04:00"
    kwh: float  # kWh consumed during this hour
    cost: float  # cost at actual flat rate (0.0 if not available from API)


@dataclass
class TOUAnalysisResult:
    days_analyzed: int
    total_kwh: float
    actual_total_cost: float       # supply-only cost at flat DSS rate (fair TOU comparison baseline)
    tou_total_cost: float          # estimated supply-only cost under TOU
    real_billed_cost: float        # actual $ billed for the period, if available (all charges included)
    real_billed_rate_includes_more_than_supply: bool  # True if real_billed_cost != a flat-supply estimate
    peak_kwh: float
    off_peak_kwh: float
    super_off_peak_kwh: float
    peak_pct: float
    off_peak_pct: float
    super_off_peak_pct: float
    annual_savings_projection: float   # positive = TOU is cheaper (supply-only basis)
    annual_savings_pct: float          # relative to supply-only cost, NOT total bill
    tou_recommended: bool
    date_range: Tuple[str, str]


# ---------------------------------------------------------------------------
# TOU analysis
# ---------------------------------------------------------------------------

class TOUAnalyzer:
    """Evaluates TOU vs flat-rate cost using stored hourly usage data."""

    def analyze(
        self,
        records: List[HourlyUsageRecord],
        tou_rates: TOURates,
    ) -> Optional[TOUAnalysisResult]:
        """
        Compare what the customer would pay on flat DSS supply against what
        they would pay on TOU supply, for the same measured usage.

        Note: these are supply-charge-only comparisons. Distribution, transmission,
        riders, and taxes are the same under either option, so they're excluded from
        the savings estimate — including them would double-count charges that don't
        change when you switch supply structure.

        Some utilities' hourly APIs (e.g. Duquesne/Opower) report a per-interval
        "cost" that is actually `kWh × your current blended all-in rate` (i.e. it
        already includes distribution/transmission/riders, not just supply). Using
        that figure as the "current" side of a supply-only TOU comparison wildly
        overstates savings, since it's compared against a supply-only TOU estimate.
        To stay apples-to-apples, the flat-vs-TOU comparison always uses
        `total_kwh * tou_rates.standard_rate` for the current side. The real billed
        total (when available) is reported separately, for context only.
        """
        valid = [r for r in records if r.kwh > 0]
        if not valid:
            logger.warning("No non-zero hourly usage records to analyze")
            return None

        total_kwh = sum(r.kwh for r in valid)
        real_billed_cost = sum(r.cost for r in valid)

        # Fair "current" baseline for the TOU comparison: what you'd pay for this
        # usage on DLC's flat DSS supply rate alone (matches the TOU rates' basis).
        actual_cost = total_kwh * tou_rates.standard_rate

        # Flag when the real billed total looks like more than a flat-supply
        # estimate (e.g. it includes distribution) so the report can warn about it.
        real_billed_rate_includes_more_than_supply = (
            real_billed_cost > 0
            and actual_cost > 0
            and real_billed_cost > actual_cost * 1.15
        )

        peak_kwh = off_peak_kwh = super_off_peak_kwh = 0.0
        tou_cost = 0.0

        for r in valid:
            try:
                dt = datetime.fromisoformat(r.dt.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                logger.debug("Could not parse datetime: %s", r.dt)
                continue

            period = classify_tou_period(dt)
            if period == 'peak':
                peak_kwh += r.kwh
                tou_cost += r.kwh * tou_rates.peak_rate
            elif period == 'super_off_peak':
                super_off_peak_kwh += r.kwh
                tou_cost += r.kwh * tou_rates.super_off_peak_rate
            else:
                off_peak_kwh += r.kwh
                tou_cost += r.kwh * tou_rates.off_peak_rate

        sorted_dts = sorted(r.dt[:10] for r in valid)
        date_range = (sorted_dts[0], sorted_dts[-1])

        try:
            span_days = (
                datetime.fromisoformat(sorted_dts[-1]) -
                datetime.fromisoformat(sorted_dts[0])
            ).days + 1
        except Exception:
            span_days = len(set(r.dt[:10] for r in valid))
        span_days = max(1, span_days)

        scale = 365 / span_days
        annual_actual = actual_cost * scale
        annual_tou = tou_cost * scale
        annual_savings = annual_actual - annual_tou

        return TOUAnalysisResult(
            days_analyzed=span_days,
            total_kwh=total_kwh,
            actual_total_cost=actual_cost,
            tou_total_cost=tou_cost,
            real_billed_cost=real_billed_cost,
            real_billed_rate_includes_more_than_supply=real_billed_rate_includes_more_than_supply,
            peak_kwh=peak_kwh,
            off_peak_kwh=off_peak_kwh,
            super_off_peak_kwh=super_off_peak_kwh,
            peak_pct=100 * peak_kwh / total_kwh if total_kwh else 0,
            off_peak_pct=100 * off_peak_kwh / total_kwh if total_kwh else 0,
            super_off_peak_pct=100 * super_off_peak_kwh / total_kwh if total_kwh else 0,
            annual_savings_projection=annual_savings,
            annual_savings_pct=100 * annual_savings / annual_actual if annual_actual else 0,
            tou_recommended=annual_savings > 0,
            date_range=date_range,
        )

    def format_report(self, result: TOUAnalysisResult, tou_rates: TOURates) -> str:
        action = "SAVE" if result.tou_recommended else "COST EXTRA"
        diff = abs(result.annual_savings_projection)
        scale = 365 / result.days_analyzed if result.days_analyzed else 0
        lines = [
            "--- TIME-OF-USE (TOU) SUPPLY RATE ANALYSIS ---",
            f"Data range       : {result.date_range[0]} through {result.date_range[1]}",
            f"                   ({result.days_analyzed} days, {result.total_kwh:.0f} kWh total)",
            "",
            "Usage by TOU period:",
            f"  Peak   (3–9 PM Mon–Fri)  : {result.peak_kwh:>7.1f} kWh  ({result.peak_pct:4.1f}%)",
            f"  Off-Peak                 : {result.off_peak_kwh:>7.1f} kWh  ({result.off_peak_pct:4.1f}%)",
            f"  Super Off-Peak (11PM–6AM): {result.super_off_peak_kwh:>7.1f} kWh  ({result.super_off_peak_pct:4.1f}%)",
            "",
            "Supply rates compared (¢/kWh, supply charge only — excludes distribution/transmission/riders):",
            f"  Flat (current)           : {tou_rates.standard_rate * 100:.4f}¢",
            f"  TOU Peak                 : {tou_rates.peak_rate * 100:.4f}¢  "
            f"({'HIGHER' if tou_rates.peak_rate > tou_rates.standard_rate else 'lower'})",
            f"  TOU Off-Peak             : {tou_rates.off_peak_rate * 100:.4f}¢  "
            f"({'lower' if tou_rates.off_peak_rate < tou_rates.standard_rate else 'HIGHER'})",
            f"  TOU Super Off-Peak       : {tou_rates.super_off_peak_rate * 100:.4f}¢  "
            f"({'lower' if tou_rates.super_off_peak_rate < tou_rates.standard_rate else 'HIGHER'})",
            f"  (Rates effective {tou_rates.effective_start} – {tou_rates.effective_end})",
            "",
            "Supply-only cost estimate (fair TOU comparison — distribution/transmission/riders",
            "unaffected by TOU enrollment and excluded from both sides):",
            f"  Flat DSS supply          : ${result.actual_total_cost:.2f}",
            f"  Under TOU supply         : ${result.tou_total_cost:.2f}",
            "",
            f"Projected annual impact    : TOU supply charge would {action} ~${diff:.2f}/year "
            f"({abs(result.annual_savings_pct):.1f}% of the SUPPLY charge, not your total bill)",
            f"Recommendation             : {'*** CONSIDER SWITCHING TO TOU ***' if result.tou_recommended else 'Stay on flat rate'}",
        ]
        if result.real_billed_cost > 0:
            annual_real = result.real_billed_cost * scale
            lines += [
                "",
                f"For context, your actual billed total for this period was ${result.real_billed_cost:.2f} "
                f"(~${annual_real:,.0f}/yr), i.e. ~{100*result.real_billed_cost/result.total_kwh:.2f}¢/kWh all-in "
                "(supply + distribution + transmission + riders + taxes).",
            ]
            if result.real_billed_rate_includes_more_than_supply:
                lines.append(
                    "  NOTE: this all-in rate is well above the flat DSS supply rate above, so it clearly "
                    "includes non-supply charges. Those charges do NOT change if you switch to TOU — only "
                    "the supply portion shown above does. Do not compare your all-in bill directly to the "
                    "TOU supply savings figure; the annual impact above is the real number to use."
                )
        if result.tou_recommended:
            lines += [
                "",
                "EV Charging Tip: Schedule charging during Super Off-Peak (11 PM – 6 AM)",
                f"  to pay only {tou_rates.super_off_peak_rate * 100:.4f}¢/kWh supply vs "
                f"{tou_rates.standard_rate * 100:.4f}¢/kWh flat "
                f"({100*(1 - tou_rates.super_off_peak_rate/tou_rates.standard_rate):.0f}% cheaper on supply).",
            ]
        return "\n".join(lines)

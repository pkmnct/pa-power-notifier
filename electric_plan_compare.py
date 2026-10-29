#!/usr/bin/env python3
"""
Fetch PA Power Switch electric offers, combine with your historical kWh usage,
and rank plans by estimated annual supply cost (energy + monthly fees).

Thin CLI over recommendation_engine — the daily notifier uses the same ranking.

Usage:
  python electric_plan_compare.py

Loads .env via env_bootstrap when present (same as pa_power_notifier).

Usage data (first match wins):
  1. HourlyUsageStore file (HOURLY_USAGE_FILE)
  2. Duquesne daily GraphQL analysis
  3. Fallback: USAGE_FALLBACK_KWH and USAGE_FALLBACK_DAYS env vars
"""

from __future__ import annotations

import logging
import os
import sys

try:
    from env_bootstrap import configure_logging_from_env

    configure_logging_from_env()
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from pa_power_notifier import RateChecker
from recommendation_engine import rank_plans_by_annual, resolve_usage

logger = logging.getLogger(__name__)


def main() -> int:
    checker = RateChecker()
    top_n = int(os.getenv("COMPARE_TOP_N", "25"))

    usage = resolve_usage(checker)
    if usage is None:
        logger.error(
            "No usage data. Add hourly data under %s, configure Duquesne, or set "
            "USAGE_FALLBACK_KWH and USAGE_FALLBACK_DAYS.",
            checker.hourly_usage_file,
        )
        return 1

    rates = checker.fetch_electric_rates()
    if not rates:
        logger.error("No rates returned from PA Power Switch.")
        return 1

    filtered = checker.apply_filters(rates)
    annual_kwh = usage.annual_kwh or (
        usage.total_kwh * (365.0 / usage.span_days) if usage.span_days else 0.0
    )
    ranked = rank_plans_by_annual(filtered, annual_kwh, "kWh")
    if not ranked:
        logger.error("No parseable electric rates after filtering.")
        return 1

    supply_reference = checker.get_electric_supply_reference_rate()
    blended_rate = usage.baseline_cost / usage.total_kwh if usage.total_kwh > 0 else 0.0
    non_supply_rate = (blended_rate - supply_reference) if supply_reference else None

    print()
    print("=" * 72)
    print("PA POWER SWITCH — RANK BY YOUR USAGE (estimated supply + monthly fees)")
    print("=" * 72)
    print(
        f"  Window      : {usage.span_days:.0f} days  |  {usage.total_kwh:,.1f} kWh total"
    )
    print(f"  Baseline    : ${usage.baseline_cost:,.2f}  ({usage.baseline_label})")
    print(f"  Annual kWh  : {annual_kwh:,.0f}  ({usage.annual_method or 'annualized window'})")
    if usage.baseline_cost > 0 and usage.total_kwh > 0:
        print(
            f"  Implied $/kWh over window: ${blended_rate:.5f}  (all-in: supply + "
            "distribution + transmission + riders)"
        )
    if non_supply_rate is not None:
        print(
            f"  Supply reference: ${supply_reference:.5f}/kWh (Price to Compare / flat DSS) "
            f"→ non-supply (distribution/transmission/riders): ${non_supply_rate:.5f}/kWh, "
            "assumed unchanged across suppliers below"
        )
    else:
        print(
            "  WARNING: no supply-only reference rate resolved — the ranking below is still valid "
            "for comparing suppliers against EACH OTHER, but comparing 'Annual ~' directly to your "
            "Baseline overstates real savings. Set TOU_FLAT_RATE (¢/kWh) for a realistic estimate."
        )
    if usage.missing_high_use_months:
        print(
            f"  Seasonal     : missing summer months {usage.missing_high_use_months} in this window"
        )
    print(
        f"  Rate type   : {checker.electric_rate_type}  (ELECTRIC_RATE_TYPE)"
    )
    print(
        f"  Filters     : no_cancel={checker.filter_no_cancellation_fee}  "
        f"renewable_only={checker.filter_renewable_only}"
    )
    print("=" * 72)
    print(
        f"{'#':<4}{'Annual $':>11}  "
        f"{'$/kWh':>8}  {'$/mo':>8}  Term      Supplier / type"
    )
    print("-" * 72)

    for i, opt in enumerate(ranked[:top_n], 1):
        p = opt.unit_price or 0.0
        line = (
            f"{i:<4} "
            f"${opt.annual_supply_usd:>9,.0f}  "
            f"{p:>8.4f}  {opt.monthly_fee:>8.2f}  "
            f"{(opt.term_raw or '')[:8]:8}  "
            f"{opt.supplier[:28]} — {opt.plan_type[:32]}"
        )
        print(line)
        for flag in opt.flags:
            print(f"      ! {flag}")

    best = ranked[0]
    print("-" * 72)
    print(
        f"BEST AS OF NOW (lowest estimated annual supply cost): "
        f"{best.supplier} — {best.plan_type}"
    )
    print(f"  ~${best.annual_supply_usd:,.0f}/yr scaled")
    if best.unit_price is not None:
        print(
            f"  Listed energy: ${best.unit_price:.4f}/kWh  |  "
            f"Monthly fee: ${best.monthly_fee:.2f}"
        )
    if best.more_info:
        print(f"  More info: {best.more_info}")

    if non_supply_rate is not None and best.unit_price is not None:
        est_new_annual_bill = non_supply_rate * annual_kwh + best.unit_price * annual_kwh + best.monthly_fee * 12
        est_current_annual_bill = blended_rate * annual_kwh
        real_savings = est_current_annual_bill - est_new_annual_bill
        real_savings_pct = (real_savings / est_current_annual_bill * 100) if est_current_annual_bill else 0.0
        print()
        print("  REALISTIC estimate (accounts for distribution/transmission/riders staying the same):")
        print(f"    Est. current all-in annual bill : ${est_current_annual_bill:,.0f}/yr")
        print(f"    Est. new all-in annual bill      : ${est_new_annual_bill:,.0f}/yr")
        print(f"    Est. real annual savings         : ${real_savings:,.0f}/yr ({real_savings_pct:.1f}%)")

    print("=" * 72)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

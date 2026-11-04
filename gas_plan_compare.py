#!/usr/bin/env python3
"""
Periodic gas comparison: Peoples Gas usage + PA Gas Switch supplier rates.

Run weekly or monthly via cron:

  PEOPLES_GAS_USERNAME=... PEOPLES_GAS_PASSWORD=... \\
    python3 gas_plan_compare.py

  make gas-plan-check

Interpretation:
  - Usage is modeled from your billing history via **median MCF per calendar month**
    (captures heating season), converted to therms via THERMS_PER_MCF (default 10.37).
  - PA Gas Switch CSV prices are **supplier** rates in the utility's billing unit
    (Mcf for Peoples), plus a Monthly Fee column. They are compared with the utility's
    supply-only Price to Compare from the PA Gas Switch shop page (needs GAS_ZIPCODE).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from env_bootstrap import configure_logging_from_env

    configure_logging_from_env()
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

from gas_usage_forecast import (
    ForecastResult,
    forecast_annual_from_calendar_profile,
    mcf_to_therms,
    therms_per_gas_unit,
    therms_per_mcf,
    usd_per_mcf_to_usd_per_therm,
)
from pa_power_notifier import Rate, RateChecker
from peoples_gas_analyzer import GasUsageAnalysis, PeoplesGasAnalyzer
from recommendation_engine import rank_plans_by_annual

logger = logging.getLogger(__name__)


def _implied_usd_per_therm(analysis: GasUsageAnalysis) -> Optional[float]:
    """Total dollars / total therms across analyzed months — blended proxy."""
    if analysis.total_usage_mcf <= 0:
        return None
    if not analysis.average_monthly_cost:
        return None
    dollars = analysis.average_monthly_cost * analysis.months_analyzed
    therms_actual = analysis.total_usage_mcf * therms_per_mcf()
    if therms_actual > 0:
        return dollars / therms_actual
    return None


def _rank_plans(
    rates: List[Rate],
    annual_units: float,
    unit: str,
    checker: RateChecker,
) -> List[Dict[str, Any]]:
    """Annual supply-energy + monthly fees (supplier side only), in the listed unit."""
    filtered = checker.apply_filters(rates)
    ranked = rank_plans_by_annual(filtered, annual_units, unit)
    out: List[Dict[str, Any]] = []
    for opt in ranked:
        out.append(
            {
                "supplier": opt.supplier,
                "type": opt.plan_type,
                "price_usd_per_unit": opt.unit_price,
                "unit": unit,
                "monthly_fee_usd": opt.monthly_fee,
                "annual_energy_usd": round((opt.unit_price or 0) * annual_units, 2),
                "annual_with_fees_usd": opt.annual_supply_usd,
                "term_length": opt.term_raw,
                "cancellation_fee": opt.cancellation_fee,
                "introductory_price": opt.intro_raw,
                "flags": opt.flags,
            }
        )
    return out


def run_report(top_n: int = 15, json_out: Optional[str] = None) -> Dict[str, Any]:
    checker = RateChecker()
    analyzer = PeoplesGasAnalyzer()

    results = analyzer.run_analysis()
    usage_list = results.get("usage_data") or []
    analysis: Optional[GasUsageAnalysis] = results.get("usage_analysis")
    effective_mcf = results.get("current_effective_rate")

    if not analysis:
        logger.error("No usage analysis produced (credentials or PEOPLES_GAS_USE_FILE?)")
        return {}

    fc: ForecastResult = forecast_annual_from_calendar_profile(
        usage_list,
        aggregator=os.environ.get("GAS_PROFILE_AGGREGATOR", "median"),
    )

    ptc, unit, ptc_source = checker.get_gas_price_to_compare()
    if not unit:
        logger.error("Unknown gas price unit — set GAS_ZIPCODE or GAS_PRICE_UNIT (mcf, ccf or therm)")
        return {}
    annual_units = fc.annual_therms / therms_per_gas_unit(unit)
    gas_rates = checker.fetch_gas_rates()
    ranked = _rank_plans(gas_rates, annual_units, unit, checker)

    implied_therm_from_mcf = None
    if effective_mcf and effective_mcf > 0:
        implied_therm_from_mcf = usd_per_mcf_to_usd_per_therm(effective_mcf)
    avg_bill_blend = _implied_usd_per_therm(analysis)

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "therms_per_mcf": therms_per_mcf(),
        "forecast": {
            "method": fc.method,
            "annual_mcf": round(fc.annual_mcf, 2),
            "annual_therms": round(fc.annual_therms, 2),
            "calendar_month_sample_counts": fc.calendar_month_sample_counts,
            "trailing_twelve_months_mcf": fc.trailing_twelve_months_mcf,
            "annual_units": round(annual_units, 2),
            "unit": unit,
            "naive_scaled_annual_mcf": round(
                (analysis.total_usage_mcf / max(analysis.months_analyzed, 1)) * 12,
                2,
            ),
        },
        "current_account": {
            "months_analyzed": analysis.months_analyzed,
            "avg_monthly_mcf": round(analysis.average_monthly_usage_mcf, 3),
            "avg_monthly_bill_usd": analysis.average_monthly_cost,
            "effective_rate_usd_per_mcf": effective_mcf,
            "effective_rate_usd_per_therm_from_mcf_blend": implied_therm_from_mcf,
            "implied_usd_per_therm_avg_bill_blend": avg_bill_blend,
        },
        "price_to_compare": {
            "usd_per_unit": ptc,
            "unit": unit,
            "source": ptc_source or None,
            "annual_usd": round(ptc * annual_units, 2) if ptc else None,
        },
        "top_plans": ranked[:top_n],
    }

    lines = [
        "Gas plan comparison (PA Gas Switch supplier rates vs your usage forecast)",
        f"Therms/MCF conversion: {therms_per_mcf()} (set THERMS_PER_MCF to adjust)",
        "",
        "Usage forecast:",
        f"  Method: {fc.method}",
        f"  Annual MCF (~): {payload['forecast']['annual_mcf']}",
        f"  Annual therms (~): {payload['forecast']['annual_therms']}",
    ]
    if fc.trailing_twelve_months_mcf is not None:
        lines.append(
            f"  Trailing-12-month sum MCF: {fc.trailing_twelve_months_mcf:.2f} "
            f"→ {mcf_to_therms(fc.trailing_twelve_months_mcf):.1f} therms"
        )
    lines.append(
        f"  Naive scale-to-12-mo MCF: {payload['forecast']['naive_scaled_annual_mcf']}"
    )

    lines += [
        "",
        "Current bill / usage (all-in; includes delivery/riders/taxes):",
        f"  Avg monthly bill: ${analysis.average_monthly_cost or 0:.2f}",
        (
            "  Effective ~$/therm from $/MCF × conversion: "
            + (f"${implied_therm_from_mcf:.4f}" if implied_therm_from_mcf else "n/a")
        ),
        "",
        (
            f"Price to Compare (supply only, {ptc_source}): ${ptc:.4f}/{unit}  "
            f"annual≈${ptc * annual_units:.0f}"
            if ptc
            else "Price to Compare: n/a (set GAS_ZIPCODE or GAS_PRICE_TO_COMPARE)"
        ),
        "",
        f"Cheapest supplier offers (annual supplier energy+fees @ {annual_units:,.1f} {unit}/yr):",
    ]
    for i, row in enumerate(payload["top_plans"][:top_n], 1):
        lines.append(
            f"  {i:>2}. {row['supplier'][:40]:40} "
            f"${row['price_usd_per_unit']:.4f}/{unit}  "
            f"annual≈${row['annual_with_fees_usd']:.0f}  "
            f"({row['type'][:30]})"
        )

    lines += [
        "",
        "Note: offers and Price to Compare are supply only; your bill adds distribution.",
    ]
    print("\n".join(lines))

    if json_out:
        parent = os.path.dirname(json_out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        logger.info("Wrote JSON report to %s", json_out)

    return payload


def main() -> int:
    p = argparse.ArgumentParser(description="Compare PA Gas Switch rates to your usage forecast.")
    p.add_argument("--top", type=int, default=15, help="Number of rows to print (default 15)")
    p.add_argument(
        "--json-out",
        default=os.environ.get("GAS_PLAN_REPORT_JSON", ""),
        help="Write machine-readable report to this path (optional)",
    )
    args = p.parse_args()
    try:
        run_report(top_n=args.top, json_out=args.json_out or None)
    except Exception as e:
        logger.exception("gas_plan_compare failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Offline tests for energy_shopper_core.decision (stdlib only, part of make unit-test)."""

from __future__ import annotations

import unittest

from energy_shopper_core.decision import (
    Baseline,
    UsageCycle,
    convert_gas_price,
    decide_gas,
    normalize_gas_unit,
)
from energy_shopper_core.eligibility import parse_prior_suppliers
from energy_shopper_core.models import Rate

MONTHLY_MCF = {1: 14.0, 2: 12.0, 3: 9.0, 4: 5.0, 5: 2.5, 6: 1.5, 7: 1.2, 8: 1.2, 9: 1.6, 10: 3.5, 11: 8.0, 12: 12.0}


def offer(supplier: str, price: float, kind: str = "Fixed", **kw) -> Rate:
    return Rate(
        price=f"${price:.4f}", supplier=supplier, type=kind,
        cancellation_fee=kw.get("cancellation_fee", "None"), deposit_required="No",
        discounts_incentives="", introductory_price="", monthly_fee=kw.get("monthly_fee", "None"),
        more_info="", service_type="Residential", term_end_date="", term_length="12 Months",
        utility_type="gas", new_customers_offer=kw.get("new", ""),
    )


def two_years() -> list:
    return [UsageCycle(usage_mcf=mcf, month=m, year=y) for y in (2024, 2025) for m, mcf in MONTHLY_MCF.items()]


OFFERS = [
    offer("Steady", 3.00),
    offer("Fee", 2.90, cancellation_fee="$150"),
    offer("Welcome", 2.80, new="Yes"),
    offer("Floaty", 3.40, kind="Variable"),
]


class UnitTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_gas_unit("USD/CCF"), "ccf")
        self.assertEqual(normalize_gas_unit("$/Mcf"), "mcf")
        self.assertEqual(normalize_gas_unit("therms"), "therm")
        self.assertEqual(normalize_gas_unit("USD/ft³"), "ft3")
        self.assertIsNone(normalize_gas_unit("USD/kWh"))

    def test_convert(self):
        self.assertAlmostEqual(convert_gas_price(0.36, "USD/CCF", "mcf"), 3.6)
        self.assertAlmostEqual(convert_gas_price(3.6, "mcf", "ccf"), 0.36)
        self.assertAlmostEqual(convert_gas_price(0.30, "therm", "mcf", 10.0), 3.0)
        self.assertAlmostEqual(convert_gas_price(0.0036, "ft3", "mcf"), 3.6)
        with self.assertRaises(ValueError):
            convert_gas_price(1.0, "kWh", "mcf")


class DecideGasTests(unittest.TestCase):
    def test_switch_to_cheapest(self):
        d = decide_gas(OFFERS, unit="mcf", usage=two_years(), baseline=Baseline(3.57), threshold_pct=5.0)
        annual = sum(MONTHLY_MCF.values())
        self.assertAlmostEqual(d.annual_units, annual)
        self.assertTrue(d.recommend_switch)
        self.assertEqual(d.recommended.supplier, "Welcome")
        self.assertAlmostEqual(d.savings_annual_usd, round(annual * 3.57, 2) - round(annual * 2.80, 2), places=2)
        self.assertEqual(d.stay.plan_type, "Default service (Price to Compare)")
        self.assertEqual(d.notes, [])

    def test_threshold_keeps_you(self):
        d = decide_gas(OFFERS, unit="mcf", usage=two_years(), baseline=Baseline(3.57), threshold_pct=50.0)
        self.assertFalse(d.recommend_switch)
        self.assertIs(d.recommended, d.stay)

    def test_prior_supplier_and_filter(self):
        d = decide_gas(
            OFFERS, unit="mcf", usage=two_years(), baseline=Baseline(3.57), threshold_pct=5.0,
            prior=parse_prior_suppliers("Welcome"), no_cancellation_fee=True,
        )
        self.assertEqual(d.recommended.supplier, "Steady")
        self.assertEqual(d.offers_eligible, 3)
        self.assertEqual(d.offers_after_filters, 2)
        self.assertIn(("Best your filters removed", d.highlights[-1][1]), d.highlights)
        self.assertTrue(any("Skipped 1 Welcome offer(s)" in m for _, m in d.notes))

    def test_no_usage_ranks_by_price(self):
        d = decide_gas(OFFERS, unit="mcf", usage=[], baseline=Baseline(3.57), threshold_pct=5.0)
        self.assertEqual(d.annual_units, 0)
        self.assertIsNone(d.stay)
        self.assertEqual([o.supplier for o in d.ranked][:2], ["Welcome", "Fee"])
        self.assertIn(("warning", "Ranked by listed $/mcf because annual usage is unknown."), d.notes)

    def test_missing_months_are_noted(self):
        winter_only = [c for c in two_years() if c.month in (1, 2, 12)]
        d = decide_gas(OFFERS, unit="mcf", usage=winter_only, baseline=Baseline(3.57), threshold_pct=5.0)
        self.assertTrue(d.seasonal_coverage_note.startswith("No samples for calendar months"))
        self.assertTrue(any("Heating-season" in m for _, m in d.notes))

    def test_ccf_summary(self):
        d = decide_gas(OFFERS, unit="ccf", usage=two_years(), baseline=Baseline(0.357), threshold_pct=5.0)
        self.assertTrue(d.usage_summary.endswith("ccf/yr"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Electric
# ---------------------------------------------------------------------------

from energy_shopper_core.decision import CurrentPlan, decide_electric  # noqa: E402
from energy_shopper_core.tou import TOUAnalysisResult, TOURates  # noqa: E402
from energy_shopper_core.usage import UsageWindow  # noqa: E402


def e_offer(supplier: str, price: float, **kw) -> Rate:
    r = offer(supplier, price, **kw)
    r.utility_type = "electric"
    return r


E_OFFERS = [e_offer("Bright", 0.10), e_offer("Fee", 0.095, cancellation_fee="$100"), e_offer("Pricey", 0.19, kind="Variable")]
USAGE = UsageWindow(total_kwh=2000, span_days=180, baseline_cost=0.0, baseline_label="test", annual_kwh=10000, annual_method="test")
TOU_RATES = TOURates(0.14, 0.20, 0.12, 0.06, "", "9999-12-31", "test")


def tou_result(peak=10.0, off=40.0, sup=50.0) -> TOUAnalysisResult:
    total = peak + off + sup
    return TOUAnalysisResult(
        days_analyzed=60, total_kwh=total, actual_total_cost=0, tou_total_cost=0, real_billed_cost=0,
        real_billed_rate_includes_more_than_supply=False, peak_kwh=peak, off_peak_kwh=off, super_off_peak_kwh=sup,
        peak_pct=100 * peak / total, off_peak_pct=100 * off / total, super_off_peak_pct=100 * sup / total,
        annual_savings_projection=1.0, annual_savings_pct=1.0, tou_recommended=True, date_range=("2026-09-01", "2026-10-30"),
    )


class DecideElectricTests(unittest.TestCase):
    def test_from_supplier_to_cheaper(self):
        d = decide_electric(
            E_OFFERS, usage=USAGE, current=CurrentPlan("Mine", 0.15), price_to_compare=0.1414, threshold_pct=5.0
        )
        self.assertEqual(d.stay.supplier, "Mine")
        self.assertTrue(d.recommend_switch)
        self.assertEqual(d.recommended.supplier, "Fee")
        self.assertAlmostEqual(d.savings_annual_usd, 1500.0 - 950.0)
        self.assertTrue(any(o.is_utility_default for o in d.ranked))
        self.assertIsNone(d.tou_recommended)
        self.assertEqual(d.notes, [])

    def test_default_service_is_the_stay(self):
        d = decide_electric(E_OFFERS[2:], usage=USAGE, current=None, price_to_compare=0.1414, threshold_pct=5.0)
        self.assertTrue(d.stay.is_utility_default)
        self.assertFalse(d.recommend_switch)

    def test_blended_fallback_flags_itself(self):
        d = decide_electric(E_OFFERS, usage=USAGE, current=None, price_to_compare=None, threshold_pct=5.0, blended_rate=0.2)
        self.assertIn("overstates", d.stay.supplier)

    def test_time_of_use_wins_and_shift_variants(self):
        d = decide_electric(
            E_OFFERS[2:], usage=USAGE, current=CurrentPlan("Mine", 0.15), price_to_compare=0.14,
            threshold_pct=5.0, tou_rates=TOU_RATES, tou_result=tou_result(),
        )
        self.assertTrue(d.recommended.is_tou)
        self.assertTrue(d.tou_recommended)
        self.assertTrue(d.recommend_switch)
        expected = 10000 * (0.1 * 0.20 + 0.4 * 0.12 + 0.5 * 0.06)
        self.assertAlmostEqual(d.recommended.annual_supply_usd, round(expected, 2))
        labels = [l for l, _ in d.highlights]
        self.assertIn("TOU, shift 25% of peak", labels)
        self.assertIn("TOU, shift 50% of peak", labels)

    def test_time_of_use_below_threshold(self):
        d = decide_electric(
            [], usage=USAGE, current=CurrentPlan("Mine", 0.099), price_to_compare=0.14,
            threshold_pct=5.0, tou_rates=TOU_RATES, tou_result=tou_result(),
        )
        self.assertFalse(d.tou_recommended)
        self.assertFalse(d.recommend_switch)

    def test_short_window_and_missing_summer_are_usage_notes(self):
        short = UsageWindow(500, 30, 0.0, "test", annual_kwh=6000, annual_method="test", missing_high_use_months=[6, 7])
        d = decide_electric(E_OFFERS, usage=short, current=None, price_to_compare=0.14, threshold_pct=5.0)
        self.assertEqual(len(d.usage_notes), 2)
        self.assertEqual(d.notes, [])
        self.assertEqual(d.all_notes, d.usage_notes)

    def test_no_usage(self):
        d = decide_electric(E_OFFERS, usage=None, current=CurrentPlan("Mine", 0.15), price_to_compare=0.14, threshold_pct=5.0)
        self.assertEqual(d.annual_kwh, 0)
        self.assertIn(("warning", "Ranked by listed $/kWh because annual kWh is unknown."), d.notes)


class GasPriceToCompareTests(unittest.TestCase):
    def test_uses_the_configured_service_type(self):
        from unittest.mock import patch

        from energy_shopper_core.markets import pa

        with patch.object(pa, "scrape_pagasswitch_price_to_compare", return_value=(3.5, "mcf")) as scrape:
            pa.PaGasSwitch("1234", service_type="commercial", zipcode="15222").price_to_compare()
        self.assertEqual(scrape.call_args.kwargs.get("service_type"), "commercial")

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

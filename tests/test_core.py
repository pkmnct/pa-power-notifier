"""Offline tests for energy_shopper_core. Synthetic data only: no network, files or env."""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from energy_shopper_core import gas
from energy_shopper_core.alerts import own_rate_alert
from energy_shopper_core.eligibility import ineligible_reason, parse_prior_suppliers, prior_suppliers
from energy_shopper_core.markets.pa import (
    only_service_type,
    parse_electric_shop_results_url,
    parse_offers_csv,
)
from energy_shopper_core.ranking import (
    best_by_category,
    parse_term_months,
    pick_recommended,
    rank_plans_by_annual,
)
from energy_shopper_core.report import RankedOption
from energy_shopper_core.tou import classify_tou_period

CSV = (
    "Supplier,Price,Type,Term Length,Monthly Fee,Cancellation Fee,Service Type,"
    "New Customers Offer,Renewable Energy,More info\n"
    "Alpha Energy LLC,$0.1000,Fixed,12,,None,Residential,No,No,\n"
    "Beta Power,$0.0900,Fixed,6,$10,$50,Residential,Yes,Yes,100% wind. New customers only.\n"
    "Gamma Co,$0.0800,Variable,Month to month,,None,Commercial,No,No,\n"
)


def offers():
    return only_service_type(parse_offers_csv(CSV, "electric"), "residential")


class MarketTests(unittest.TestCase):
    def test_csv_parsed_and_other_service_types_dropped(self):
        got = offers()
        self.assertEqual([o.supplier for o in got], ["Alpha Energy LLC", "Beta Power"])
        self.assertEqual(got[1].get_numeric_price(), 0.09)
        self.assertEqual(got[1].get_numeric_monthly_fee(), 10.0)
        self.assertTrue(got[1].has_cancellation_fee())
        self.assertEqual(got[1].renewable_tier(), "100%")
        self.assertEqual(got[0].renewable_tier(), "none")

    def test_shop_results_url(self):
        got = parse_electric_shop_results_url(
            "https://www.papowerswitch.com/shop-for-rates-results/?zipcode=15000"
            "&serviceType=residential&distributor=1180&distributorrate=RS+-+Regular+Residential+Service"
        )
        self.assertEqual(
            got,
            {"zipcode": "15000", "servicetype": "residential", "id": "1180",
             "ratetype": "RS - Regular Residential Service"},
        )


class RankingTests(unittest.TestCase):
    def test_monthly_fee_changes_the_order(self):
        # 10,000 kWh: Alpha 1000; Beta 900 + 120 fee = 1020
        ranked = rank_plans_by_annual(offers(), 10_000, "kWh")
        self.assertEqual([(o.supplier, o.annual_supply_usd) for o in ranked],
                         [("Alpha Energy LLC", 1000.0), ("Beta Power", 1020.0)])
        # 20,000 kWh: Alpha 2000; Beta 1800 + 120 = 1920
        self.assertEqual(rank_plans_by_annual(offers(), 20_000, "kWh")[0].supplier, "Beta Power")

    def test_term_parsing(self):
        self.assertEqual(parse_term_months("12"), 12)
        self.assertEqual(parse_term_months("2 years"), 24)
        self.assertEqual(parse_term_months("Month to month"), 1)
        self.assertIsNone(parse_term_months("Default"))

    def test_switch_only_above_threshold(self):
        ranked = rank_plans_by_annual(offers(), 10_000, "kWh")
        stay = RankedOption(supplier="Now", plan_type="Current", unit="kWh",
                            annual_supply_usd=1040.0, unit_price=0.104, is_current_egs=True)
        rec, switch, pct, saved = pick_recommended(ranked + [stay], stay, 5.0)
        self.assertFalse(switch)  # 40 / 1040 = 3.8%
        self.assertIs(rec, stay)
        rec, switch, pct, saved = pick_recommended(ranked + [stay], stay, 3.0)
        self.assertTrue(switch)
        self.assertEqual((rec.supplier, saved), ("Alpha Energy LLC", 40.0))

    def test_best_by_category(self):
        rows = dict(best_by_category(rank_plans_by_annual(offers(), 10_000, "kWh"), "electric"))
        self.assertEqual(rows["Best overall"].supplier, "Alpha Energy LLC")
        self.assertEqual(rows["Best new-customer offer"].supplier, "Beta Power")
        self.assertEqual(rows["Best 100% renewable"].supplier, "Beta Power")
        self.assertNotIn("Best variable", rows)


class EligibilityTests(unittest.TestCase):
    def test_new_customer_offer_closed_to_past_customers(self):
        beta = offers()[1]
        today = date(2026, 11, 1)
        self.assertIsNone(ineligible_reason(beta, [], today))
        used = prior_suppliers(configured="Beta Power LLC@2025-06-30; Someone Else")
        self.assertEqual([p.name for p in used], ["Beta Power LLC", "Someone Else"])
        self.assertIn("new customers only", ineligible_reason(beta, used, today))
        # an offer open to everyone is never ruled out
        self.assertIsNone(ineligible_reason(offers()[0], parse_prior_suppliers("Alpha Energy"), today))

    def test_bill_history_and_current_supplier(self):
        used = prior_suppliers([{"supplier": "Beta Power", "last_bill": "2026-01-15"}],
                               current_supplier="Alpha Energy")
        self.assertEqual([(p.name, p.source) for p in used],
                         [("Beta Power", "bill"), ("Alpha Energy", "current bill")])


class GasAndTouTests(unittest.TestCase):
    def test_unit_conversion_takes_heat_content(self):
        self.assertEqual(gas.therms_per_gas_unit("mcf"), 10.37)
        self.assertEqual(gas.therms_per_gas_unit("ccf", 10.0), 1.0)
        self.assertEqual(gas.therms_per_gas_unit("therm", 10.0), 1.0)
        self.assertEqual(gas.mcf_to_therms(2, 10.0), 20.0)
        with self.assertRaises(ValueError):
            gas.therms_per_gas_unit("gallon")

    def test_tou_periods(self):
        self.assertEqual(classify_tou_period(datetime(2026, 7, 8, 16)), "peak")  # Wed 4 PM
        self.assertEqual(classify_tou_period(datetime(2026, 7, 11, 16)), "off_peak")  # Sat 4 PM
        self.assertEqual(classify_tou_period(datetime(2026, 7, 8, 2)), "super_off_peak")
        self.assertEqual(classify_tou_period(datetime(2026, 9, 7, 16)), "off_peak")  # Labor Day


class AlertTests(unittest.TestCase):
    def test_rate_jump(self):
        hist = [
            {"rate": 0.10, "supplier": "A", "bill_date": "2026-09-01", "bill_id": "1"},
            {"rate": 0.12, "supplier": "A", "bill_date": "2026-10-01", "bill_id": "2"},
        ]
        msg, bill_id = own_rate_alert(hist, 10.0)
        self.assertEqual(bill_id, "2")
        self.assertIn("rose 20%", msg)
        self.assertIsNone(own_rate_alert(hist, 25.0))


if __name__ == "__main__":
    unittest.main()

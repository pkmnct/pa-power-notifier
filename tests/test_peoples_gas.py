"""Offline tests for energy_shopper_core.utilities.peoples_gas. Synthetic responses only."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from energy_shopper_core.utilities import peoples_gas as pg


class FakeClient:
    def fetch_current_usage(self):
        rows = [
            {"MeterReadType": "01", "UsageValue": "5.2", "UsageDate": "1/30/2026", "Month": 1,
             "Year": 2026, "FromDate": "12/30/2025", "ToDate": "1/30/2026"},
            {"MeterReadType": "03", "UsageValue": "9.9", "Month": 2, "Year": 2026},  # estimate: dropped
            {"MeterReadType": "01", "UsageValue": None, "Month": 3, "Year": 2026},  # no value: dropped
        ]
        return {"d": json.dumps({"objUsageGenerationResultSetTwo": rows})}

    def fetch_compare_spending(self):
        rows = [
            {"Consumed": "4.0", "MOD": 1, "YOD": 2026, "MeterReadDate": "1/29/2026"},  # dup of current
            {"Consumed": "6.1", "MOD": 12, "YOD": 2025, "MeterReadDate": "12/30/2025"},
            {"Consumed": "0", "MOD": 7, "YOD": 2025},
        ]
        return {"d": json.dumps({"GetCompareMePrevious": rows})}


BILLS = {"d": json.dumps([
    {"Type": "Bill", "Amount": "$61.20", "Date": "01/31/2026", "InvoiceNumber": "111", "DBDate": "2026-01-31T00:00:00"},
    {"Type": "Payment", "Amount": "$50.00", "Date": "01/15/2026", "InvoiceNumber": "x"},
    {"Type": "Bill", "Amount": "$70.00", "Date": "12/31/2025", "InvoiceNumber": ""},
])}

# What pdfplumber extracts from a People's bill: most spaces dropped.
BILL_TEXT = """Commodity Charge$3.5000perMCF 4.90
YourPRICETOCOMPAREis$3.50perMCF.
CapacityCharge$1.8278perMCF 2.56
MCFUsedin 30Days 1.4
Customer Charge-1Month $16.80
TotalCurrent Charges $1,040.00"""


class PeoplesGasTests(unittest.TestCase):
    def test_usage_merges_both_sources_and_keeps_actual_reads(self):
        usage = pg.fetch_all_usage(FakeClient())
        self.assertEqual([(u.year, u.month, u.usage_mcf) for u in usage], [(2025, 12, 6.1), (2026, 1, 5.2)])

    def test_billing_history_and_cross_reference(self):
        bills = pg.parse_billing_history(BILLS)
        self.assertEqual([(b["invoice"], b["amount"]) for b in bills], [("111", 61.2), ("", 70.0)])
        self.assertEqual([r["InvoiceNumber"] for r in pg.bill_rows(BILLS)], ["111"])
        usage = pg.cross_reference(pg.fetch_all_usage(FakeClient()), bills)
        jan = usage[-1]
        self.assertEqual(jan.bill_amount, 61.2)
        self.assertAlmostEqual(jan.effective_rate, 61.2 / 5.2)

    def test_analysis(self):
        usage = pg.cross_reference(pg.fetch_all_usage(FakeClient()), pg.parse_billing_history(BILLS))
        a = pg.analyze(usage)
        self.assertEqual(a.months_analyzed, 2)
        self.assertAlmostEqual(a.total_usage_mcf, 11.3)
        self.assertEqual(a.peak_monthly_usage_mcf, 6.1)
        with self.assertRaises(ValueError):
            pg.analyze([])

    def test_bill_pdf_text(self):
        got = pg.parse_bill_pdf_text(BILL_TEXT)
        self.assertEqual(got["supplier"], "")
        self.assertEqual(got["price_to_compare_per_mcf"], 3.5)
        self.assertEqual(got["commodity_per_mcf"], 3.5)
        self.assertEqual(got["capacity_per_mcf"], 1.8278)
        self.assertEqual((got["mcf_used"], got["days"]), (1.4, 30.0))
        self.assertEqual(got["customer_charge_usd"], 16.8)
        self.assertEqual(got["total_current_charges_usd"], 1040.0)

    def test_login_needs_credentials(self):
        with self.assertRaises(ValueError):
            pg.PeoplesGasClient("", "").login()


if __name__ == "__main__":
    unittest.main()

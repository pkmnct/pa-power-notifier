"""Offline tests for energy_shopper_core.utilities.duquesne. Synthetic responses only."""

import base64
import json
import os
import sys
import unittest
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from energy_shopper_core.utilities import duquesne as dq


def _read(start, end, kwh, cost):
    return {"timeInterval": f"{start}/{end}", "measuredAmount": {"value": kwh}, "monetaryAmount": {"value": cost}}


DAILY = {"data": {"billingAccountByAuthContext": {"serviceAgreements": [{
    "ratePlan": {"titleShort": "RS", "code": "RS-1"},
    "servicePointsConnection": {"edges": [{"node": {"readStreams": {
        "rates": [{"rateType": "FLAT", "costPerUnit": {"value": 0.25, "currency": "USD"},
                   "activationDate": "2026-01-01", "inactivationDate": "",
                   "timeOfUsePeriod": {"label": "All day"}}],
        "netUsage": [{"reads": [
            _read("2026-10-01T00:00:00-04:00", "2026-10-02T00:00:00-04:00", 20.0, 5.0),
            _read("2026-10-02T00:00:00-04:00", "2026-10-03T00:00:00-04:00", 30.0, 9.0),
        ]}],
        "demand": [{"reads": [{"isMaximum": False, "measuredAmount": {"value": 1.0}},
                              {"isMaximum": True, "measuredAmount": {"value": 4.5}}]}],
    }}}]},
}]}}}

HOURLY = {"data": {"billingAccountByAuthContext": {"serviceAgreements": [{
    "servicePointsConnection": {"edges": [{"node": {"readStreams": {"netUsage": [{"reads": [
        _read("2026-10-01T00:00:00-04:00", "2026-10-01T01:00:00-04:00", 0.5, 0.12),
        _read("2026-10-01T01:00:00-04:00", "2026-10-01T02:00:00-04:00", 0.4, None),
        {"timeInterval": "", "measuredAmount": {"value": 1}},  # no interval: dropped
    ]}]}}}]},
}]}}}

# What pdfplumber extracts from a Duquesne bill (amounts invented).
BILL_TEXT = """Bill ID: 100000000000 Date Prepared: 08/27/2026
DLC Rate RS-Residential Service
Supply Charges - Example Energy of PA LLC $108.05
ENERGY CHARGE 900.40 KH@ 0.120000 $108.05
Price to Compare $0.1414 / kWh"""

# A bill spanning a switch from default service, letter-spaced as on real bills.
SWITCH_BILL_TEXT = """Date Prepared: 1/5/2026
Supply Charges - Duquesne Li g h t 11/24 - 12/21 $94.97
Supply Charges - Example Energy of PA LLC 12/22 - 12/30 $12.00
ENERGY CHARGE 100 KH@ 12.0000 $12.00"""

DEFAULT_SERVICE_TEXT = "Supply Charges - Duquesne Light $80.00\nENERGY CHARGE 500 KH@ 0.14 $70.00"


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


class FakeResponse:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload
        self.headers = {}
        self.url = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._payload


class FakeSession(requests.Session):
    """Routes by URL; records GraphQL calls."""

    def __init__(self, login_status=200, page_html="", graphql=None):
        super().__init__()
        self.login_status = login_status
        self.page_html = page_html
        self.graphql = graphql or (lambda payload: {})
        self.graphql_calls = []

    def post(self, url, json=None, **kw):
        if url == dq.LOGIN_URL:
            return FakeResponse(self.login_status)
        self.graphql_calls.append(json)
        return FakeResponse(payload=self.graphql(json))

    def get(self, url, **kw):
        if url == dq.ELECTRIC_USE_URL:
            return FakeResponse(text=self.page_html)
        if url == dq.OPOWER_CUSTOMERS_URL:
            return FakeResponse(payload={"uuid": "c-1", "utilityAccounts": [{"uuid": "ua-1"}]})
        return FakeResponse()


TOKEN_PAGE = '<script>app.opower.init({modelJson: {"OPowerToken": "%s"}})</script>' % _jwt(4102444800)
BOT_WALL = '<html><script src="/_Incapsula_Resource?x=1"></script></html>'


class ParsingTests(unittest.TestCase):
    def test_daily_response(self):
        out = dq.analyze_response(DAILY)
        self.assertEqual(out["rate_plan_label"], "RS")
        self.assertEqual(out["peak_demand"], 4.5)
        self.assertEqual([(r.rate_type, r.cost_per_unit, r.time_of_use_period) for r in out["rates"]],
                         [("FLAT", 0.25, "All day")])
        self.assertEqual([(u.date, u.kwh) for u in out["usage_data"]], [("2026-10-01", 20.0), ("2026-10-02", 30.0)])
        ua = out["usage_analysis"]
        self.assertEqual((ua.total_kwh, ua.total_cost, ua.days_analyzed, ua.date_range),
                         (50.0, 14.0, 2, ("2026-10-01", "2026-10-02")))
        self.assertAlmostEqual(out["current_effective_rate"], 0.28)
        self.assertEqual(dq.analyze_response({}), {})

    def test_projected_cost(self):
        ua = dq.analyze_usage(dq.parse_usage(DAILY))
        p = dq.calculate_projected_cost(ua, 0.2)
        self.assertAlmostEqual(p["annual_kwh"], 25 * 365)
        self.assertAlmostEqual(p["annual_savings"], 7 * 365 - 25 * 365 * 0.2)

    def test_hourly(self):
        recs = dq.parse_hourly(HOURLY)
        self.assertEqual([(r.dt, r.kwh, r.cost) for r in recs],
                         [("2026-10-01T00:00:00-04:00", 0.5, 0.12), ("2026-10-01T01:00:00-04:00", 0.4, 0.0)])

    def test_bill_egs(self):
        egs = dq.parse_egs_from_bill_text(BILL_TEXT, bill_id="b1")
        self.assertEqual(egs.supplier, "Example Energy of PA LLC")
        self.assertEqual((egs.supply_rate_per_kwh, egs.price_to_compare, egs.kwh_on_bill, egs.supply_charges_usd),
                         (0.12, 0.1414, 900.40, 108.05))
        self.assertEqual((egs.rate_schedule, egs.bill_id, egs.bill_date),
                         ("RS-Residential Service", "b1", "2026-08-27"))

    def test_bill_spanning_switch_and_cents(self):
        egs = dq.parse_egs_from_bill_text(SWITCH_BILL_TEXT)
        self.assertEqual(egs.supplier, "Example Energy of PA LLC")
        self.assertAlmostEqual(egs.supply_rate_per_kwh, 0.12)  # ¢/kWh converted
        self.assertEqual(egs.bill_date, "2026-01-05")

    def test_default_service_bill_has_no_egs(self):
        self.assertIsNone(dq.parse_egs_from_bill_text(DEFAULT_SERVICE_TEXT))
        self.assertEqual(dq.parse_bill_meta(DEFAULT_SERVICE_TEXT), {"bill_date": ""})
        self.assertIsNone(dq.parse_egs_from_bill_text(""))

    def test_token_and_bot_wall(self):
        token, exp = dq.extract_opower_token(TOKEN_PAGE)
        self.assertEqual(exp, 4102444800.0)
        self.assertTrue(token.startswith("h."))
        self.assertIsNone(dq.extract_opower_token(BOT_WALL))
        self.assertTrue(dq.html_looks_like_bot_challenge(BOT_WALL))
        self.assertFalse(dq.html_looks_like_bot_challenge(TOKEN_PAGE + "x" * 1000))


class ClientTests(unittest.TestCase):
    def test_missing_credentials(self):
        with self.assertRaises(dq.DuquesneAuthError):
            dq.DuquesneClient("", "").ensure_authenticated()

    def test_rejected_login(self):
        client = dq.DuquesneClient("u", "p", session=FakeSession(login_status=401))
        with self.assertRaises(dq.DuquesneAuthError):
            client.ensure_authenticated()

    def test_server_error_is_not_an_auth_error(self):
        client = dq.DuquesneClient("u", "p", session=FakeSession(login_status=503))
        with self.assertRaises(dq.DuquesneError) as cm:
            client.login()
        self.assertNotIsInstance(cm.exception, dq.DuquesneAuthError)

    def test_bot_wall_without_browser_is_blocked(self):
        client = dq.DuquesneClient("u", "p", session=FakeSession(page_html=BOT_WALL))
        with self.assertRaises(dq.DuquesneBlockedError):
            client.ensure_authenticated()

    def test_bot_wall_uses_injected_browser_then_discovers_account(self):
        calls = []

        def browser(url, session):
            calls.append(url)
            return TOKEN_PAGE

        client = dq.DuquesneClient("u", "p", html_fetcher=browser, session=FakeSession(page_html=BOT_WALL))
        client.ensure_authenticated()
        self.assertEqual(calls, [dq.ELECTRIC_USE_URL])
        self.assertTrue(client.token_valid())
        self.assertEqual((client.customer_urn, client.utility_account_uuid), ("urn:opower:customer:uuid:c-1", "ua-1"))
        self.assertIn("Bearer h.", client.opower_headers()["Authorization"])

    def test_cached_token_skips_login(self):
        session = FakeSession(login_status=401, graphql=lambda p: DAILY)
        client = dq.DuquesneClient("u", "p", customer_uuid="c", utility_account_uuid="ua",
                                   opower_token="t", token_expires_at=4102444800, session=session)
        data = client.fetch_daily_response(now=datetime(2026, 11, 1))
        self.assertEqual(data, DAILY)
        variables = session.graphql_calls[0]["variables"]
        self.assertEqual(variables["timeInterval"], "2026-09-12T00:00:00-04:00/2026-11-01T00:00:00-04:00")
        self.assertEqual(variables["customerURN"], "urn:opower:customer:uuid:c")

    def test_hourly_history_pages_back_and_stops_after_empty_windows(self):
        def graphql(payload):
            start = payload["variables"]["timeInterval"][:10]
            return HOURLY if start >= "2026-09-01" else {}

        session = FakeSession(graphql=graphql)
        client = dq.DuquesneClient("u", "p", customer_uuid="c", utility_account_uuid="ua",
                                   opower_token="t", token_expires_at=4102444800, session=session)
        recs = client.fetch_hourly_history(lookback_days=400, now=datetime(2026, 11, 15))
        self.assertEqual(len(recs), 2)  # windows overlap on the same hours: deduplicated
        # 10-17→11-15 and 09-18→10-17 have data; the next two windows are empty, so it stops at 4
        self.assertEqual(len(session.graphql_calls), 4)


    def test_hourly_history_pages_past_failed_windows(self):
        def graphql(payload):
            start = payload["variables"]["timeInterval"][:10]
            if "2026-08-20" <= start < "2026-10-17":
                raise requests.ConnectionError("timeout")  # two windows fail
            return HOURLY if start >= "2026-07-01" else {}

        session = FakeSession(graphql=graphql)
        client = dq.DuquesneClient("u", "p", customer_uuid="c", utility_account_uuid="ua",
                                   opower_token="t", token_expires_at=4102444800, session=session)
        client.fetch_hourly_history(lookback_days=400, now=datetime(2026, 11, 15))
        self.assertEqual(client.last_hourly_errors, 2)
        # Failures aren't the end of the history: it reached the July window and then
        # stopped on two genuinely empty ones (not after the two failures, at call 3).
        starts = [c["variables"]["timeInterval"][:10] for c in session.graphql_calls]
        self.assertIn("2026-07-22", starts)
        self.assertEqual(len(starts), 6)

    def test_hourly_window_error_payload_is_a_failure(self):
        session = FakeSession(graphql=lambda p: {"errors": [{"message": "unauthorized"}]})
        client = dq.DuquesneClient("u", "p", customer_uuid="c", utility_account_uuid="ua",
                                   opower_token="t", token_expires_at=4102444800, session=session)
        self.assertIsNone(client.fetch_hourly_window(datetime(2026, 11, 1), datetime(2026, 11, 2)))
        self.assertEqual(client.fetch_hourly(1, datetime(2026, 11, 2)), [])

if __name__ == "__main__":
    unittest.main()

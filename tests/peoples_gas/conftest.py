"""Fixtures for the peoples_gas tests: a fake portal in place of PeoplesGasClient."""

from __future__ import annotations

from collections.abc import Generator
import json
from unittest.mock import patch

import pytest

from custom_components.peoples_gas.const import DOMAIN
from custom_components.peoples_gas.vendor.energy_shopper_core.utilities import (
    peoples_gas as pg,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from pytest_homeassistant_custom_component.common import MockConfigEntry

ACCOUNT = "1234567890"
USERNAME = "user@example.com"
PASSWORD = "secret"

# What pdfplumber extracts from a Peoples bill (spaces mostly dropped).
BILL_TEXT = """Commodity Charge$3.6389perMCF 6.55
YourPRICETOCOMPAREis$3.57perMCF.
CapacityCharge$1.8278perMCF 3.29
MCFUsedin 29Days 1.8
Customer Charge-1Month $16.80
TotalCurrent Charges $39.24"""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The integration depends on recorder, which must start before hass is set up."""
    yield


class FakePortal:
    """Mutable portal state; every FakeClient made while patched reads it."""

    def __init__(self) -> None:
        self.password = PASSWORD
        self.account = ACCOUNT
        self.login_error: Exception | None = None
        # (month, year, mcf) cycles on the usage page, dated the 1st of the billing month
        self.current = [(m, 2026, mcf) for m, mcf in [(1, 9.0), (2, 8.0), (3, 6.0)]]
        self.previous = [(12, 2025, 7.0), (11, 2025, 4.0)]
        self.bills = [  # (date, amount, invoice)
            ("2025-11-18", 52.10, "800000000011"),
            ("2025-12-17", 80.00, "800000000012"),
            ("2026-01-16", 98.40, "800000000001"),
            ("2026-02-17", 90.15, "800000000002"),
            ("2026-03-18", 70.25, "800000000003"),
        ]
        self.balance = {"ToTalBalance": "193.7600", "BillDue": "04/12/2026"}
        self.pdf_downloads: list[str] = []
        self.logins = 0

    def add_cycle(self, month: int, year: int, mcf: float, bill_date: str, amount: float, invoice: str):
        self.current.append((month, year, mcf))
        self.bills.append((bill_date, amount, invoice))


class FakeClient:
    def __init__(self, portal: FakePortal, username: str, password: str) -> None:
        self.portal, self.username, self.password = portal, username, password

    def login(self) -> bool:
        self.portal.logins += 1
        if self.portal.login_error is not None:
            raise self.portal.login_error
        if self.password != self.portal.password:
            raise pg.PeoplesGasAuthError("Login failed: Invalid username or password")
        return True

    def _account_number(self) -> str:
        return self.portal.account

    def fetch_current_usage(self) -> dict:
        rows = [
            {
                "MeterReadType": "01",
                "UsageValue": str(mcf),
                "UsageDate": f"{m}/1/{y}",
                "Month": m,
                "Year": y,
                "FromDate": f"{m - 1 or 12}/1/{y if m > 1 else y - 1}",
                "ToDate": f"{m}/1/{y}",
            }
            for m, y, mcf in self.portal.current
        ]
        rows.append({"MeterReadType": "03", "UsageValue": "99", "Month": 4, "Year": 2026})
        return {"d": json.dumps({"objUsageGenerationResultSetTwo": rows})}

    def fetch_compare_spending(self) -> dict:
        rows = [
            {"Consumed": str(mcf), "MOD": m, "YOD": y, "MeterReadDate": f"{m}/1/{y}"}
            for m, y, mcf in self.portal.previous
        ]
        return {"d": json.dumps({"GetCompareMePrevious": rows})}

    def fetch_billing_history(self, from_date: str = "", to_date: str = "") -> dict:
        rows = []
        for d, amount, invoice in self.portal.bills:
            y, m, day = d.split("-")
            rows.append(
                {
                    "Type": "Bill",
                    "Date": f"{m}/{day}/{y}",
                    "DBDate": f"{d}T00:00:00",
                    "Amount": f"${amount:,.2f}",
                    "InvoiceNumber": invoice,
                    "AccountNumber": "0",
                }
            )
        rows.append({"Type": "Payment", "Date": "03/20/2026", "Amount": "$70.25", "InvoiceNumber": ""})
        return {"d": json.dumps(rows)}

    def fetch_current_balance(self) -> dict:
        return {"d": {"__type": "CustomerPortal.BillDashboard+Bill", **self.portal.balance}}

    def fetch_bill_pdf(self, row: dict) -> bytes:
        self.portal.pdf_downloads.append(row["InvoiceNumber"])
        return b"%PDF-1.4 fake " + row["InvoiceNumber"].encode()


@pytest.fixture
def portal() -> Generator[FakePortal]:
    portal = FakePortal()
    with (
        patch.object(pg, "PeoplesGasClient", lambda u, p, session=None: FakeClient(portal, u, p)),
        patch.object(pg, "pdf_text", lambda source: BILL_TEXT),
    ):
        yield portal


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Peoples Gas {ACCOUNT}",
        unique_id=ACCOUNT,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )

"""
People's Gas (Peoples Natural Gas, Pennsylvania) account client.

Authenticates against the People's Gas e-Account portal (peopleseaccount.com)
and fetches:
  - monthly usage per billing cycle (Mcf)
  - bill amounts, and the effective $/Mcf per cycle
  - bill PDFs, with Price to Compare and commodity rate parsed from them

Plain HTTP (requests); no browser. Credentials are passed in; nothing here reads the
environment or writes files.

Authentication flow (discovered via browser DevTools):
  1. GET  /Portal/default.aspx              → parse hdnCSRFToken hidden input
  2. POST /Portal/Default.aspx/validateLogin → obtain session cookies
  3. All subsequent POSTs need csrftoken header + session cookies

Key endpoints:
  POST /Portal/Usages.aspx/LoadGasUsage              → trailing 12 months of usage (MCF)
  POST /Portal/Compare-Spending.aspx/LoadData         → current + previous year usage
  POST /Portal/BillingHistory.aspx/LoadW2UIGridData   → bill amounts with date filter
  POST /Portal/BillDashboard.aspx/LoadBilling         → current balance + due date
  POST /Portal/Common.aspx/GetEncryptedData + GET /Portal/BillPDF.aspx → bill PDF

Notes:
  - usageyear param on LoadGasUsage is ignored by the server (always returns last 12 months)
  - Usage units are MCF (thousand cubic feet)
  - Billing history goes back to account open date (fromDate/toDate supported)
  - Bills are matched to usage periods by: month(bill_date) == month(usage_to_date)
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://peopleseaccount.com/Portal"

METER_READ_TYPES = {
    "01": "Actual Reading",
    "02": "Customer Reading",
    "03": "Estimated Reading",
}


def _normalize_aspnet_d(value) -> dict:
    """ASP.NET 'd' may be a JSON object, a one-element list, or a JSON string of either."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], dict):
            return value[0]
        return {}
    if isinstance(value, dict):
        return value
    return {}


@dataclass
class GasUsageData:
    """Monthly gas usage for one billing cycle"""
    date: str            # UsageDate (end of cycle, e.g. "5/1/2025")
    usage_mcf: float     # Usage in MCF
    month: int
    year: int
    from_date: str
    to_date: str
    meter_read_type: str  # "01" Actual, "02" Customer, "03" Estimated
    bill_amount: Optional[float] = None    # $ from billing history
    effective_rate: Optional[float] = None  # $/MCF = bill_amount / usage_mcf


@dataclass
class GasUsageAnalysis:
    """Summary statistics over the analyzed period"""
    average_monthly_usage_mcf: float
    total_usage_mcf: float
    months_analyzed: int
    date_range: Tuple[str, str]
    peak_monthly_usage_mcf: Optional[float] = None
    average_monthly_cost: Optional[float] = None
    average_effective_rate_per_mcf: Optional[float] = None


# ---------------------------------------------------------------------------
# Authentication & HTTP client
# ---------------------------------------------------------------------------

class PeoplesGasAuthError(RuntimeError):
    """The portal rejected the username or password."""


class PeoplesGasClient:
    """Authentication and API calls against peopleseaccount.com."""

    def __init__(
        self,
        username: str,
        password: str,
        session: Optional[requests.Session] = None,
    ):
        self.username = username or ""
        self.password = password or ""
        self.session = session or requests.Session()
        self.csrf_token: Optional[str] = None
        self._logged_in = False
        self._account_no: Optional[str] = None

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """
        Authenticate and store session cookies.
        Returns True on success.
        """
        if not self.username or not self.password:
            raise ValueError("People's Gas username and password are required.")

        # Step 1: GET login page to obtain CSRF token
        resp = self.session.get(
            f"{BASE_URL}/default.aspx",
            headers={"User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        csrf_input = soup.find("input", {"id": "hdnCSRFToken"})
        if not csrf_input or not csrf_input.get("value"):
            raise RuntimeError("Could not find hdnCSRFToken on login page")
        self.csrf_token = csrf_input["value"]
        logger.debug("CSRF token obtained")

        # Step 2: POST credentials
        payload = {
            "username": self.username,
            "password": self.password,
            "rememberme": False,
            "calledFrom": "LN",
            "ExternalLoginId": "",
            "LoginMode": "1",
        }
        resp = self.session.post(
            f"{BASE_URL}/Default.aspx/validateLogin",
            json=payload,
            headers=self._api_headers(referer=f"{BASE_URL}/default.aspx"),
            timeout=30,
        )
        resp.raise_for_status()

        data = resp.json()
        inner = _normalize_aspnet_d(data.get("d"))

        if inner.get("IsAuthenticated") is False or inner.get("LoginStatus") == "F":
            raise PeoplesGasAuthError(f"Login failed: {inner.get('Message', 'unknown error')}")

        self._logged_in = True
        logger.info("People's Gas login successful")
        return True

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _api_headers(self, referer: Optional[str] = None) -> Dict:
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://peopleseaccount.com",
        }
        if self.csrf_token:
            headers["csrftoken"] = self.csrf_token
        if referer:
            headers["Referer"] = referer
        return headers

    def _post(self, path: str, payload: dict, referer_path: str = "/Portal/Dashboard.aspx") -> dict:
        self._ensure_logged_in()
        resp = self.session.post(
            f"https://peopleseaccount.com{path}",
            json=payload,
            headers=self._api_headers(referer=f"https://peopleseaccount.com{referer_path}"),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _ensure_logged_in(self):
        if not self._logged_in:
            self.login()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def fetch_current_usage(self) -> dict:
        """
        Fetch trailing 12 months of usage data.
        Endpoint: POST /Portal/Usages.aspx/LoadGasUsage
        Note: usageyear param is ignored by the server.
        """
        payload = {
            "Type": "C",
            "Mode": "M",
            "strDate": "",
            "hourlyType": "H",
            "seasonId": "",
            "weatherOverlay": 0,
            "usageyear": "",
            "MeterNumber": None,
            "DateFromDaily": "",
            "DateToDaily": "",
        }
        return self._post("/Portal/Usages.aspx/LoadGasUsage", payload, "/Portal/Usages.aspx")

    def fetch_compare_spending(self) -> dict:
        """
        Fetch current year + previous year usage for comparison.
        Endpoint: POST /Portal/Compare-Spending.aspx/LoadData
        Returns GetCompareMeCurrent (last 12 mo) + GetCompareMePrevious (prior 12 mo).
        """
        return self._post(
            "/Portal/Compare-Spending.aspx/LoadData",
            {"type": "K", "strComparetype": ""},
            "/Portal/compare-spending.aspx",
        )

    def fetch_billing_history(self, from_date: str = "", to_date: str = "") -> dict:
        """
        Fetch bill history.
        Endpoint: POST /Portal/BillingHistory.aspx/LoadW2UIGridData
        Args:
            from_date: "MM/DD/YYYY" or "" for all
            to_date:   "MM/DD/YYYY" or "" for all
        """
        return self._post(
            "/Portal/BillingHistory.aspx/LoadW2UIGridData",
            {"tabType": "menu1", "fromDate": from_date, "toDate": to_date},
            "/Portal/BillingHistory.aspx",
        )

    def fetch_current_balance(self) -> dict:
        """
        Fetch current balance and due date.
        Endpoint: POST /Portal/BillDashboard.aspx/LoadBilling
        """
        return self._post("/Portal/BillDashboard.aspx/LoadBilling", {}, "/Portal/BillDashboard.aspx")

    def _account_number(self) -> Optional[str]:
        # The billing grid reports AccountNumber "0"; BillPDF needs the real one, which the
        # bill dashboard keeps in a hidden input.
        if self._account_no is None:
            self._ensure_logged_in()
            html = self.session.get(f"{BASE_URL}/BillDashboard.aspx", timeout=30).text
            m = re.search(r'id="hdnAccountNumber" value="([^"]*)"', html)
            self._account_no = m.group(1) if m else ""
        return self._account_no or None

    def fetch_bill_pdf(self, bill_row: dict) -> Optional[bytes]:
        """
        Download one bill's PDF. bill_row is a raw LoadW2UIGridData row (DBDate, InvoiceNumber).
        Mirrors BillingHistory.js BillPdfClick: encrypt the query server-side, then GET BillPDF.aspx.
        """
        account = self._account_number()
        if not account:
            logger.warning("People's Gas account number not found on bill dashboard")
            return None
        query = (
            f"AccountNo={account}&BillingDate={bill_row.get('DBDate', '')}"
            f"&InvoiceNumber={bill_row.get('InvoiceNumber', '')}&ctype=inline"
        )
        enc = self._post(
            "/Portal/Common.aspx/GetEncryptedData", {"data": query}, "/Portal/BillingHistory.aspx"
        ).get("d")
        if not enc:
            return None
        # Passed unencoded, as the portal's JS does; URL-encoding it makes BillPDF.aspx 500.
        resp = self.session.get(f"{BASE_URL}/BillPDF.aspx?EncQuery={enc}&EncType=A", timeout=60)
        if resp.content[:5] != b"%PDF-":
            logger.warning(
                "People's Gas bill %s did not return a PDF (%s)",
                bill_row.get("InvoiceNumber"),
                resp.headers.get("content-type"),
            )
            return None
        return resp.content



_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def pdf_text(source: Union[str, bytes]) -> str:
    """Text of a bill PDF, given its path or its bytes. "" if it can't be read (needs pdfplumber)."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(source) if isinstance(source, bytes) else source) as doc:
            return "\n".join((p.extract_text() or "") for p in doc.pages)
    except Exception as e:
        logger.warning("Could not read People's Gas bill PDF: %s", e)
        return ""


def parse_amount(value) -> Optional[float]:
    m = re.search(r"-?[\d,]+\.?\d*", str(value or ""))
    return float(m.group(0).replace(",", "")) if m else None


def parse_bill_pdf_text(text: str) -> Dict:
    """
    Rates and usage from a People's Gas bill PDF. Its text extraction drops most spaces:

      Commodity Charge$3.5000perMCF 4.90
      YourPRICETOCOMPAREis$3.50perMCF.
      MCFUsedin 30Days 1.4
      Customer Charge-1Month $16.80
      TotalCurrent Charges $40.00

    supplier is "" on People's default service; a Natural Gas Supplier line names it otherwise.
    """
    flat = re.sub(r"\s+", "", text or "")

    def num(pattern: str) -> Optional[float]:
        m = re.search(pattern, flat, re.I)
        return float(m.group(1).replace(",", "")) if m else None

    supplier = ""
    m = re.search(r"Natural\s*Gas\s*Supplier\s*[:\-]\s*([^\n$]+)", text or "", re.I)
    if m and "please" not in m.group(1).lower():
        supplier = m.group(1).strip()
    return {
        "supplier": supplier,
        "price_to_compare_per_mcf": num(r"PRICETOCOMPAREis\$([\d.]+)perMCF"),
        "commodity_per_mcf": num(r"CommodityCharge\$([\d.]+)perMCF"),
        "capacity_per_mcf": num(r"CapacityCharge\$([\d.]+)perMCF"),
        "mcf_used": num(r"MCFUsedin\d+Days([\d.]+)"),
        "days": num(r"MCFUsedin(\d+)Days"),
        "customer_charge_usd": num(r"CustomerCharge-?1Month\$([\d.]+)"),
        "total_current_charges_usd": num(r"TotalCurrentCharges\$([\d,.]+)"),
    }


def parse_usage_response(raw: dict) -> List[GasUsageData]:
    """
    Parse the LoadGasUsage response into GasUsageData objects.
    Only returns MeterReadType "01" (Actual Reading) records with non-null values.
    """
    records = []
    d_str = raw.get("d", "")
    if not d_str:
        return records

    try:
        inner = json.loads(d_str) if isinstance(d_str, str) else d_str
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse usage JSON: %s", exc)
        return records

    for rec in inner.get("objUsageGenerationResultSetTwo", []):
        if rec.get("MeterReadType") != "01":
            continue
        val = rec.get("UsageValue")
        if val is None:
            continue
        try:
            usage_mcf = float(val)
        except (ValueError, TypeError):
            continue
        records.append(
            GasUsageData(
                date=rec.get("UsageDate", ""),
                usage_mcf=usage_mcf,
                month=rec.get("Month", 0),
                year=rec.get("Year", 0),
                from_date=rec.get("FromDate", ""),
                to_date=rec.get("ToDate", ""),
                meter_read_type="01",
            )
        )
    return records


def parse_compare_spending_previous(raw: dict) -> List[GasUsageData]:
    """
    Parse the Compare-Spending response's previous-year data into GasUsageData objects.
    These records only have month/year and consumed value (no from/to dates).
    """
    records = []
    d_str = raw.get("d", "")
    if not d_str:
        return records

    try:
        inner = json.loads(d_str) if isinstance(d_str, str) else d_str
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse compare-spending JSON: %s", exc)
        return records

    for rec in inner.get("GetCompareMePrevious", []):
        val = rec.get("Consumed")
        if val is None or val == "0":
            continue
        try:
            usage_mcf = float(val)
        except (ValueError, TypeError):
            continue
        if usage_mcf <= 0:
            continue
        month = rec.get("MOD", 0)
        year = rec.get("YOD", 0)
        meter_date = rec.get("MeterReadDate", "")
        records.append(
            GasUsageData(
                date=meter_date or f"{month}/1/{year}",
                usage_mcf=usage_mcf,
                month=month,
                year=year,
                from_date="",
                to_date=meter_date or f"{month}/1/{year}",
                meter_read_type="01",
            )
        )
    return records


def parse_billing_history(raw: dict) -> List[Dict]:
    """
    Parse billing history into list of dicts with date (datetime), amount (float),
    invoice_number (str).
    """
    bills = []
    d_str = raw.get("d", "")
    if not d_str:
        return bills

    try:
        records = json.loads(d_str) if isinstance(d_str, str) else d_str
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse billing history JSON: %s", exc)
        return bills

    for rec in (records or []):
        if rec.get("Type") != "Bill":
            continue
        try:
            amount = float(rec["Amount"].replace("$", "").replace(",", ""))
            date = datetime.strptime(rec["Date"], "%m/%d/%Y")
        except (KeyError, ValueError):
            continue
        bills.append({
            "date": date,
            "amount": amount,
            "invoice": rec.get("InvoiceNumber", ""),
        })
    return bills


def cross_reference(
    usage_records: List[GasUsageData],
    bills: List[Dict],
) -> List[GasUsageData]:
    """
    Match each usage record to a bill by:
      month(bill.date) == month(usage.to_date)  AND
      year(bill.date)  == year(usage.to_date)

    Populates bill_amount and effective_rate on each matched record.
    """
    bill_index: Dict[Tuple[int, int], float] = {}
    for bill in bills:
        key = (bill["date"].month, bill["date"].year)
        # If multiple bills in same month (unlikely), take the largest
        bill_index[key] = max(bill_index.get(key, 0.0), bill["amount"])

    for rec in usage_records:
        if not rec.to_date:
            continue
        try:
            to_dt = datetime.strptime(rec.to_date, "%m/%d/%Y")
        except ValueError:
            continue
        key = (to_dt.month, to_dt.year)
        amount = bill_index.get(key)
        if amount is not None:
            rec.bill_amount = amount
            if rec.usage_mcf > 0:
                rec.effective_rate = amount / rec.usage_mcf

    return usage_records


def bill_rows(raw: dict) -> List[dict]:
    """Raw LoadW2UIGridData rows of Type "Bill" (what fetch_bill_pdf takes)."""
    d = raw.get("d") if isinstance(raw, dict) else None
    rows = json.loads(d) if isinstance(d, str) else (d or [])
    return [r for r in rows if r.get("Type") == "Bill" and r.get("InvoiceNumber")]


def fetch_all_usage(client: PeoplesGasClient) -> List[GasUsageData]:
    """
    All available usage records, deduplicated and sorted by date. Combines:
      - trailing 12 months from LoadGasUsage
      - previous year from Compare-Spending (extends history ~12 more months)
    """
    current = parse_usage_response(client.fetch_current_usage())
    previous = parse_compare_spending_previous(client.fetch_compare_spending())

    # Deduplicate: current data takes precedence
    current_keys = {(r.month, r.year) for r in current}
    unique_previous = [r for r in previous if (r.month, r.year) not in current_keys]

    all_usage = current + unique_previous
    all_usage.sort(key=lambda r: (r.year, r.month))
    logger.info("Fetched %d usage records total", len(all_usage))
    return all_usage


def analyze(usage: List[GasUsageData]) -> GasUsageAnalysis:
    """Compute summary statistics from a list of usage records."""
    if not usage:
        raise ValueError("No usage data provided")

    valid = [u for u in usage if u.usage_mcf > 0]
    if not valid:
        raise ValueError("No valid usage readings found")

    total = sum(u.usage_mcf for u in valid)
    months = len(valid)
    avg = total / months

    costs = [u.bill_amount for u in valid if u.bill_amount is not None]
    rates = [u.effective_rate for u in valid if u.effective_rate is not None]

    dates = sorted(u.date for u in valid if u.date)

    return GasUsageAnalysis(
        average_monthly_usage_mcf=avg,
        total_usage_mcf=total,
        months_analyzed=months,
        date_range=(dates[0], dates[-1]) if dates else ("", ""),
        peak_monthly_usage_mcf=max(u.usage_mcf for u in valid),
        average_monthly_cost=sum(costs) / len(costs) if costs else None,
        average_effective_rate_per_mcf=sum(rates) / len(rates) if rates else None,
    )


def calculate_projected_cost(analysis: GasUsageAnalysis, rate_per_mcf: float) -> Dict:
    """Project annual **full-bill-style** cost at a given **$/MCF** blended rate."""
    monthly_cost = analysis.average_monthly_usage_mcf * rate_per_mcf
    annual_cost = monthly_cost * 12
    current_monthly = analysis.average_monthly_cost
    current_annual = current_monthly * 12 if current_monthly else None
    monthly_savings = (current_monthly - monthly_cost) if current_monthly else None
    annual_savings = (current_annual - annual_cost) if current_annual else None

    return {
        "rate_per_mcf": rate_per_mcf,
        "monthly_usage_mcf": analysis.average_monthly_usage_mcf,
        "annual_usage_mcf": analysis.average_monthly_usage_mcf * 12,
        "monthly_cost": monthly_cost,
        "annual_cost": annual_cost,
        "current_monthly_cost": current_monthly,
        "current_annual_cost": current_annual,
        "monthly_savings": monthly_savings,
        "annual_savings": annual_savings,
        "monthly_savings_pct": (monthly_savings / current_monthly * 100)
        if current_monthly and monthly_savings is not None
        else None,
        "annual_savings_pct": (annual_savings / current_annual * 100)
        if current_annual and annual_savings is not None
        else None,
    }

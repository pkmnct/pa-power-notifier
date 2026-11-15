"""
Duquesne Light (Pittsburgh) account client: Opower usage and bill-PDF parsing.

Credentials, account identifiers and an optional cached token are passed in; nothing
here reads the environment or touches files. Login or token failures raise
`DuquesneAuthError` (bad or missing credentials) or `DuquesneBlockedError` (the site's
bot wall kept the token out of reach).

www.duquesnelight.com sits behind Imperva Incapsula, which blocks plain HTTP. The one
page that needs a real browser here — the electric-use page that carries the Opower
token — goes through an injected `html_fetcher(url, session) -> html | None`. The
headless-browser code itself (Puppeteer) lives outside this package. Bill PDFs are only
downloadable in the browser too, so that download also stays outside; this module
parses their text and provides the logged-in session the browser needs.

Auth flow (discovered via DevTools):
  1. POST https://auth.duquesnelight.com/oauth/authorize/login  → sets session cookies
  2. GET  https://www.duquesnelight.com/energy-money-savings/my-electric-use
         → server-renders OPowerToken into page HTML (app.opower.init({modelJson: ...}))
  3. Use OPowerToken as  Authorization: Bearer <token>  for all duq.opower.com calls
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple, Union

import requests

from energy_shopper_core.tou import HourlyUsageRecord

logger = logging.getLogger(__name__)

LOGIN_URL = "https://auth.duquesnelight.com/oauth/authorize/login"
ELECTRIC_USE_URL = "https://www.duquesnelight.com/energy-money-savings/my-electric-use"
ACCOUNT_SUMMARY_URL = "https://www.duquesnelight.com/account-billing/account-summary"
OPOWER_CUSTOMERS_URL = "https://duq.opower.com/ei/edge/apis/multi-account-v1/cws/duq/customers/current"
GRAPHQL_URL = "https://duq.opower.com/ei/edge/apis/dsm-graphql-v1/cws/graphql"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# Opower's HOUR-resolution reads endpoint rejects windows over 720 hours
# ("Provided range must be no more than 720 hours") — cap requests at 29 days
# to stay safely under that with DST/timezone rounding.
MAX_HOURLY_DAYS = 29
# How far back to page when backfilling a local hourly archive.
DEFAULT_HOURLY_LOOKBACK_DAYS = 400

# (url, logged-in requests session) -> page HTML, or None on failure
HtmlFetcher = Callable[[str, requests.Session], Optional[str]]


class DuquesneError(RuntimeError):
    """Anything that stopped a Duquesne fetch."""


class DuquesneAuthError(DuquesneError):
    """Missing or rejected credentials."""


class DuquesneBlockedError(DuquesneError):
    """Logged in, but no Opower token came back (bot wall, or no browser fetcher given)."""


@dataclass
class UsageData:
    """Represents daily usage data"""
    date: str
    kwh: float
    cost: float
    effective_rate: float  # cost / kwh


@dataclass
class RateInfo:
    """Represents rate information from Duquesne Light"""
    rate_type: str
    cost_per_unit: float
    currency: str
    activation_date: str
    inactivation_date: str
    tier: Optional[str] = None
    time_of_use_period: Optional[str] = None


@dataclass
class UsageAnalysis:
    """Analysis results of usage patterns"""
    average_daily_kwh: float
    average_daily_cost: float
    average_effective_rate: float
    total_kwh: float
    total_cost: float
    days_analyzed: int
    date_range: Tuple[str, str]
    peak_demand_kw: Optional[float] = None


@dataclass
class CurrentEgsFromBill:
    """
    Current electric generation supplier (EGS) details parsed from a DLC bill PDF.

    This is the supply-only rate that switching suppliers actually changes — not
    the blended all-in Opower effective rate.
    """
    supplier: str
    supply_rate_per_kwh: float
    price_to_compare: Optional[float] = None
    rate_schedule: str = ""
    bill_id: str = ""
    supply_charges_usd: Optional[float] = None
    kwh_on_bill: Optional[float] = None
    source: str = "duquesne_bill_pdf"
    bill_date: str = ""  # YYYY-MM-DD ("Date Prepared")


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

DAILY_QUERY = """
        query WDB_GetCostReadsForDayAndHour($customerURN: ID, $timeInterval: TimeInterval, $resolution: ReadResolution, $forceLegacyData: Boolean, $lastForServicePoints: Int, $aliased: Boolean) {
          billingAccountByAuthContext(
            singlePremise: $customerURN
            forceLegacyData: $forceLegacyData
          ) {
            serviceAgreements(onlyActive: true, aliased: $aliased) {
              uuid
              serviceType
              ratePlan {
                code
                currentRatePlan
                rateTags
                rateAttributes {
                  key
                  value
                  __typename
                }
                titleShort
                descriptionShort
                __typename
              }
              servicePointsConnection(last: $lastForServicePoints) {
                edges {
                  node {
                    serviceType
                    utilityId
                    uuid
                    readStreams(timeInterval: $timeInterval, readResolution: $resolution) {
                      timeInterval
                      rates {
                        rateType
                        tier
                        costPerUnit {
                          value
                          currency
                          __typename
                        }
                        timeOfUsePeriod {
                          label
                          timeOfUse
                          season
                          times
                          __typename
                        }
                        activationDate
                        inactivationDate
                        __typename
                      }
                      netUsage {
                        serviceQuantityIdentifier
                        unit
                        isOpowerDerived
                        registerId
                        reads {
                          readType
                          timeInterval
                          measuredAmount {
                            unit
                            value
                            __typename
                          }
                          monetaryAmount {
                            currency
                            value
                            __typename
                          }
                          rebateAmount {
                            currency
                            value
                            __typename
                          }
                          isPeakPeriod
                          ratedReadComponents {
                            attributes {
                              key
                              value
                              __typename
                            }
                            measuredAmount {
                              value
                              __typename
                            }
                            monetaryAmount {
                              value
                              __typename
                            }
                            __typename
                          }
                          __typename
                        }
                        __typename
                      }
                      demand {
                        serviceQuantityIdentifier
                        unit
                        isOpowerDerived
                        registerId
                        reads {
                          readType
                          timeInterval
                          isMaximum
                          measuredAmount {
                            unit
                            value
                            __typename
                          }
                          __typename
                        }
                        __typename
                      }
                      __typename
                    }
                    __typename
                  }
                  __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """

HOURLY_QUERY = """
        query WDB_GetCostReadsForDayAndHour($customerURN: ID, $timeInterval: TimeInterval, $resolution: ReadResolution, $forceLegacyData: Boolean, $lastForServicePoints: Int, $aliased: Boolean) {
          billingAccountByAuthContext(
            singlePremise: $customerURN
            forceLegacyData: $forceLegacyData
          ) {
            serviceAgreements(onlyActive: true, aliased: $aliased) {
              servicePointsConnection(last: $lastForServicePoints) {
                edges {
                  node {
                    readStreams(timeInterval: $timeInterval, readResolution: $resolution) {
                      netUsage {
                        reads {
                          timeInterval
                          measuredAmount { value }
                          monetaryAmount { value }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """


def time_interval(start: datetime, end: datetime) -> str:
    """Opower TimeInterval for whole days. The fixed -04:00 offset matches the site's own requests."""
    return (
        f"{start.strftime('%Y-%m-%dT00:00:00-04:00')}/"
        f"{end.strftime('%Y-%m-%dT00:00:00-04:00')}"
    )


def html_looks_like_bot_challenge(html: str) -> bool:
    """Heuristic for CDN bot walls (Imperva Incapsula returns tiny placeholder HTML)."""
    if not html:
        return True
    if "_Incapsula_Resource" in html or "Incapsula incident ID" in html:
        return True
    if len(html) < 800 and ("Incapsula" in html or "incident id" in html.lower()):
        return True
    return False


def extract_opower_token(html: str, now: Optional[float] = None) -> Optional[Tuple[str, float]]:
    """(token, expiry epoch seconds) from the electric-use page HTML, or None if absent."""
    match = re.search(r'"OPowerToken":\s*"([^"]+)"', html or "")
    if not match:
        return None
    token = match.group(1)
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        expires_at = float(payload.get("exp", 0))
    except Exception:
        expires_at = (now if now is not None else time.time()) + 3600
    return token, expires_at


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DuquesneClient:
    """
    Logged-in Duquesne Light / Opower session.

    html_fetcher fetches a page in a real browser using the session's cookies; it is
    tried when plain HTTP returns a bot-challenge page (or always, with
    prefer_browser=True). opower_token/token_expires_at reuse a token from an
    earlier run.
    """

    def __init__(
        self,
        username: str,
        password: str,
        customer_uuid: Optional[str] = None,
        utility_account_uuid: Optional[str] = None,
        customer_urn: Optional[str] = None,
        html_fetcher: Optional[HtmlFetcher] = None,
        prefer_browser: bool = False,
        opower_token: Optional[str] = None,
        token_expires_at: float = 0.0,
        session: Optional[requests.Session] = None,
    ):
        self.username = username or ""
        self.password = password or ""
        self.customer_uuid = customer_uuid or None
        self.utility_account_uuid = utility_account_uuid or None
        self.customer_urn = (
            f"urn:opower:customer:uuid:{self.customer_uuid}" if self.customer_uuid else (customer_urn or None)
        )
        self.html_fetcher = html_fetcher
        self.prefer_browser = prefer_browser
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.opower_token = opower_token
        self.token_expires_at = float(token_expires_at or 0.0)

    # -- authentication ----------------------------------------------------

    def login(self) -> None:
        """POST credentials to the DLC auth server to establish session cookies."""
        if not self.username or not self.password:
            raise DuquesneAuthError("Duquesne Light username and password are required")
        try:
            resp = self.session.post(
                LOGIN_URL,
                json={
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                    "remember_username": False,
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (400, 401, 403):
                raise DuquesneAuthError(f"Duquesne Light login rejected (HTTP {status})") from exc
            raise DuquesneError(f"Duquesne Light login failed: {exc}") from exc
        except requests.RequestException as exc:
            raise DuquesneError(f"Duquesne Light login failed: {exc}") from exc
        logger.info("DLC login successful")

    def _store_token(self, found: Tuple[str, float]) -> str:
        self.opower_token, self.token_expires_at = found
        logger.info(
            "Extracted OPowerToken (expires in ~%.0f min)",
            (self.token_expires_at - time.time()) / 60,
        )
        return self.opower_token

    def fetch_opower_token(self) -> str:
        """
        GET the electric-use page and extract the OPowerToken the server embeds in
        the app.opower.init({modelJson: ...}) script block, retrying through
        html_fetcher when plain HTTP gets a bot-challenge page.
        """
        try:
            resp = self.session.get(ELECTRIC_USE_URL, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise DuquesneError(f"Failed to fetch electric-use page: {exc}") from exc

        html = resp.text
        found = extract_opower_token(html)
        if found:
            return self._store_token(found)

        challenged = html_looks_like_bot_challenge(html)
        if (self.prefer_browser or challenged) and self.html_fetcher:
            logger.info(
                "my-electric-use: no OPowerToken in requests response%s — trying browser",
                " (bot wall / Incapsula suspected)" if challenged else "",
            )
            html2 = self.html_fetcher(ELECTRIC_USE_URL, self.session)
            if html2:
                found = extract_opower_token(html2)
                if found:
                    return self._store_token(found)
                html = html2

        logger.debug(
            "electric-use response: final_url=%s status=%s content_type=%s len=%s "
            "has_literal_OPowerToken=%s has_app_opower=%s has_modelJson=%s",
            getattr(resp, "url", ""),
            resp.status_code,
            resp.headers.get("Content-Type", ""),
            len(html),
            "OPowerToken" in html,
            "app.opower" in html,
            "modelJson" in html,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("electric-use HTML snippet (first 2500 chars): %s", html[:2500].replace("\n", " "))
        if html_looks_like_bot_challenge(html):
            raise DuquesneBlockedError(
                "OPowerToken not found: the electric-use page returned a bot-challenge page"
                + ("" if self.html_fetcher else " and no browser fetcher is configured")
            )
        raise DuquesneBlockedError("OPowerToken not found in electric-use page HTML")

    def discover_account_info(self) -> bool:
        """Fill customer_uuid / customer_urn / utility_account_uuid from Opower's customers/current."""
        try:
            resp = self.session.get(OPOWER_CUSTOMERS_URL, headers=self.opower_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Failed to discover Opower account info: %s", exc)
            return False

        uuid = data.get("uuid")
        if uuid:
            self.customer_uuid = uuid
            self.customer_urn = f"urn:opower:customer:uuid:{uuid}"
        accounts = data.get("utilityAccounts", [])
        if accounts:
            self.utility_account_uuid = accounts[0].get("uuid")
        logger.info(
            "Opower account discovered — customer_uuid=%s  utility_account_uuid=%s",
            self.customer_uuid,
            self.utility_account_uuid,
        )
        return bool(self.customer_uuid)

    def token_valid(self) -> bool:
        # 5-minute safety margin before expiry
        return bool(self.opower_token) and time.time() < self.token_expires_at - 300

    def ensure_authenticated(self) -> None:
        """Log in and fetch a fresh Opower token unless the current one is still valid."""
        if self.token_valid():
            return
        self.login()
        self.fetch_opower_token()
        if not self.customer_uuid or not self.utility_account_uuid:
            self.discover_account_info()

    def billing_session(self) -> requests.Session:
        """
        Log in and touch the account summary so the session's cookies cover the
        billing pages; hand the result to the browser bill-PDF download.
        """
        self.login()
        try:
            self.session.get(ACCOUNT_SUMMARY_URL, timeout=30)
        except Exception:
            pass
        return self.session

    def opower_headers(self) -> Dict:
        """Common headers for every duq.opower.com request."""
        headers: Dict = {
            "Authorization": f"Bearer {self.opower_token}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "Referer": "https://www.duquesnelight.com/",
            "Origin": "https://www.duquesnelight.com",
            "Accept-Language": "en-US",
        }
        if self.customer_urn:
            headers["Opower-Selected-Entities"] = f'["{self.customer_urn}"]'
        return headers

    def _require_account(self) -> None:
        if not self.customer_urn or not self.utility_account_uuid:
            raise DuquesneError(
                "Opower account identifiers missing after login (customer UUID / utility account UUID)"
            )

    # -- usage -------------------------------------------------------------

    def fetch_daily_response(self, interval: Optional[str] = None, now: Optional[datetime] = None) -> Dict:
        """
        Raw DAY-resolution GraphQL response (rates, rate plan, daily kWh/cost, demand).
        interval defaults to the 50 days ending today.
        """
        self.ensure_authenticated()
        self._require_account()
        if not interval:
            end = now or datetime.now()
            interval = time_interval(end - timedelta(days=50), end)
        payload = {
            "operationName": "WDB_GetCostReadsForDayAndHour",
            "variables": {
                "path": "day",
                "resolution": "DAY",
                "timeInterval": interval,
                "utilityAccountUuid": self.utility_account_uuid,
                "lastForServicePoints": 50,
                "aliased": False,
                "forceLegacyData": True,
                "customerURN": self.customer_urn,
                "locale": "en-US",
            },
            "query": DAILY_QUERY,
        }
        logger.info("Fetching daily usage data from Duquesne Light API...")
        try:
            response = self.session.post(GRAPHQL_URL, json=payload, headers=self.opower_headers(), timeout=30)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DuquesneError(f"Error fetching Duquesne Light data: {exc}") from exc

    def fetch_hourly_window(self, start: datetime, end: datetime) -> List[HourlyUsageRecord]:
        """
        One HOUR-resolution window (keep it ≤ MAX_HOURLY_DAYS). Request errors are
        logged and give [] so a backfill can keep paging; call ensure_authenticated first.
        """
        payload = {
            "operationName": "WDB_GetCostReadsForDayAndHour",
            "variables": {
                "resolution": "HOUR",
                "timeInterval": time_interval(start, end),
                "lastForServicePoints": 10,
                "aliased": False,
                "forceLegacyData": True,
                "customerURN": self.customer_urn,
            },
            "query": HOURLY_QUERY,
        }
        try:
            logger.info(
                "Fetching hourly usage %s → %s ...", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            )
            response = self.session.post(GRAPHQL_URL, json=payload, headers=self.opower_headers(), timeout=60)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("Error fetching hourly Duquesne data: %s", exc)
            return []
        records = parse_hourly(data)
        logger.info("Fetched %d hourly usage records from Duquesne Light", len(records))
        return records

    def fetch_hourly(self, days: int = MAX_HOURLY_DAYS, end: Optional[datetime] = None) -> List[HourlyUsageRecord]:
        """The newest hourly window: `days` (clamped to MAX_HOURLY_DAYS) ending at `end` (default now)."""
        if days > MAX_HOURLY_DAYS:
            logger.debug(
                "Clamping hourly fetch window from %d to %d days (API limit: 720 hours)", days, MAX_HOURLY_DAYS
            )
            days = MAX_HOURLY_DAYS
        self.ensure_authenticated()
        self._require_account()
        end = end or datetime.now()
        return self.fetch_hourly_window(end - timedelta(days=days), end)

    def fetch_hourly_history(
        self, lookback_days: int = DEFAULT_HOURLY_LOOKBACK_DAYS, now: Optional[datetime] = None
    ) -> List[HourlyUsageRecord]:
        """
        Page backward in ≤29-day windows to pull the AMI history Opower still has.

        Each check only needs the newest window for freshness; this fills gaps that a
        single rolling fetch would miss. Stops after two consecutive empty windows or
        when the lookback is exhausted. Deduplicated by hour.
        """
        lookback = max(MAX_HOURLY_DAYS, lookback_days)
        self.ensure_authenticated()
        self._require_account()

        all_records: List[HourlyUsageRecord] = []
        end = now or datetime.now()
        earliest = end - timedelta(days=lookback)
        empty_streak = 0
        windows = 0
        while end > earliest:
            start = max(earliest, end - timedelta(days=MAX_HOURLY_DAYS))
            if start >= end:
                break
            chunk = self.fetch_hourly_window(start, end)
            windows += 1
            if chunk:
                all_records.extend(chunk)
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= 2 and windows > 1:
                    logger.info(
                        "Stopping hourly backfill after %d empty windows (reached end of AMI history)",
                        empty_streak,
                    )
                    break
            end = start

        out = list({r.dt: r for r in all_records}.values())
        logger.info(
            "Hourly history backfill: %d unique hours across %d windows (lookback=%dd)",
            len(out),
            windows,
            lookback,
        )
        return out


# ---------------------------------------------------------------------------
# GraphQL response parsing
# ---------------------------------------------------------------------------

def parse_rates(data: Dict) -> List[RateInfo]:
    """Extract rate information from a daily GraphQL response."""
    rates = []
    try:
        service_agreements = data.get('data', {}).get('billingAccountByAuthContext', {}).get('serviceAgreements', [])
        for agreement in service_agreements:
            for edge in agreement.get('servicePointsConnection', {}).get('edges', []):
                read_streams = edge.get('node', {}).get('readStreams', {})
                for rate in read_streams.get('rates', []):
                    cost_per_unit = rate.get('costPerUnit', {})
                    rates.append(RateInfo(
                        rate_type=rate.get('rateType', 'UNKNOWN'),
                        cost_per_unit=cost_per_unit.get('value', 0.0),
                        currency=cost_per_unit.get('currency', 'USD'),
                        activation_date=rate.get('activationDate', ''),
                        inactivation_date=rate.get('inactivationDate', ''),
                        tier=rate.get('tier'),
                        time_of_use_period=(
                            rate.get('timeOfUsePeriod', {}).get('label') if rate.get('timeOfUsePeriod') else None
                        ),
                    ))
        logger.info(f"Parsed {len(rates)} rate(s) from Duquesne Light data")
        return rates
    except Exception as e:
        logger.error(f"Error parsing rates: {e}")
        return []


def parse_rate_plan_label(data: Dict) -> Optional[str]:
    """Best-effort current tariff name from GraphQL ratePlan (e.g. RS)."""
    try:
        agreements = (data.get("data") or {}).get("billingAccountByAuthContext", {}).get("serviceAgreements") or []
        for agreement in agreements:
            rp = (agreement or {}).get("ratePlan") or {}
            title = rp.get("titleShort") or rp.get("code") or rp.get("descriptionShort")
            if title:
                return str(title)
    except Exception as e:
        logger.debug("Could not parse ratePlan label: %s", e)
    return None


def parse_usage(data: Dict) -> List[UsageData]:
    """Daily kWh and cost rows from a daily GraphQL response."""
    usage_list = []
    try:
        service_agreements = data.get('data', {}).get('billingAccountByAuthContext', {}).get('serviceAgreements', [])
        for agreement in service_agreements:
            for edge in agreement.get('servicePointsConnection', {}).get('edges', []):
                read_streams = edge.get('node', {}).get('readStreams', {})
                for stream in read_streams.get('netUsage', []):
                    for read in stream.get('reads', []):
                        # interval: "2025-10-27T00:00:00-04:00/2025-10-28T00:00:00-04:00"
                        interval = read.get('timeInterval', '')
                        date_str = interval.split('/')[0].split('T')[0] if '/' in interval else ''
                        kwh = read.get('measuredAmount', {}).get('value', 0.0)
                        cost = read.get('monetaryAmount', {}).get('value', 0.0)
                        usage_list.append(UsageData(
                            date=date_str,
                            kwh=kwh,
                            cost=cost,
                            effective_rate=cost / kwh if kwh > 0 else 0.0,
                        ))
        logger.info(f"Parsed {len(usage_list)} usage records")
        return usage_list
    except Exception as e:
        logger.error(f"Error parsing usage data: {e}")
        return []


def parse_peak_demand(data: Dict) -> Optional[float]:
    """Peak demand (kW) from a daily GraphQL response, if reported."""
    try:
        service_agreements = data.get('data', {}).get('billingAccountByAuthContext', {}).get('serviceAgreements', [])
        for agreement in service_agreements:
            for edge in agreement.get('servicePointsConnection', {}).get('edges', []):
                read_streams = edge.get('node', {}).get('readStreams', {})
                for stream in read_streams.get('demand', []):
                    for read in stream.get('reads', []):
                        if read.get('isMaximum', False):
                            return read.get('measuredAmount', {}).get('value')
        return None
    except Exception as e:
        logger.error(f"Error parsing peak demand: {e}")
        return None


def parse_hourly(data: Dict) -> List[HourlyUsageRecord]:
    """Hourly records from an HOUR-resolution GraphQL response."""
    for err in data.get("errors") or []:
        logger.warning("Duquesne hourly API reported an error: %s", err.get("message", err))
    records: List[HourlyUsageRecord] = []
    try:
        account = (data.get("data") or {}).get("billingAccountByAuthContext") or {}
        for agreement in account.get("serviceAgreements") or []:
            for edge in ((agreement or {}).get("servicePointsConnection") or {}).get("edges") or []:
                streams = ((edge or {}).get("node") or {}).get("readStreams") or {}
                for net_usage in streams.get("netUsage") or []:
                    for read in (net_usage or {}).get("reads") or []:
                        interval = read.get("timeInterval") or ""
                        dt_str = interval.split("/")[0] if "/" in interval else interval
                        if not dt_str:
                            continue
                        kwh = (read.get("measuredAmount") or {}).get("value", 0.0) or 0.0
                        cost = (read.get("monetaryAmount") or {}).get("value", 0.0) or 0.0
                        if kwh >= 0:
                            records.append(HourlyUsageRecord(dt=dt_str, kwh=float(kwh), cost=float(cost)))
    except Exception as e:
        logger.error("Error parsing hourly usage data: %s", e)
    return records


def analyze_usage(usage_data: List[UsageData]) -> UsageAnalysis:
    """Totals and averages over daily rows."""
    if not usage_data:
        raise ValueError("No usage data provided")
    total_kwh = sum(u.kwh for u in usage_data)
    total_cost = sum(u.cost for u in usage_data)
    days = len(usage_data)
    dates = sorted([u.date for u in usage_data if u.date])
    return UsageAnalysis(
        average_daily_kwh=total_kwh / days if days > 0 else 0.0,
        average_daily_cost=total_cost / days if days > 0 else 0.0,
        average_effective_rate=total_cost / total_kwh if total_kwh > 0 else 0.0,
        total_kwh=total_kwh,
        total_cost=total_cost,
        days_analyzed=days,
        date_range=(dates[0], dates[-1]) if dates else ('', ''),
    )


def analyze_response(data: Dict) -> Dict:
    """
    Everything the notifier uses from a daily GraphQL response:
    rates, usage_data, usage_analysis (None without usage), current_effective_rate,
    peak_demand, rate_plan_label. {} for an empty response.
    """
    if not data:
        logger.error("No data retrieved from Duquesne Light API")
        return {}
    rates = parse_rates(data)
    usage_data = parse_usage(data)
    peak_demand = parse_peak_demand(data)
    rate_plan_label = parse_rate_plan_label(data)
    if not usage_data:
        logger.error("No usage data found")
        return {
            'rates': rates,
            'usage_data': [],
            'usage_analysis': None,
            'current_effective_rate': 0.0,
            'peak_demand': peak_demand,
            'rate_plan_label': rate_plan_label,
        }
    usage_analysis = analyze_usage(usage_data)
    return {
        'rates': rates,
        'usage_data': usage_data,
        'usage_analysis': usage_analysis,
        'current_effective_rate': usage_analysis.average_effective_rate,
        'peak_demand': peak_demand,
        'rate_plan_label': rate_plan_label,
    }


def calculate_projected_cost(usage_analysis: UsageAnalysis, rate_per_kwh: float) -> Dict:
    """Projected monthly/annual cost at rate_per_kwh, and savings against current spend."""
    avg_daily_kwh = usage_analysis.average_daily_kwh
    monthly_kwh = avg_daily_kwh * 30
    annual_kwh = avg_daily_kwh * 365
    monthly_cost = monthly_kwh * rate_per_kwh
    annual_cost = annual_kwh * rate_per_kwh
    current_monthly_cost = usage_analysis.average_daily_cost * 30
    current_annual_cost = usage_analysis.average_daily_cost * 365
    monthly_savings = current_monthly_cost - monthly_cost
    annual_savings = current_annual_cost - annual_cost
    return {
        'rate_per_kwh': rate_per_kwh,
        'monthly_kwh': monthly_kwh,
        'annual_kwh': annual_kwh,
        'monthly_cost': monthly_cost,
        'annual_cost': annual_cost,
        'current_monthly_cost': current_monthly_cost,
        'current_annual_cost': current_annual_cost,
        'monthly_savings': monthly_savings,
        'annual_savings': annual_savings,
        'monthly_savings_percent': (monthly_savings / current_monthly_cost * 100) if current_monthly_cost > 0 else 0,
        'annual_savings_percent': (annual_savings / current_annual_cost * 100) if current_annual_cost > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Bill PDFs
# ---------------------------------------------------------------------------

def pdf_text(source: Union[str, bytes]) -> Optional[str]:
    """Text of a bill PDF, given its path or bytes. None if it can't be read (needs pdfplumber)."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — cannot parse bill PDF")
        return None
    try:
        with pdfplumber.open(io.BytesIO(source) if isinstance(source, bytes) else source) as doc:
            return "\n".join((p.extract_text() or "") for p in doc.pages)
    except Exception as e:
        logger.warning("Failed to read bill PDF %s: %s", "<bytes>" if isinstance(source, bytes) else source, e)
        return None


def parse_bill_meta(text: str) -> Dict[str, str]:
    """
    Bill date from bill PDF text (present with or without an EGS).

      Bill ID: 100000000000 Date Prepared: 08/27/2026

    (The bill's "Supplier Agreement ID" is account-level — unchanged across
    suppliers — so it can't mark re-enrollment and isn't recorded.)
    """
    out = {"bill_date": ""}
    m = re.search(r"Date Prepared:\s*(\d{1,2})/(\d{1,2})/(\d{4})", text or "", re.I)
    if m:
        out["bill_date"] = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return out


def parse_egs_from_bill_text(text: str, bill_id: str = "") -> Optional[CurrentEgsFromBill]:
    """
    Parse supplier name + ENERGY CHARGE rate (+ PTC) from bill PDF text.
    None for bills on Duquesne default service, or when no supply rate is found.

    Example lines:
      Supply Charges - Example Energy of PA LLC $108.05
      ENERGY CHARGE 900.40 KH@ 0.120000 $108.05
      Price to Compare $0.1414 / kWh
      DLC Rate RS-Residential Service
    """
    if not text:
        return None

    supplier = None
    # A bill spanning a switch lists one line per provider, letter-spaced and with
    # service dates: "Supply Charges - Duquesne Li g h t 11/24 - 12/21 $94.97".
    # Duquesne lines are default service, not an EGS.
    supply_lines = re.findall(r"Supply Charges\s*[-–—]\s*(.+?)\s*\$\s*([\d,]+\.?\d*)", text, re.I)
    egs_lines = [
        (re.sub(r"\s+\d{1,2}/\d{1,2}\s*-\s*\d{1,2}/\d{1,2}$", "", name.strip()), amount)
        for name, amount in supply_lines
        if "duquesnelight" not in re.sub(r"\s+", "", name).lower()
    ]
    if egs_lines:
        supplier = egs_lines[-1][0]
        supply_charges = float(egs_lines[-1][1].replace(",", ""))
    elif supply_lines:
        return None  # Duquesne default service only
    else:
        m = re.search(r"Supply Charges\s*[-–—]\s*(.+?)(?:\n|$)", text, re.I)
        supplier = m.group(1).strip() if m else None
        supply_charges = None

    # ENERGY CHARGE … @ 0.120000  (KH or kWh)
    rate = None
    kwh = None
    m = re.search(r"ENERGY\s+CHARGE\s+([\d,]+\.?\d*)\s*(?:KH|KWH|kWh)?\s*[@at]+\s*([\d.]+)", text, re.I)
    if m:
        kwh = float(m.group(1).replace(",", ""))
        rate = float(m.group(2))
        if rate > 1.5:  # listed in ¢/kWh
            rate = rate / 100.0

    if rate is None and supply_charges and kwh and kwh > 0:
        rate = supply_charges / kwh

    if not supplier or not rate or rate <= 0:
        return None

    ptc = None
    m = re.search(r"Price to Compare\s*\$?\s*([\d.]+)\s*/?\s*kWh", text, re.I)
    if m:
        ptc = float(m.group(1))
        if ptc > 1.5:
            ptc = ptc / 100.0

    schedule = ""
    m = re.search(r"DLC Rate\s+([^\n]+)", text, re.I)
    if m:
        schedule = m.group(1).strip()
    else:
        m = re.search(r"Rate Schedule:\s*([^\n]+)", text, re.I)
        if m:
            schedule = m.group(1).strip()

    return CurrentEgsFromBill(
        supplier=supplier,
        supply_rate_per_kwh=rate,
        price_to_compare=ptc,
        rate_schedule=schedule,
        bill_id=bill_id,
        supply_charges_usd=supply_charges,
        kwh_on_bill=kwh,
        bill_date=parse_bill_meta(text)["bill_date"],
    )

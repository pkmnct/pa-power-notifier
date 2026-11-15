#!/usr/bin/env python3
"""
Duquesne Light API Analyzer
Fetches usage and rate data from Duquesne Light's GraphQL API to:
- Infer current effective rate
- Analyze usage patterns
- Compare with available plans from PA Power Switch

Auth flow (discovered via DevTools):
  1. POST https://auth.duquesnelight.com/oauth/authorize/login  → sets session cookies
  2. GET  https://www.duquesnelight.com/energy-money-savings/my-electric-use
         → server-renders OPowerToken into page HTML (app.opower.init({modelJson: ...}))
  3. Use OPowerToken as  Authorization: Bearer <token>  for all duq.opower.com calls
"""

import os
import re
import json
import time
import base64
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

try:
    from browser_fetch import (
        fetch_html_via_puppeteer,
        fetch_duquesne_bill_history_via_puppeteer,
        fetch_duquesne_bill_pdf_via_puppeteer,
        html_looks_like_bot_challenge,
        puppeteer_eager_enabled,
    )
except ImportError:
    fetch_html_via_puppeteer = None  # type: ignore
    fetch_duquesne_bill_history_via_puppeteer = None  # type: ignore
    fetch_duquesne_bill_pdf_via_puppeteer = None  # type: ignore
    html_looks_like_bot_challenge = lambda _html: False  # type: ignore
    puppeteer_eager_enabled = lambda: False  # type: ignore

try:
    import pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    pdfplumber = None  # type: ignore
    _PDFPLUMBER_AVAILABLE = False

_DLC_LOGIN_URL = "https://auth.duquesnelight.com/oauth/authorize/login"
_DLC_ELECTRIC_USE_URL = "https://www.duquesnelight.com/energy-money-savings/my-electric-use"
_DLC_ACCOUNT_SUMMARY_URL = "https://www.duquesnelight.com/account-billing/account-summary"
_OPOWER_CUSTOMERS_URL = "https://duq.opower.com/ei/edge/apis/multi-account-v1/cws/duq/customers/current"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


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


class DuquesneLightAnalyzer:
    """Analyzes Duquesne Light usage and rates from their GraphQL API"""

    def __init__(self):
        self.api_url = "https://duq.opower.com/ei/edge/apis/dsm-graphql-v1/cws/graphql"

        # Credentials for the DLC web portal login
        self.username = os.getenv('DUQUESNE_USERNAME')
        self.password = os.getenv('DUQUESNE_PASSWORD')

        # Opower identifiers — auto-discovered after login if not provided
        self.customer_uuid = os.getenv('DUQUESNE_CUSTOMER_UUID')
        self.utility_account_uuid = os.getenv('DUQUESNE_UTILITY_ACCOUNT_UUID')

        # Derived URN
        self.customer_urn = (
            f"urn:opower:customer:uuid:{self.customer_uuid}"
            if self.customer_uuid
            else os.getenv('DUQUESNE_CUSTOMER_URN')
        )

        from env_bootstrap import data_dir

        # Latest raw Opower response (debugging), and the input for DUQUESNE_USE_FILE
        self.response_file = os.getenv(
            'DUQUESNE_RESPONSE_FILE', str(data_dir() / 'duquesne_response.json')
        )
        self.use_file = os.getenv('DUQUESNE_USE_FILE', 'false').lower() == 'true'

        # Shared session keeps DLC cookies alive across calls
        self._session = requests.Session()
        self._session.headers.update({'User-Agent': _USER_AGENT})
        self._opower_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        """POST credentials to DLC auth server to establish a session."""
        if not self.username or not self.password:
            logger.warning(
                "DLC credentials not set. "
                "Provide DUQUESNE_USERNAME and DUQUESNE_PASSWORD env vars."
            )
            return False
        try:
            resp = self._session.post(
                _DLC_LOGIN_URL,
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
            logger.info("DLC login successful")
            return True
        except Exception as exc:
            logger.error("DLC login failed: %s", exc)
            return False

    def _extract_opower_token_from_html(self, html: str) -> Optional[str]:
        """Parse OPower bearer JWT from server-rendered page HTML."""
        match = re.search(r'"OPowerToken":\s*"([^"]+)"', html)
        if not match:
            return None
        token = match.group(1)
        try:
            payload_b64 = token.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            payload = json.loads(
                base64.urlsafe_b64decode(payload_b64 + "=" * padding)
            )
            self._token_expires_at = float(payload.get("exp", 0))
        except Exception:
            self._token_expires_at = time.time() + 3600

        logger.info(
            "Extracted OPowerToken (expires in ~%.0f min)",
            (self._token_expires_at - time.time()) / 60,
        )
        return token

    def _fetch_opower_token(self) -> Optional[str]:
        """
        GET the electric-use page and extract the OPowerToken the server
        embeds in the app.opower.init({modelJson: ...}) script block.

        If plain HTTP gets an Imperva/Incapsula challenge page, optionally retries
        via Puppeteer (see browser_fetch / DUQUESNE_USE_PUPPETEER).
        """
        try:
            resp = self._session.get(_DLC_ELECTRIC_USE_URL, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to fetch electric-use page: %s", exc)
            return None

        html = resp.text
        token = self._extract_opower_token_from_html(html)
        if token:
            return token

        try_puppeteer = puppeteer_eager_enabled() or html_looks_like_bot_challenge(html)
        if try_puppeteer and fetch_html_via_puppeteer:
            logger.info(
                "my-electric-use: no OPowerToken in requests response%s — trying Puppeteer",
                " (bot wall / Incapsula suspected)" if html_looks_like_bot_challenge(html) else "",
            )
            html2 = fetch_html_via_puppeteer(_DLC_ELECTRIC_USE_URL, self._session)
            if html2:
                token = self._extract_opower_token_from_html(html2)
                if token:
                    return token
                html = html2

        logger.error("OPowerToken not found in electric-use page HTML")
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
            snippet = html[:2500].replace("\n", " ")
            logger.debug("electric-use HTML snippet (first 2500 chars): %s", snippet)
        return None

    def _discover_account_info(self) -> bool:
        """
        Call the Opower customers/current endpoint to auto-populate
        customer_uuid, customer_urn, and utility_account_uuid.
        """
        try:
            resp = self._session.get(
                _OPOWER_CUSTOMERS_URL,
                headers=self._opower_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Failed to discover Opower account info: %s", exc)
            return False

        uuid = data.get('uuid')
        if uuid:
            self.customer_uuid = uuid
            self.customer_urn = f"urn:opower:customer:uuid:{uuid}"

        accounts = data.get('utilityAccounts', [])
        if accounts:
            self.utility_account_uuid = accounts[0].get('uuid')

        logger.info(
            "Opower account discovered — customer_uuid=%s  utility_account_uuid=%s",
            self.customer_uuid, self.utility_account_uuid,
        )
        return bool(self.customer_uuid)

    def _ensure_authenticated(self) -> bool:
        """
        Guarantee a valid OPowerToken, refreshing it when expired or absent.
        Returns True if authentication succeeded.
        """
        # 5-minute safety margin before expiry
        if self._opower_token and time.time() < self._token_expires_at - 300:
            return True

        if not self._login():
            return False

        self._opower_token = self._fetch_opower_token()
        if not self._opower_token:
            return False

        # Auto-discover account identifiers if not explicitly configured
        if not self.customer_uuid or not self.utility_account_uuid:
            self._discover_account_info()

        return True

    def _opower_headers(self) -> Dict:
        """Build the common headers for every duq.opower.com request."""
        headers: Dict = {
            'Authorization': f'Bearer {self._opower_token}',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': '*/*',
            'Referer': 'https://www.duquesnelight.com/',
            'Origin': 'https://www.duquesnelight.com',
            'Accept-Language': 'en-US',
        }
        if self.customer_urn:
            headers['Opower-Selected-Entities'] = (
                f'["{self.customer_urn}"]'
            )
        return headers
        
    def fetch_usage_data(self, time_interval: Optional[str] = None) -> Dict:
        """
        Fetch usage data from Duquesne Light GraphQL API.

        Args:
            time_interval: ISO 8601 interval string, e.g.
                           "2025-10-27T00:00:00-04:00/2025-12-15T00:00:00-05:00"
                           Defaults to the last 50 days when omitted.

        Returns:
            GraphQL response as a dictionary (empty dict on failure).
        """
        if self.use_file and os.path.exists(self.response_file):
            logger.info("Loading Duquesne Light data from file: %s", self.response_file)
            try:
                with open(self.response_file, 'r') as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("Error loading response file: %s", exc)
                return {}

        if not self._ensure_authenticated():
            logger.warning("Duquesne Light authentication failed — skipping fetch")
            return {}

        if not self.customer_urn or not self.utility_account_uuid:
            logger.warning(
                "Account identifiers missing after auth. "
                "Set DUQUESNE_CUSTOMER_UUID and DUQUESNE_UTILITY_ACCOUNT_UUID."
            )
            return {}

        # Default to last 50 days if not specified
        if not time_interval:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=50)
            time_interval = (
                f"{start_date.strftime('%Y-%m-%dT00:00:00-04:00')}/"
                f"{end_date.strftime('%Y-%m-%dT00:00:00-04:00')}"
            )
        
        query = """
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
        
        variables = {
            "path": "day",
            "resolution": "DAY",
            "timeInterval": time_interval,
            "utilityAccountUuid": self.utility_account_uuid,
            "lastForServicePoints": 50,
            "aliased": False,
            "forceLegacyData": True,
            "customerURN": self.customer_urn,
            "locale": "en-US"
        }
        
        payload = {
            "operationName": "WDB_GetCostReadsForDayAndHour",
            "variables": variables,
            "query": query
        }
        
        try:
            logger.info("Fetching daily usage data from Duquesne Light API...")
            response = self._session.post(
                self.api_url,
                json=payload,
                headers=self._opower_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if self.response_file:
                try:
                    with open(self.response_file, 'w') as f:
                        json.dump(data, f, indent=2)
                    logger.info("Saved response to %s", self.response_file)
                except Exception as exc:
                    logger.warning("Could not save response file: %s", exc)

            return data

        except Exception as exc:
            logger.error("Error fetching Duquesne Light data: %s", exc)
            return {}
    
    def parse_rates(self, data: Dict) -> List[RateInfo]:
        """Extract rate information from GraphQL response"""
        rates = []
        
        try:
            service_agreements = data.get('data', {}).get('billingAccountByAuthContext', {}).get('serviceAgreements', [])
            
            for agreement in service_agreements:
                service_points = agreement.get('servicePointsConnection', {}).get('edges', [])
                
                for edge in service_points:
                    node = edge.get('node', {})
                    read_streams = node.get('readStreams', {})
                    rate_list = read_streams.get('rates', [])
                    
                    for rate in rate_list:
                        cost_per_unit = rate.get('costPerUnit', {})
                        rate_info = RateInfo(
                            rate_type=rate.get('rateType', 'UNKNOWN'),
                            cost_per_unit=cost_per_unit.get('value', 0.0),
                            currency=cost_per_unit.get('currency', 'USD'),
                            activation_date=rate.get('activationDate', ''),
                            inactivation_date=rate.get('inactivationDate', ''),
                            tier=rate.get('tier'),
                            time_of_use_period=rate.get('timeOfUsePeriod', {}).get('label') if rate.get('timeOfUsePeriod') else None
                        )
                        rates.append(rate_info)
            
            logger.info(f"Parsed {len(rates)} rate(s) from Duquesne Light data")
            return rates
            
        except Exception as e:
            logger.error(f"Error parsing rates: {e}")
            return []

    def parse_rate_plan_label(self, data: Dict) -> Optional[str]:
        """Best-effort current tariff name from GraphQL ratePlan (e.g. RS)."""
        try:
            agreements = (
                (data.get("data") or {})
                .get("billingAccountByAuthContext", {})
                .get("serviceAgreements")
                or []
            )
            for agreement in agreements:
                rp = (agreement or {}).get("ratePlan") or {}
                title = rp.get("titleShort") or rp.get("code") or rp.get("descriptionShort")
                if title:
                    return str(title)
        except Exception as e:
            logger.debug("Could not parse ratePlan label: %s", e)
        return None
    
    def parse_usage(self, data: Dict) -> List[UsageData]:
        """Extract usage data from GraphQL response"""
        usage_list = []
        
        try:
            service_agreements = data.get('data', {}).get('billingAccountByAuthContext', {}).get('serviceAgreements', [])
            
            for agreement in service_agreements:
                service_points = agreement.get('servicePointsConnection', {}).get('edges', [])
                
                for edge in service_points:
                    node = edge.get('node', {})
                    read_streams = node.get('readStreams', {})
                    net_usage_streams = read_streams.get('netUsage', [])
                    
                    for stream in net_usage_streams:
                        reads = stream.get('reads', [])
                        
                        for read in reads:
                            time_interval = read.get('timeInterval', '')
                            # Extract date from interval (format: "2025-10-27T00:00:00-04:00/2025-10-28T00:00:00-04:00")
                            date_str = time_interval.split('/')[0].split('T')[0] if '/' in time_interval else ''
                            
                            measured = read.get('measuredAmount', {})
                            monetary = read.get('monetaryAmount', {})
                            
                            kwh = measured.get('value', 0.0)
                            cost = monetary.get('value', 0.0)
                            
                            # Calculate effective rate
                            effective_rate = cost / kwh if kwh > 0 else 0.0
                            
                            usage_data = UsageData(
                                date=date_str,
                                kwh=kwh,
                                cost=cost,
                                effective_rate=effective_rate
                            )
                            usage_list.append(usage_data)
            
            logger.info(f"Parsed {len(usage_list)} usage records")
            return usage_list
            
        except Exception as e:
            logger.error(f"Error parsing usage data: {e}")
            return []
    
    def get_peak_demand(self, data: Dict) -> Optional[float]:
        """Extract peak demand from GraphQL response"""
        try:
            service_agreements = data.get('data', {}).get('billingAccountByAuthContext', {}).get('serviceAgreements', [])
            
            for agreement in service_agreements:
                service_points = agreement.get('servicePointsConnection', {}).get('edges', [])
                
                for edge in service_points:
                    node = edge.get('node', {})
                    read_streams = node.get('readStreams', {})
                    demand_streams = read_streams.get('demand', [])
                    
                    for stream in demand_streams:
                        reads = stream.get('reads', [])
                        
                        for read in reads:
                            if read.get('isMaximum', False):
                                measured = read.get('measuredAmount', {})
                                return measured.get('value')
            
            return None
            
        except Exception as e:
            logger.error(f"Error parsing peak demand: {e}")
            return None
    
    def analyze_usage(self, usage_data: List[UsageData]) -> UsageAnalysis:
        """Analyze usage patterns and calculate statistics"""
        if not usage_data:
            raise ValueError("No usage data provided")
        
        total_kwh = sum(u.kwh for u in usage_data)
        total_cost = sum(u.cost for u in usage_data)
        days = len(usage_data)
        
        average_daily_kwh = total_kwh / days if days > 0 else 0.0
        average_daily_cost = total_cost / days if days > 0 else 0.0
        average_effective_rate = total_cost / total_kwh if total_kwh > 0 else 0.0
        
        dates = sorted([u.date for u in usage_data if u.date])
        date_range = (dates[0], dates[-1]) if dates else ('', '')
        
        return UsageAnalysis(
            average_daily_kwh=average_daily_kwh,
            average_daily_cost=average_daily_cost,
            average_effective_rate=average_effective_rate,
            total_kwh=total_kwh,
            total_cost=total_cost,
            days_analyzed=days,
            date_range=date_range
        )
    
    def calculate_projected_cost(self, usage_analysis: UsageAnalysis, rate_per_kwh: float) -> Dict:
        """
        Calculate projected monthly/annual costs at a given rate
        
        Args:
            usage_analysis: UsageAnalysis object with usage patterns
            rate_per_kwh: Rate per kWh to use for projection
        
        Returns:
            Dictionary with projected costs
        """
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
            'annual_savings_percent': (annual_savings / current_annual_cost * 100) if current_annual_cost > 0 else 0
        }
    
    def get_current_effective_rate(self, data: Dict) -> float:
        """Get the current effective rate from usage data"""
        usage_data = self.parse_usage(data)
        if not usage_data:
            return 0.0
        
        analysis = self.analyze_usage(usage_data)
        return analysis.average_effective_rate
    
    # Opower's HOUR-resolution reads endpoint rejects windows over 720 hours
    # ("Provided range must be no more than 720 hours") — cap requests at 29 days
    # to stay safely under that with DST/timezone rounding.
    _MAX_HOURLY_DAYS = 29
    # How far back to page when backfilling the local hourly archive.
    _DEFAULT_HOURLY_LOOKBACK_DAYS = 400

    def fetch_hourly_data(
        self,
        days: int = 29,
        end_date: Optional[datetime] = None,
    ) -> List:
        """
        Fetch one HOUR-resolution window (max 29 days) from Opower.

        Args:
            days: Window length ending at end_date (clamped to API max).
            end_date: End of window (default: now). Used to page backward
                      for historical backfill.
        """
        if days > self._MAX_HOURLY_DAYS:
            logger.debug(
                "Clamping hourly fetch window from %d to %d days (API limit: 720 hours)",
                days, self._MAX_HOURLY_DAYS,
            )
            days = self._MAX_HOURLY_DAYS
        try:
            from energy_shopper_core.tou import HourlyUsageRecord
        except ImportError:
            logger.error("energy_shopper_core not available")
            return []

        if self.use_file:
            logger.info("DUQUESNE_USE_FILE=true — skipping hourly API fetch")
            return []

        if not self._ensure_authenticated():
            logger.warning("Duquesne Light authentication failed — skipping hourly fetch")
            return []

        if not self.customer_urn or not self.utility_account_uuid:
            logger.debug("Duquesne account identifiers missing; skipping hourly fetch")
            return []

        end = end_date or datetime.now()
        start = end - timedelta(days=days)
        return self._fetch_hourly_window(start, end)

    def _fetch_hourly_window(self, start_date: datetime, end_date: datetime) -> List:
        try:
            from energy_shopper_core.tou import HourlyUsageRecord
        except ImportError:
            return []

        time_interval = (
            f"{start_date.strftime('%Y-%m-%dT00:00:00-04:00')}/"
            f"{end_date.strftime('%Y-%m-%dT00:00:00-04:00')}"
        )

        query = """
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

        variables = {
            "resolution": "HOUR",
            "timeInterval": time_interval,
            "lastForServicePoints": 10,
            "aliased": False,
            "forceLegacyData": True,
            "customerURN": self.customer_urn,
        }

        payload = {
            "operationName": "WDB_GetCostReadsForDayAndHour",
            "variables": variables,
            "query": query,
        }

        try:
            logger.info(
                "Fetching hourly usage %s → %s ...",
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            )
            response = self._session.post(
                self.api_url,
                json=payload,
                headers=self._opower_headers(),
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("Error fetching hourly Duquesne data: %s", exc)
            return []

        for err in data.get("errors") or []:
            logger.warning("Duquesne hourly API reported an error: %s", err.get("message", err))

        records: List = []
        try:
            root = data.get("data") or {}
            account = root.get("billingAccountByAuthContext") or {}
            agreements = account.get("serviceAgreements") or []
            for agreement in agreements:
                conn = (agreement or {}).get("servicePointsConnection") or {}
                edges = conn.get("edges") or []
                for edge in edges:
                    node = (edge or {}).get("node") or {}
                    streams = node.get("readStreams") or {}
                    for net_usage in streams.get("netUsage") or []:
                        for read in (net_usage or {}).get("reads") or []:
                            interval = read.get("timeInterval") or ""
                            dt_str = interval.split("/")[0] if "/" in interval else interval
                            if not dt_str:
                                continue
                            measured = read.get("measuredAmount") or {}
                            monetary = read.get("monetaryAmount") or {}
                            kwh = measured.get("value", 0.0) or 0.0
                            cost = monetary.get("value", 0.0) or 0.0
                            if kwh >= 0:
                                records.append(HourlyUsageRecord(
                                    dt=dt_str,
                                    kwh=float(kwh),
                                    cost=float(cost),
                                ))
        except Exception as e:
            logger.error("Error parsing hourly usage data: %s", e)

        logger.info("Fetched %d hourly usage records from Duquesne Light", len(records))
        return records

    def fetch_hourly_history(self, lookback_days: Optional[int] = None) -> List:
        """
        Page backward in ≤29-day windows to pull AMI history Opower still has.

        Each check only needs the newest window for freshness; this fills gaps
        (and months like June) that a single rolling 29-day fetch would miss.
        Stops after two consecutive empty windows or when lookback is exhausted.
        """
        lookback = lookback_days
        if lookback is None:
            lookback = int(os.getenv("HOURLY_LOOKBACK_DAYS", str(self._DEFAULT_HOURLY_LOOKBACK_DAYS)))
        lookback = max(self._MAX_HOURLY_DAYS, lookback)

        try:
            from energy_shopper_core.tou import HourlyUsageRecord
        except ImportError:
            logger.error("energy_shopper_core not available")
            return []

        if self.use_file:
            return []

        if not self._ensure_authenticated():
            logger.warning("Duquesne Light authentication failed — skipping hourly history")
            return []

        if not self.customer_urn or not self.utility_account_uuid:
            return []

        all_records: List = []
        end = datetime.now()
        earliest = end - timedelta(days=lookback)
        empty_streak = 0
        windows = 0

        while end > earliest:
            start = max(earliest, end - timedelta(days=self._MAX_HOURLY_DAYS))
            # Avoid zero-length windows
            if start >= end:
                break
            chunk = self._fetch_hourly_window(start, end)
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

        # Deduplicate by dt (overlapping window edges)
        by_dt = {r.dt: r for r in all_records}
        out = list(by_dt.values())
        logger.info(
            "Hourly history backfill: %d unique hours across %d windows (lookback=%dd)",
            len(out),
            windows,
            lookback,
        )
        return out

    def update_hourly_store(self, store, backfill: bool = True) -> int:
        """
        Fetch latest + optional historical hourly windows and merge into store.
        Returns the number of new records added.
        """
        if backfill:
            records = self.fetch_hourly_history()
        else:
            records = self.fetch_hourly_data()
        if records:
            return store.merge(records)
        return 0

    # ------------------------------------------------------------------
    # Current EGS from bill PDF
    # ------------------------------------------------------------------

    @staticmethod
    def parse_egs_from_bill_text(text: str, bill_id: str = "") -> Optional[CurrentEgsFromBill]:
        """
        Parse supplier name + ENERGY CHARGE rate (+ PTC) from bill PDF text.

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
        supply_lines = re.findall(
            r"Supply Charges\s*[-–—]\s*(.+?)\s*\$\s*([\d,]+\.?\d*)", text, re.I
        )
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
            m = re.search(
                r"Supply Charges\s*[-–—]\s*(.+?)(?:\n|$)",
                text,
                re.I,
            )
            supplier = m.group(1).strip() if m else None
            supply_charges = None

        # ENERGY CHARGE … @ 0.120000  (KH or kWh)
        rate = None
        kwh = None
        m = re.search(
            r"ENERGY\s+CHARGE\s+([\d,]+\.?\d*)\s*(?:KH|KWH|kWh)?\s*[@at]+\s*([\d.]+)",
            text,
            re.I,
        )
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
        m = re.search(
            r"Price to Compare\s*\$?\s*([\d.]+)\s*/?\s*kWh",
            text,
            re.I,
        )
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

        meta = DuquesneLightAnalyzer.parse_bill_meta(text)
        return CurrentEgsFromBill(
            supplier=supplier,
            supply_rate_per_kwh=rate,
            price_to_compare=ptc,
            rate_schedule=schedule,
            bill_id=bill_id,
            supply_charges_usd=supply_charges,
            kwh_on_bill=kwh,
            bill_date=meta["bill_date"],
        )

    @staticmethod
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

    @staticmethod
    def _bill_pdf_text(path: str) -> Optional[str]:
        if not _PDFPLUMBER_AVAILABLE:
            logger.warning("pdfplumber not installed — cannot parse bill PDF")
            return None
        try:
            with pdfplumber.open(path) as doc:
                return "\n".join((p.extract_text() or "") for p in doc.pages)
        except Exception as e:
            logger.warning("Failed to read bill PDF %s: %s", path, e)
            return None

    def backfill_bill_archive(self, archive) -> int:
        """
        Download every Duquesne bill not yet in `archive` (a usage_archive.BillArchive)
        and record each bill's supplier, so supplier history survives Duquesne dropping
        old bills. Returns the number of bills added.
        """
        if fetch_duquesne_bill_history_via_puppeteer is None:
            return 0
        if not self._login():
            return 0
        try:
            self._session.get(_DLC_ACCOUNT_SUMMARY_URL, timeout=30)
        except Exception:
            pass

        import tempfile

        added = 0
        with tempfile.TemporaryDirectory() as tmp:
            rows = fetch_duquesne_bill_history_via_puppeteer(
                self._session, tmp, skip_ids=archive.known_ids()
            )
            for row in rows or []:
                bill_id = str(row.get("billId") or "")
                if not bill_id:
                    continue
                amount = {"amount_due_usd": row.get("amountDue")}
                if row.get("skipped"):
                    archive.fill_bill_meta(bill_id, row.get("date") or "", amount)
                    continue
                text = self._bill_pdf_text(row.get("pdfPath") or "")
                if text is None:
                    continue
                meta = self.parse_bill_meta(text)
                egs = self.parse_egs_from_bill_text(text, bill_id=bill_id)
                archive.archive_pdf(
                    row["pdfPath"],
                    bill_id,
                    egs=egs,
                    bill_date=row.get("date") or meta["bill_date"],
                    supplier=egs.supplier if egs else "",
                    details=amount,
                    update_latest=False,
                )
                added += 1
        if added:
            logger.info("Backfilled %d Duquesne bills into the bill archive", added)
        return added

    def fetch_current_egs_from_bill(
        self,
        bill_id: Optional[str] = None,
        pdf_path: Optional[str] = None,
    ) -> Optional[CurrentEgsFromBill]:
        """
        Download the latest bill PDF (Puppeteer) and parse the current EGS
        supply rate. Falls back to DUQUESNE_BILL_PDF if set for offline/dev.
        """
        # Offline / cached PDF path
        env_pdf = os.getenv("DUQUESNE_BILL_PDF", "").strip()
        path = pdf_path or env_pdf

        meta_bill_id = bill_id or ""
        if not path:
            if fetch_duquesne_bill_pdf_via_puppeteer is None:
                logger.warning("browser_fetch bill PDF helper unavailable")
                return None
            if not self._login():
                return None
            # Touch account summary so cookies cover the billing domain
            try:
                self._session.get(_DLC_ACCOUNT_SUMMARY_URL, timeout=30)
            except Exception:
                pass

            import tempfile
            from pathlib import Path

            from env_bootstrap import data_dir as _data_dir

            data_dir = _data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            path = str(data_dir / "latest_bill.pdf")
            meta = fetch_duquesne_bill_pdf_via_puppeteer(
                self._session, path, bill_id=bill_id
            )
            if not meta:
                return None
            meta_bill_id = str(meta.get("billId") or bill_id or "")
            path = str(meta.get("pdfPath") or path)

        if not path or not os.path.exists(path):
            logger.warning("Bill PDF not found at %s", path)
            return None

        text = self._bill_pdf_text(path)
        if text is None:
            return None

        egs = self.parse_egs_from_bill_text(text, bill_id=meta_bill_id)
        if egs:
            logger.info(
                "Current EGS from bill %s: %s @ %.6f $/kWh (PTC=%s)",
                egs.bill_id or "?",
                egs.supplier,
                egs.supply_rate_per_kwh,
                f"{egs.price_to_compare:.6f}" if egs.price_to_compare else "n/a",
            )
            try:
                from usage_archive import BillArchive

                BillArchive().archive_pdf(path, egs.bill_id or meta_bill_id or "unknown", egs=egs)
            except Exception as archive_err:
                logger.debug("Bill archive skipped: %s", archive_err)
        else:
            logger.warning("Could not parse EGS supply rate from bill PDF %s", path)
        return egs

    def run_analysis(self, time_interval: Optional[str] = None) -> Dict:
        """
        Run complete analysis: fetch data, parse rates and usage, return analysis
        
        Returns:
            Dictionary with analysis results including:
            - rates: List of RateInfo objects
            - usage_data: List of UsageData objects
            - usage_analysis: UsageAnalysis object
            - current_effective_rate: float
            - peak_demand: Optional[float]
        """
        logger.info("Starting Duquesne Light analysis...")
        
        # Fetch data
        data = self.fetch_usage_data(time_interval)
        
        if not data:
            logger.error("No data retrieved from Duquesne Light API")
            return {}
        
        # Parse rates and usage
        rates = self.parse_rates(data)
        usage_data = self.parse_usage(data)
        peak_demand = self.get_peak_demand(data)
        rate_plan_label = self.parse_rate_plan_label(data)

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
        
        # Analyze usage
        usage_analysis = self.analyze_usage(usage_data)
        
        return {
            'rates': rates,
            'usage_data': usage_data,
            'usage_analysis': usage_analysis,
            'current_effective_rate': usage_analysis.average_effective_rate,
            'peak_demand': peak_demand,
            'rate_plan_label': rate_plan_label,
        }


def main():
    """Example usage"""
    analyzer = DuquesneLightAnalyzer()
    results = analyzer.run_analysis()
    
    if results:
        print("\n" + "="*60)
        print("Duquesne Light Usage Analysis")
        print("="*60)
        
        if results.get('rates'):
            print("\nCurrent Rates:")
            for rate in results['rates']:
                print(f"  {rate.rate_type}: ${rate.cost_per_unit:.6f} per kWh")
        
        if results.get('usage_analysis'):
            analysis = results['usage_analysis']
            print(f"\nUsage Analysis ({analysis.days_analyzed} days):")
            print(f"  Date Range: {analysis.date_range[0]} to {analysis.date_range[1]}")
            print(f"  Average Daily Usage: {analysis.average_daily_kwh:.2f} kWh")
            print(f"  Average Daily Cost: ${analysis.average_daily_cost:.2f}")
            print(f"  Total Usage: {analysis.total_kwh:.2f} kWh")
            print(f"  Total Cost: ${analysis.total_cost:.2f}")
            print(f"  Effective Rate: ${analysis.average_effective_rate:.6f} per kWh")
        
        if results.get('peak_demand'):
            print(f"\nPeak Demand: {results['peak_demand']:.2f} kW")
        
        print("="*60 + "\n")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    main()


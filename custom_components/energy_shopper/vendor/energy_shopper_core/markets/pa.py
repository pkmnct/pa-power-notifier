"""
Pennsylvania: PA Power Switch (electric) and PA Gas Switch (gas).

Both sites export a distributor's supplier offers as CSV from ShopApi/RateCSV.
"""

from __future__ import annotations

import csv
import logging
import re
from io import StringIO
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests

from ..models import Rate
from .base import OfferListing

logger = logging.getLogger(__name__)

PA_POWER_SWITCH_CSV_URL = "https://www.papowerswitch.com/umbraco/Api/ShopApi/RateCSV"
PA_GAS_SWITCH_CSV_URL = "https://www.pagasswitch.com/umbraco/Api/ShopApi/RateCSV"
# PA Gas Switch shop page: shows the utility's supply-only Price to Compare and the
# billing unit (Mcf / ccf / therm) its supplier offers are quoted in.
PA_GAS_SWITCH_SHOP_URL = "https://www.pagasswitch.com/shop-for-rates/"


def parse_electric_shop_results_url(url: str) -> Dict[str, str]:
    """
    Map PA Power Switch "shop for rates results" query string to ShopApi RateCSV params.

    Example:
      ...?zipcode=15xxx&serviceType=residential&distributor=1180
          &distributorrate=RS+-+Regular+Residential+Service
    → id=1180, servicetype=residential, ratetype=RS - Regular Residential Service,
      zipcode=15xxx
    """
    url = (url or "").strip()
    if not url:
        return {}
    parsed = urlparse(url)
    q = parse_qs(parsed.query, keep_blank_values=False)

    def first(key: str) -> Optional[str]:
        vals = q.get(key)
        if not vals or not str(vals[0]).strip():
            return None
        return unquote_plus(str(vals[0]).strip())

    out: Dict[str, str] = {}
    z = first("zipcode")
    if z:
        out["zipcode"] = z
    st = first("serviceType")
    if st:
        out["servicetype"] = st
    dist = first("distributor")
    if dist:
        out["id"] = dist
    dr = first("distributorrate")
    if dr:
        out["ratetype"] = dr
    return out


def parse_offers_csv(text: str, utility_type: str) -> List[Rate]:
    reader = csv.DictReader(StringIO(text))
    return [
        Rate(
            price=row.get('Price', ''),
            supplier=row.get('Supplier', ''),
            type=row.get('Type', ''),
            cancellation_fee=row.get('Cancellation Fee', ''),
            deposit_required=row.get('Deposit Required', ''),
            # Electric and gas CSVs name these columns differently
            discounts_incentives=row.get('Discounts/Incentives Available')
            or row.get('Discounts available?', ''),
            introductory_price=row.get('Introductory price') or row.get('Introductory price?', ''),
            monthly_fee=row.get('Monthly Fee') or row.get('Monthly service fee amount', ''),
            more_info=row.get('More info', ''),
            service_type=row.get('Service Type', ''),
            term_end_date=row.get('Term End Date', ''),
            term_length=row.get('Term Length', ''),
            utility_type=utility_type,
            new_customers_offer=row.get('New Customers Offer', ''),
            renewable=row.get('Renewable Energy', ''),
            pa_wind=row.get('PA wind', ''),
            phone=row.get('Contact Phone Number', ''),
        )
        for row in reader
    ]


def only_service_type(rates: List[Rate], service_type: str) -> List[Rate]:
    """RateCSV mixes in other service types (e.g. Commercial on a residential query)."""
    wanted = (service_type or '').strip().lower()
    if not wanted:
        return rates
    kept = [r for r in rates if (r.service_type or '').strip().lower() in ('', wanted)]
    if len(kept) < len(rates):
        logger.info(
            "Dropped %d %s offers whose Service Type is not %r",
            len(rates) - len(kept),
            rates[0].utility_type,
            service_type,
        )
    return kept


class PaPowerSwitch:
    """Electric offers for one distributor and rate class (RateCSV returns nothing without both)."""

    utility_type = "electric"

    def __init__(
        self,
        distributor_id: str,
        rate_type: str,
        service_type: str = "residential",
        zipcode: str = "",
        session: Optional[requests.Session] = None,
    ):
        self.distributor_id = distributor_id
        self.rate_type = rate_type
        self.service_type = service_type
        self.zipcode = zipcode
        self._session = session

    def params(self) -> Dict[str, str]:
        params = {
            "id": self.distributor_id,
            "servicetype": self.service_type,
            "ratetype": self.rate_type,
        }
        if self.zipcode:
            params["zipcode"] = self.zipcode
        return params

    def fetch_offers(self) -> OfferListing:
        resp = (self._session or requests).get(PA_POWER_SWITCH_CSV_URL, params=self.params(), timeout=30)
        resp.raise_for_status()
        offers = only_service_type(parse_offers_csv(resp.text, "electric"), self.service_type)
        return OfferListing(offers, resp.text)

    def price_to_compare(self) -> Optional[Tuple[float, str]]:
        # PA Power Switch doesn't publish it per distributor in a readable form; the
        # utility's own bill or rates page is the source.
        return None


class PaGasSwitch:
    """Gas offers for one distributor, plus its Price to Compare and price unit."""

    utility_type = "gas"

    def __init__(
        self,
        distributor_id: str,
        service_type: str = "residential",
        zipcode: str = "",
        session: Optional[requests.Session] = None,
    ):
        self.distributor_id = distributor_id
        self.service_type = service_type
        self.zipcode = zipcode
        self._session = session

    def params(self) -> Dict[str, str]:
        return {"id": self.distributor_id, "servicetype": self.service_type}

    def fetch_offers(self) -> OfferListing:
        resp = (self._session or requests).get(PA_GAS_SWITCH_CSV_URL, params=self.params(), timeout=30)
        resp.raise_for_status()
        offers = only_service_type(parse_offers_csv(resp.text, "gas"), self.service_type)
        return OfferListing(offers, resp.text)

    def price_to_compare(self) -> Optional[Tuple[float, str]]:
        return scrape_pagasswitch_price_to_compare(self.distributor_id, self.zipcode, self._session)


_GAS_PTC_RE = re.compile(
    r"Price to Compare for\s*(\d{5}).{0,250}?\$\s*(\d+\.\d+)\s*per\s*(Mcf|ccf|therm)\b",
    re.IGNORECASE | re.DOTALL,
)


def scrape_pagasswitch_price_to_compare(
    distributor_id: str,
    zipcode: str,
    session: Optional[requests.Session] = None,
    service_type: str = "residential",
) -> Optional[Tuple[float, str]]:
    """
    Scrape the utility's residential Price to Compare from the PA Gas Switch shop page.

    Returns (usd_per_unit, unit) where unit is 'mcf', 'ccf' or 'therm' — the same unit
    PA Gas Switch quotes that distributor's supplier offers in. None if unavailable.
    """
    if not distributor_id or not zipcode:
        return None
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; PA-Power-Notifier/1.0)")
    try:
        r = sess.get(
            PA_GAS_SWITCH_SHOP_URL,
            params={"zipcode": zipcode, "serviceType": service_type, "distributor": distributor_id},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning("PA Gas Switch shop page request failed: %s", e)
        return None

    # The page silently falls back to another utility when the ZIP/distributor pair is
    # invalid; its CSV export link names the distributor actually shown.
    if f"RateCSV?id={distributor_id}&" not in r.text.replace("&amp;", "&"):
        logger.warning(
            "PA Gas Switch shop page did not show distributor %s for ZIP %s — check GAS_ZIPCODE",
            distributor_id,
            zipcode,
        )
        return None

    from bs4 import BeautifulSoup

    body = BeautifulSoup(r.text, "html.parser").get_text(separator=" ", strip=True)
    m = _GAS_PTC_RE.search(body)
    if not m:
        logger.warning("Could not parse Price to Compare from PA Gas Switch shop page")
        return None
    price, unit = float(m.group(2)), m.group(3).lower()
    logger.info("Gas Price to Compare from PA Gas Switch: $%.5f per %s", price, unit)
    return price, unit

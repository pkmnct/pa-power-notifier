#!/usr/bin/env python3
"""
Scrapers for utility rate pages (no public API).
- Duquesne Light: residential "Price to Compare" for electric
- Duquesne Light: TOU supply rates from published tariff PDFs
- People's Gas (PA): residential gas rate when available
"""

import io
import json
import os
import re
import logging
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False

try:
    from browser_fetch import (
        fetch_html_via_puppeteer,
        html_looks_like_bot_challenge,
        puppeteer_eager_enabled,
    )
except ImportError:
    fetch_html_via_puppeteer = None  # type: ignore
    html_looks_like_bot_challenge = lambda _h: False  # type: ignore
    puppeteer_eager_enabled = lambda: False  # type: ignore

logger = logging.getLogger(__name__)

# Duquesne Light residential rates page
DUQUESNE_LIGHT_RATES_URL = "https://duquesnelight.com/service-reliability/service-map/rates/residential-rates"

# People's Gas PA - set PEOPLES_GAS_RATES_URL if your utility uses a different page
PEOPLES_GAS_DEFAULT_URL = "https://www.peoplesgas.com/rates/residential/"


def _parse_cents_or_dollars(text: str) -> Optional[float]:
    """Parse a string like '12.43¢' or '$0.1243' into dollars per unit."""
    if not text:
        return None
    # Match cents: 12.43¢ or 12.43 cents
    cents_match = re.search(r"(\d+\.?\d*)\s*[¢c]|(\d+\.?\d*)\s*cents?", text, re.I)
    if cents_match:
        g = cents_match.group(1) or cents_match.group(2)
        if g:
            return round(float(g) / 100.0, 6)
    # Match dollars: $0.1243 or 0.1243
    dollar_match = re.search(r"\$?\s*(\d+\.?\d*)", text)
    if dollar_match:
        return float(dollar_match.group(1))
    return None


def _parse_duquesne_ptc_html(text: str) -> Optional[float]:
    """Best-effort parse of Duquesne residential PTC from full page HTML."""
    soup = BeautifulSoup(text, "html.parser")

    # Common patterns: "Price to Compare", "12.43¢", "cents per kilowatt-hour"
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json|application/json")):
        try:
            data = script.string or ""
            if "price" in data.lower() or "rate" in data.lower() or "kwh" in data.lower():
                v = _parse_cents_or_dollars(data)
                if v and 0.01 < v < 1.0:
                    logger.info("Duquesne Light PTC from script: %.6f $/kWh", v)
                    return v
        except Exception:
            pass

    body = soup.get_text(separator=" ", strip=True)
    for pattern in [
        r"price\s+to\s+compare[^\d]*(\d+\.?\d*)\s*[¢c]",
        r"(\d+\.?\d*)\s*[¢c]\s*per\s*(?:kilowatt|kwh)",
        r"(\d+\.?\d*)\s*cents?\s*per\s*(?:kilowatt|kwh)",
    ]:
        m = re.search(pattern, body, re.I)
        if m:
            cents = float(m.group(1))
            if 1 < cents < 50:
                rate = round(cents / 100.0, 6)
                logger.info("Duquesne Light PTC from page text: %.6f $/kWh", rate)
                return rate

    for m in re.finditer(r"(\d+\.?\d{2})\s*[¢c]?", body):
        val = float(m.group(1))
        if 5 < val < 30:
            idx = m.start()
            snippet = body[max(0, idx - 80) : idx + 40].lower()
            if "kwh" in snippet or "compare" in snippet or "rate" in snippet:
                rate = round(val / 100.0, 6)
                logger.info("Duquesne Light PTC from snippet: %.6f $/kWh", rate)
                return rate

    return None


def scrape_duquesne_light_ptc(
    url: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Optional[float]:
    """
    Scrape Duquesne Light residential "Price to Compare" (default supply rate) in $/kWh.
    Returns None if the page is unavailable or parsing fails.
    """
    url = url or DUQUESNE_LIGHT_RATES_URL
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; PA-Power-Notifier/1.0)")
    try:
        r = sess.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.warning("Duquesne Light rates page request failed: %s", e)
        return None

    text = r.text
    parsed = _parse_duquesne_ptc_html(text)
    if parsed is not None:
        return parsed

    if fetch_html_via_puppeteer and (
        puppeteer_eager_enabled() or html_looks_like_bot_challenge(text)
    ):
        html2 = fetch_html_via_puppeteer(url, None)
        if html2:
            parsed = _parse_duquesne_ptc_html(html2)
            if parsed is not None:
                return parsed

    logger.warning("Could not parse Price to Compare from Duquesne Light page")
    return None


def scrape_peoples_gas_rate(
    url: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Optional[float]:
    """
    Scrape People's Gas (PA) residential gas rate in $/therm when possible.
    Returns None if the page is unavailable or doesn't contain a parseable rate.
    """
    url = url or os.getenv("PEOPLES_GAS_RATES_URL", PEOPLES_GAS_DEFAULT_URL)
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; PA-Power-Notifier/1.0)")
    try:
        r = sess.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.warning("People's Gas rates page request failed: %s", e)
        return None

    text = r.text
    soup = BeautifulSoup(text, "html.parser")
    body = soup.get_text(separator=" ", strip=True)

    for pattern in [
        r"\$(\d+\.?\d*)\s*per\s*(?:therm|ccf)",
        r"(\d+\.?\d*)\s*[¢c]?\s*per\s*(?:therm|ccf)",
        r"rate[^\d]*\$?(\d+\.?\d*)",
    ]:
        m = re.search(pattern, body, re.I)
        if m:
            val = float(m.group(1))
            if val > 1 and ("¢" in m.group(0) or "cent" in m.group(0).lower()):
                val = round(val / 100.0, 6)
            if 0.1 < val < 5.0:
                logger.info("People's Gas rate from page: %.6f $/therm", val)
                return val

    v = _parse_cents_or_dollars(body)
    if v and 0.1 < v < 5.0:
        logger.info("People's Gas rate from page (generic): %.6f", v)
        return v

    logger.warning("Could not parse rate from People's Gas page")
    return None


# ---------------------------------------------------------------------------
# Duquesne Light TOU supply rate scraper
# ---------------------------------------------------------------------------

# Pages that link to the DSS tariff supplement PDFs
_DUQUESNE_SEARCH_PAGES = [
    "https://duquesnelight.com/service-reliability/service-map/rates/residential-rates",
    "https://duquesnelight.com/energy-money-savings/electric-vehicles/charge-smart-and-save/time-of-use-supply-rate",
]

# Pattern for Duquesne DSS supplement PDFs in URL/href attributes
# e.g. supplement-no-96-to-tariff-no-25-rider-no-8-...-effective-12-01-2025.pdf
_DSS_PDF_RE = re.compile(
    r"supplement-no-(\d+)-to-tariff-no-25.*?effective-(\d{2}-\d{2}-\d{4})\.pdf",
    re.IGNORECASE,
)

# Residential TOU rate table row: period text then 4 decimal numbers (¢/kWh)
# e.g. "June 1, 2025, through November 30, 2025   9.7093   27.9343   5.1528   3.7957"
_RATE_ROW_RE = re.compile(
    r"([A-Z][a-z]+ \d+,\s*\d{4}),\s+through\s+([A-Z][a-z]+ \d+,\s*\d{4})"
    r"\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
)


def _find_dss_pdf_links(
    session: requests.Session,
) -> list:
    """
    Scrape Duquesne Light pages for DSS tariff supplement PDF links.
    Returns a list of (url, supplement_number, effective_date) tuples,
    sorted by effective_date descending (most recent first).
    """
    found = {}
    for page_url in _DUQUESNE_SEARCH_PAGES:
        try:
            resp = session.get(page_url, timeout=20)
            if resp.status_code != 200:
                logger.debug("Duquesne page %s returned %d", page_url, resp.status_code)
                continue
            text = resp.text
            if fetch_html_via_puppeteer and (
                puppeteer_eager_enabled() or html_looks_like_bot_challenge(text)
            ):
                html2 = fetch_html_via_puppeteer(page_url, None)
                if html2:
                    text = html2
            soup = BeautifulSoup(text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = _DSS_PDF_RE.search(href)
                if not m:
                    continue
                sup_num = int(m.group(1))
                eff_date = datetime.strptime(m.group(2), "%m-%d-%Y").date()
                full_url = href if href.startswith("http") else "https://duquesnelight.com" + href
                if eff_date not in found:
                    found[eff_date] = (full_url, sup_num, eff_date)
        except Exception as e:
            logger.debug("Could not scrape %s: %s", page_url, e)

    return sorted(found.values(), key=lambda t: t[2], reverse=True)


def _parse_tou_rates_from_pdf_bytes(
    pdf_bytes: bytes,
    source_url: str = "",
) -> Optional[object]:
    """
    Parse a Duquesne Light DSS tariff PDF for the residential TOU rates
    that apply to today.

    Returns a TOURates dataclass or None if parsing fails.
    The import is deferred so this module can load even if pdfplumber
    is not installed.
    """
    if not _PDFPLUMBER_AVAILABLE:
        logger.warning("pdfplumber is not installed; cannot parse TOU rates from PDF")
        return None

    from energy_shopper_core.tou import TOURates

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # Concatenate all page text for regex searching
            full_text = "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )
    except Exception as e:
        logger.error("pdfplumber could not open PDF: %s", e)
        return None

    today = date.today()

    # The residential TOU table is under the "Residential" header in the
    # DEFAULT SERVICE SUPPLY RATE section.  Locate it and search within
    # a reasonable window so we don't accidentally match Small C&I rows.
    residential_start = full_text.lower().find("residential\n")
    small_ci_start = full_text.lower().find("small commercial", residential_start + 1) if residential_start >= 0 else -1

    search_text = (
        full_text[residential_start:small_ci_start]
        if residential_start >= 0 and small_ci_start > residential_start
        else full_text
    )

    for m in _RATE_ROW_RE.finditer(search_text):
        try:
            period_start = datetime.strptime(m.group(1).replace("  ", " "), "%B %d, %Y").date()
            period_end = datetime.strptime(m.group(2).replace("  ", " "), "%B %d, %Y").date()
        except ValueError:
            continue

        if not (period_start <= today <= period_end):
            continue

        # Values in the PDF are in ¢/kWh; convert to $/kWh
        standard_rate = float(m.group(3)) / 100
        peak_rate = float(m.group(4)) / 100
        off_peak_rate = float(m.group(5)) / 100
        super_off_peak_rate = float(m.group(6)) / 100

        logger.info(
            "Parsed TOU rates (effective %s–%s): flat=%.4f¢ peak=%.4f¢ "
            "off_peak=%.4f¢ super_off_peak=%.4f¢",
            period_start, period_end,
            standard_rate * 100, peak_rate * 100,
            off_peak_rate * 100, super_off_peak_rate * 100,
        )
        return TOURates(
            standard_rate=standard_rate,
            peak_rate=peak_rate,
            off_peak_rate=off_peak_rate,
            super_off_peak_rate=super_off_peak_rate,
            effective_start=period_start.isoformat(),
            effective_end=period_end.isoformat(),
            source=source_url,
        )

    logger.warning("No TOU rate row matching today (%s) found in PDF", today)
    return None


def _load_tou_cache(cache_path: str) -> Optional[object]:
    """Load cached TOURates from disk, returning None if stale or missing."""
    if not os.path.exists(cache_path):
        return None
    try:
        from energy_shopper_core.tou import TOURates
        with open(cache_path) as f:
            data = json.load(f)
        rates = TOURates(**data)
        # Cache is valid until effective_end
        if date.today() <= date.fromisoformat(rates.effective_end):
            logger.info(
                "Using cached TOU rates (valid through %s, source: %s)",
                rates.effective_end, rates.source,
            )
            return rates
        logger.info("Cached TOU rates expired on %s, will re-scrape", rates.effective_end)
    except Exception as e:
        logger.debug("Could not load TOU rate cache at %s: %s", cache_path, e)
    return None


def _save_tou_cache(rates, cache_path: str):
    """Persist TOURates to disk."""
    try:
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        from dataclasses import asdict
        with open(cache_path, "w") as f:
            json.dump(asdict(rates), f, indent=2)
        logger.info("Cached TOU rates to %s", cache_path)
    except Exception as e:
        logger.warning("Could not save TOU rate cache: %s", e)


def scrape_duquesne_tou_rates(
    cache_path: Optional[str] = None,
    pdf_url_override: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Optional[object]:
    """
    Fetch and return the current Duquesne Light residential TOU supply rates
    as a TOURates dataclass.

    Strategy (in order):
      1. Return cached rates if still valid (effective_end >= today).
      2. If pdf_url_override is set, download that PDF directly.
      3. Scrape the Duquesne Light rates/TOU pages for DSS supplement PDF links,
         pick the most recent PDF with an effective date on or before today,
         download and parse it.
      4. On success, update the cache file.
      5. If all attempts fail, return None (caller should fall back to env vars).

    Args:
        cache_path: Path to cache file (default /data/tou_rates.json).
        pdf_url_override: Override PDF URL (e.g. TOU_RATES_PDF_URL env var).
        session: Optional requests.Session for shared connection pooling.
    """
    if not _PDFPLUMBER_AVAILABLE:
        logger.warning(
            "pdfplumber not installed — cannot scrape TOU rates from PDF. "
            "Install it or set TOU_PEAK_RATE / TOU_OFF_PEAK_RATE / TOU_SUPER_OFF_PEAK_RATE manually."
        )
        return None

    cache_path = cache_path or "/data/tou_rates.json"

    # 1. Try cache first
    cached = _load_tou_cache(cache_path)
    if cached is not None:
        return cached

    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; PA-Power-Notifier/1.0)")

    pdf_candidates = []

    # 2. Honour manual override
    if pdf_url_override:
        pdf_candidates.append(pdf_url_override)

    # 3. Scrape pages for PDF links
    scraped_links = _find_dss_pdf_links(sess)
    today = date.today()
    for url, _sup_num, eff_date in scraped_links:
        # Only consider PDFs whose effective date is on or before today
        if eff_date <= today:
            pdf_candidates.append(url)

    if not pdf_candidates:
        logger.warning("No Duquesne Light DSS PDF URLs found; cannot scrape TOU rates")
        return None

    for pdf_url in pdf_candidates:
        logger.info("Downloading Duquesne DSS PDF: %s", pdf_url)
        try:
            resp = sess.get(pdf_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("Could not download PDF %s: %s", pdf_url, e)
            continue

        rates = _parse_tou_rates_from_pdf_bytes(resp.content, source_url=pdf_url)
        if rates is not None:
            _save_tou_cache(rates, cache_path)
            return rates

    logger.warning("Could not parse TOU rates from any Duquesne Light PDF")
    return None

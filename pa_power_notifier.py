#!/usr/bin/env python3
"""
PA Power & Gas Rate Notifier
Runs on a configurable interval; alerts via email when lower rates are available.
"""

import os
import json
import smtplib
import time
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple
from dataclasses import asdict
from pathlib import Path

from energy_shopper_core.gas import GAS_PRICE_UNITS
from energy_shopper_core.markets.pa import (
    PaGasSwitch,
    PaPowerSwitch,
    only_service_type,
    parse_electric_shop_results_url,
    parse_offers_csv,
    scrape_pagasswitch_price_to_compare,
)
from energy_shopper_core.models import Rate
from energy_shopper_core.tou import TOUAnalyzer, TOURates
from env_bootstrap import data_dir
from gas_usage_forecast import therms_per_mcf, usd_per_mcf_to_usd_per_therm
from usage_archive import HourlyUsageStore


def _default_data_file(name: str) -> str:
    """Default path under DATA_DIR (<repo>/data locally, /data in the image)."""
    return str(data_dir() / name)


try:
    from duquesne_light_analyzer import DuquesneLightAnalyzer
except ImportError:
    DuquesneLightAnalyzer = None  # type: ignore

try:
    from peoples_gas_analyzer import PeoplesGasAnalyzer
except ImportError:
    PeoplesGasAnalyzer = None  # type: ignore

try:
    from rate_scrapers import (
        scrape_duquesne_light_ptc,
        scrape_peoples_gas_rate,
        scrape_duquesne_tou_rates,
    )
except ImportError:
    scrape_duquesne_light_ptc = None  # type: ignore
    scrape_peoples_gas_rate = None  # type: ignore
    scrape_duquesne_tou_rates = None  # type: ignore

try:
    from env_bootstrap import configure_logging_from_env

    configure_logging_from_env()
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
logger = logging.getLogger(__name__)


def _email_state_path():
    from env_bootstrap import data_dir

    return data_dir() / "last_email.json"


def _email_state_changed(report) -> bool:
    """
    EMAIL_MODE=change (default): True when the report's findings differ from the last
    email, a new rate jump is reported, or EMAIL_REMIND_DAYS have passed since the last
    email. EMAIL_MODE=daily: always True.
    """
    if os.getenv("EMAIL_MODE", "change").strip().lower() == "daily":
        return True
    if any(sec and sec.rate_alert_is_new for sec in (report.electric, report.gas)):
        return True
    path = _email_state_path()
    try:
        last = json.loads(path.read_text()) if path.is_file() else None
    except Exception:
        last = None
    if not last or last.get("fingerprint") != report.email_fingerprint():
        return True
    try:
        remind_days = float(os.getenv("EMAIL_REMIND_DAYS") or "0")
    except ValueError:
        remind_days = 0
    if remind_days > 0:
        try:
            sent = datetime.fromisoformat(last.get("sent_at", ""))
            return (datetime.now() - sent).total_seconds() >= remind_days * 86400
        except ValueError:
            return True
    return False


def _record_email_state(report) -> None:
    path = _email_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "sent_at": datetime.now().isoformat(timespec="seconds"),
                    "subject": report.email_subject(),
                    "fingerprint": report.email_fingerprint(),
                },
                indent=2,
            )
        )
    except Exception as e:
        logger.warning("Could not record email state: %s", e)


class RateChecker:
    def __init__(self):
        # Electric API — required: ELECTRIC_ID + ELECTRIC_RATE_TYPE, or the shop results URL
        # they come from (RateCSV returns nothing without a rate type), e.g.
        # https://www.papowerswitch.com/shop-for-rates-results/
        #   ?zipcode=…&serviceType=residential&distributor=1180
        #   &distributorrate=RS+-+Regular+Residential+Service
        self.electric_api_url = "https://www.papowerswitch.com/umbraco/Api/ShopApi/RateCSV"
        self.electric_id = os.getenv("ELECTRIC_ID", "").strip()
        self.electric_service_type = os.getenv("ELECTRIC_SERVICE_TYPE", "residential")
        self.electric_rate_type = os.getenv("ELECTRIC_RATE_TYPE", "").strip()
        self.electric_zipcode = os.getenv("ELECTRIC_ZIPCODE", "").strip()

        shop_url = os.getenv("ELECTRIC_SHOP_RESULTS_URL", "").strip()
        if shop_url:
            shop_params = parse_electric_shop_results_url(shop_url)
            if "id" in shop_params:
                self.electric_id = shop_params["id"]
            if "servicetype" in shop_params:
                self.electric_service_type = shop_params["servicetype"]
            if "ratetype" in shop_params:
                self.electric_rate_type = shop_params["ratetype"]
            if "zipcode" in shop_params:
                self.electric_zipcode = shop_params["zipcode"]

        # Gas API
        self.gas_api_url = "https://www.pagasswitch.com/umbraco/Api/ShopApi/RateCSV"
        self.gas_id = os.getenv('GAS_ID', '').strip()
        self.gas_service_type = os.getenv('GAS_SERVICE_TYPE', 'residential')
        # ZIP for the PA Gas Switch shop page (Price to Compare + price unit)
        self.gas_zipcode = os.getenv('GAS_ZIPCODE', '').strip() or self.electric_zipcode
        # Manual fallbacks when the shop page can't be scraped. PA Gas Switch quotes
        # offers in the utility's billing unit (Mcf, ccf or therm, by utility).
        self.gas_price_unit = os.getenv('GAS_PRICE_UNIT', '').strip().lower()
        self.gas_price_to_compare = float(os.getenv('GAS_PRICE_TO_COMPARE', '0') or 0)
        # Raw RateCSV text from the latest fetch, kept for the market archive
        self.last_offers_csv: Dict[str, str] = {}

        # Fallback current rates (overridden by scraper/analyzer when available)
        self.current_electric_price = float(os.getenv('CURRENT_ELECTRIC_PRICE', '0'))
        self.current_gas_price = float(os.getenv('CURRENT_GAS_PRICE', '0'))

        # Savings threshold to trigger a notification
        self.notification_threshold = float(os.getenv('NOTIFICATION_THRESHOLD', '5.0'))

        # Rate filters
        self.filter_no_cancellation_fee = os.getenv('FILTER_NO_CANCELLATION_FEE', 'false').lower() == 'true'
        self.filter_renewable_only = os.getenv('FILTER_RENEWABLE_ONLY', 'false').lower() == 'true'

        # Include Duquesne Light PTC as a synthetic "stay on default" option
        self.include_duquesne_as_option = os.getenv('INCLUDE_DUQUESNE_AS_OPTION', 'true').lower() == 'true'

        # Storage (defaults under repo ./data/ when env unset — avoids /data EROFS on macOS/Linux desktop)
        self.storage_file = os.getenv('STORAGE_FILE', _default_data_file('previous_rates.json'))

        # TOU analysis
        self.hourly_usage_file = os.getenv('HOURLY_USAGE_FILE', _default_data_file('hourly_usage.json'))
        self.daily_usage_file = os.getenv('DAILY_USAGE_FILE', _default_data_file('daily_usage.json'))
        self.gas_usage_file = os.getenv('GAS_USAGE_FILE', _default_data_file('gas_usage.json'))
        self.bill_archive_dir = os.getenv('BILL_ARCHIVE_DIR', _default_data_file('bills'))
        self.tou_rates_cache = os.getenv('TOU_RATES_CACHE_FILE', _default_data_file('tou_rates.json'))
        self.tou_pdf_url = os.getenv('TOU_RATES_PDF_URL', '')
        # Manual rate overrides (¢/kWh, converted to $/kWh). Used only when
        # the PDF scraper is unavailable or explicitly overridden.
        self.tou_peak_override = self._cents_env('TOU_PEAK_RATE')
        self.tou_off_peak_override = self._cents_env('TOU_OFF_PEAK_RATE')
        self.tou_super_off_peak_override = self._cents_env('TOU_SUPER_OFF_PEAK_RATE')
        self.tou_flat_override = self._cents_env('TOU_FLAT_RATE')

        # Email
        self.smtp_host = os.getenv('SMTP_HOST', '')
        self.smtp_port = int(os.getenv('SMTP_PORT', '587'))
        self.smtp_username = os.getenv('SMTP_USERNAME', '')
        self.smtp_password = os.getenv('SMTP_PASSWORD', '')
        self.notify_to = os.getenv('NOTIFY_TO_EMAIL', '')
        self.notify_from = os.getenv('NOTIFY_FROM_EMAIL', '') or self.smtp_username

        self._ptc = None
        self._ptc_resolved = False
        self._last_electric_analysis = None
        self._last_gas_analysis = None
        self._last_egs_bill = None
        self._egs_bill_resolved = False

    @staticmethod
    def _cents_env(name: str) -> Optional[float]:
        """Read an env var as ¢/kWh and return $/kWh, or None if unset."""
        val = os.getenv(name, '')
        if val:
            try:
                return float(val) / 100.0
            except ValueError:
                pass
        return None

    # ------------------------------------------------------------------
    # Current-rate resolution
    # ------------------------------------------------------------------

    def _cached_ptc(self) -> Optional[float]:
        """Resolve Duquesne PTC once per check cycle (bill → scrape → None)."""
        if self._ptc_resolved:
            return self._ptc
        self._ptc_resolved = True
        self._ptc = None

        egs = self.get_current_egs_from_bill()
        if egs and egs.price_to_compare and egs.price_to_compare > 0:
            self._ptc = egs.price_to_compare
            logger.info("Electric PTC from bill PDF: %.6f $/kWh", self._ptc)
            return self._ptc

        if scrape_duquesne_light_ptc is not None:
            try:
                ptc = scrape_duquesne_light_ptc()
                if ptc and ptc > 0:
                    self._ptc = ptc
                    logger.info("Electric supply reference (PTC scrape): %.6f $/kWh", ptc)
            except Exception as e:
                logger.debug("Duquesne PTC scrape failed: %s", e)
        return self._ptc

    def get_current_egs_from_bill(self):
        """Current EGS name + supply-only $/kWh from the latest DLC bill PDF."""
        if self._egs_bill_resolved:
            return self._last_egs_bill
        self._egs_bill_resolved = True
        self._last_egs_bill = None
        if DuquesneLightAnalyzer is None:
            return None
        try:
            analyzer = DuquesneLightAnalyzer()
            self._last_egs_bill = analyzer.fetch_current_egs_from_bill()
        except Exception as e:
            logger.warning("Duquesne bill EGS fetch failed: %s", e)
        return self._last_egs_bill

    def get_current_electric_rate(self) -> float:
        if DuquesneLightAnalyzer is not None:
            try:
                analyzer = DuquesneLightAnalyzer()
                results = analyzer.run_analysis()
                self._last_electric_analysis = results
                try:
                    from usage_archive import DailyElectricUsageStore

                    rows = (results or {}).get("usage_data") or []
                    if rows:
                        store = DailyElectricUsageStore(self.daily_usage_file)
                        store.merge(rows)
                        store.save()
                except Exception as archive_err:
                    logger.debug("Daily usage archive skipped: %s", archive_err)
                rate = results.get("current_effective_rate") if results else None
                if rate and rate > 0:
                    logger.info("Electric rate from Duquesne account: %.6f $/kWh", rate)
                    return rate
            except Exception as e:
                logger.debug("Duquesne analyzer failed: %s", e)

        ptc = self._cached_ptc()
        if ptc and ptc > 0:
            logger.info("Electric rate from Duquesne PTC: %.6f $/kWh", ptc)
            return ptc

        return self.current_electric_price

    def get_current_electric_supply_rate(self) -> Optional[float]:
        """
        Supply-only $/kWh you are paying now (EGS ENERGY CHARGE from bill).

        Prefer this over PTC when deciding whether a new offer beats *your*
        current supplier.
        """
        egs = self.get_current_egs_from_bill()
        if egs and egs.supply_rate_per_kwh > 0:
            return egs.supply_rate_per_kwh
        return None

    def get_electric_supply_reference_rate(self) -> Optional[float]:
        """
        Duquesne Price to Compare (default supply) — useful as a competing
        "return to DLC default" option, not necessarily your current EGS rate.

        Priority:
          1. PTC printed on the latest bill PDF
          2. Scraped Duquesne residential PTC page
          3. TOU_FLAT_RATE env var (¢/kWh)
        """
        ptc = self._cached_ptc()
        if ptc and ptc > 0:
            return ptc

        if self.tou_flat_override:
            logger.info(
                "Electric PTC fallback: using TOU_FLAT_RATE env var (%.6f $/kWh)",
                self.tou_flat_override,
            )
            return self.tou_flat_override

        return None

    def get_current_gas_rate(self) -> float:
        if PeoplesGasAnalyzer is not None:
            try:
                analyzer = PeoplesGasAnalyzer()
                results = analyzer.run_analysis()
                # Cache so _estimate_monthly_gas_therms() doesn't re-fetch over the network.
                self._last_gas_analysis = results
                try:
                    from usage_archive import GasUsageStore

                    rows = (results or {}).get("usage_data") or []
                    if rows:
                        store = GasUsageStore(self.gas_usage_file)
                        store.merge(rows)
                        store.save()
                except Exception as archive_err:
                    logger.debug("Gas usage archive skipped: %s", archive_err)
                rate_mcf = results.get('current_effective_rate') if results else None
                if rate_mcf and rate_mcf > 0:
                    rate_therm = usd_per_mcf_to_usd_per_therm(rate_mcf)
                    logger.info(
                        "Gas from People's Gas account: %.6f $/MCF → %.6f $/therm (÷%.4f)",
                        rate_mcf,
                        rate_therm,
                        therms_per_mcf(),
                    )
                    return rate_therm
            except Exception as e:
                logger.debug("People's Gas analyzer failed: %s", e)

        if scrape_peoples_gas_rate is not None:
            try:
                r = scrape_peoples_gas_rate()
                if r and r > 0:
                    logger.info("Gas rate from scraped People's Gas page: %.6f $/therm", r)
                    return r
            except Exception as e:
                logger.debug("People's Gas scrape failed: %s", e)

        return self.current_gas_price

    def get_gas_price_to_compare(self) -> Tuple[Optional[float], str, str]:
        """
        Supply-only default-service gas price: (usd_per_unit, unit, source).

        unit is the PA Gas Switch quoting unit for GAS_ID, so offers and PTC compare
        directly. price is None when neither the shop page nor GAS_PRICE_TO_COMPARE
        provides one; unit then falls back to GAS_PRICE_UNIT, or "" if that's unset too.
        """
        if self.gas_zipcode:
            try:
                got = scrape_pagasswitch_price_to_compare(self.gas_id, self.gas_zipcode)
                if got and got[0] > 0:
                    return got[0], got[1], "PA Gas Switch shop page"
            except Exception as e:
                logger.debug("PA Gas Switch PTC scrape failed: %s", e)
        unit = self.gas_price_unit if self.gas_price_unit in GAS_PRICE_UNITS else ""
        if self.gas_price_to_compare > 0:
            return self.gas_price_to_compare, unit, "GAS_PRICE_TO_COMPARE"
        return None, unit, ""

    def estimate_monthly_electric_kwh(self) -> Optional[float]:
        """
        Rough average monthly kWh, used only to normalize a supplier's monthly
        service fee into an equivalent $/kWh so it can be fairly weighed
        against per-kWh price when ranking offers. Uses the persisted hourly
        usage store (no extra network call) when available.
        """
        try:
            store = HourlyUsageStore(self.hourly_usage_file)
        except Exception as e:
            logger.debug("Could not load hourly usage store for fee normalization: %s", e)
            return None

        records = store.all_records()
        if not records:
            return None

        total_kwh = sum(r.kwh for r in records)
        distinct_days = len({r.dt[:10] for r in records})
        if distinct_days <= 0 or total_kwh <= 0:
            return None

        return (total_kwh / distinct_days) * 30.4375

    def estimate_monthly_gas_therms(self) -> Optional[float]:
        """
        Rough average monthly therms, used only to normalize a supplier's
        monthly service fee into an equivalent $/therm for ranking. Reuses the
        analysis already fetched by get_current_gas_rate() when available, to
        avoid a duplicate network round-trip to People's Gas.
        """
        analysis_dict = getattr(self, '_last_gas_analysis', None)
        if analysis_dict is None and PeoplesGasAnalyzer is not None:
            try:
                analysis_dict = PeoplesGasAnalyzer().run_analysis()
                self._last_gas_analysis = analysis_dict
            except Exception as e:
                logger.debug("Could not fetch People's Gas analysis for fee normalization: %s", e)
                return None

        usage_analysis = (analysis_dict or {}).get('usage_analysis')
        monthly_mcf = getattr(usage_analysis, 'average_monthly_usage_mcf', None)
        if not monthly_mcf:
            return None

        return monthly_mcf * therms_per_mcf()

    # ------------------------------------------------------------------
    # Fetching supplier rates
    # ------------------------------------------------------------------

    def fetch_electric_rates(self) -> List[Rate]:
        if not self.electric_id or not self.electric_rate_type:
            logger.error(
                "Set ELECTRIC_ID and ELECTRIC_RATE_TYPE, or ELECTRIC_SHOP_RESULTS_URL — "
                "copy them from your papowerswitch.com shop results URL"
            )
            return []
        try:
            market = PaPowerSwitch(
                self.electric_id,
                self.electric_rate_type,
                service_type=self.electric_service_type,
                zipcode=self.electric_zipcode,
            )
            logger.info("Fetching electric rates: %s", market.params())
            listing = market.fetch_offers()
            rates = listing.offers
            self.last_offers_csv['electric'] = listing.raw

            if self.include_duquesne_as_option:
                ptc = self._cached_ptc()
                if ptc and ptc > 0:
                    rates.append(Rate(
                        price=f"${ptc:.4f}",
                        supplier="Duquesne Light",
                        type="Default / Price to Compare",
                        cancellation_fee="",
                        deposit_required="",
                        discounts_incentives="",
                        introductory_price="",
                        monthly_fee="",
                        more_info="https://duquesnelight.com/service-reliability/service-map/rates/residential-rates",
                        service_type=self.electric_service_type,
                        term_end_date="",
                        term_length="Default",
                        utility_type='electric',
                    ))
                    logger.info("Added Duquesne Light as option at %.4f $/kWh", ptc)

            logger.info("Fetched %d electric rates", len(rates))
            return rates
        except Exception as e:
            logger.error("Error fetching electric rates: %s", e)
            return []

    def fetch_gas_rates(self) -> List[Rate]:
        if not self.gas_id:
            logger.error("Set GAS_ID — the distributor id in your pagasswitch.com shop URL")
            return []
        try:
            market = PaGasSwitch(self.gas_id, service_type=self.gas_service_type)
            logger.info("Fetching gas rates: %s", market.params())
            listing = market.fetch_offers()
            self.last_offers_csv['gas'] = listing.raw
            logger.info("Fetched %d gas rates", len(listing.offers))
            return listing.offers
        except Exception as e:
            logger.error("Error fetching gas rates: %s", e)
            return []

    @staticmethod
    def _only_service_type(rates: List[Rate], service_type: str) -> List[Rate]:
        return only_service_type(rates, service_type)

    def _parse_csv(self, text: str, utility_type: str) -> List[Rate]:
        return parse_offers_csv(text, utility_type)

    # ------------------------------------------------------------------
    # Filtering & ranking
    # ------------------------------------------------------------------

    def apply_filters(self, rates: List[Rate]) -> List[Rate]:
        filtered = rates
        if self.filter_no_cancellation_fee:
            filtered = [r for r in filtered if not r.has_cancellation_fee()]
        if self.filter_renewable_only:
            filtered = [r for r in filtered if r.is_renewable()]
        removed = len(rates) - len(filtered)
        if removed:
            logger.info("Filters removed %d rates (%d remaining)", removed, len(filtered))
        return filtered

    def find_best_rates(
        self,
        rates: List[Rate],
        monthly_usage: Optional[float] = None,
    ) -> List[Rate]:
        """
        Rank offers cheapest-first.

        When `monthly_usage` (kWh or therms, matching the rates' unit) is given,
        ranks by TOTAL cost — per-unit price plus the monthly fee normalized into
        an equivalent per-unit amount — so a low price with a hidden fee doesn't
        beat a slightly higher, fee-free price for your actual usage. Without a
        usage estimate, falls back to ranking by price alone (and warns if any
        offer actually carries a fee, since that ranking could then be wrong).
        """
        valid = [
            r for r in rates
            if r.get_numeric_price() is not None and r.get_numeric_price() > 0
        ]

        if monthly_usage and monthly_usage > 0:
            def total_unit_cost(r: Rate) -> float:
                return r.get_numeric_price() + r.get_numeric_monthly_fee() / monthly_usage

            return sorted(valid, key=total_unit_cost)

        if any(r.get_numeric_monthly_fee() > 0 for r in valid):
            logger.warning(
                "Ranking rates by price only — no usage estimate available to fold in monthly "
                "fees, and at least one offer has a nonzero fee. Ranking may not reflect true cost."
            )
        return sorted(valid, key=lambda r: r.get_numeric_price())

    def calculate_savings(self, current: float, new: float) -> Dict:
        if current == 0:
            return {'percentage': 0, 'amount': 0, 'message': 'Current price not set'}
        amount = current - new
        pct = (amount / current) * 100
        return {
            'percentage': pct,
            'amount': amount,
            'message': f"{pct:.2f}% savings (${amount:.4f} per unit)",
        }

    def check_for_savings(
        self,
        rates: List[Rate],
        current_price: float,
        utility_type: str,
        supply_reference_price: Optional[float] = None,
        monthly_usage: Optional[float] = None,
    ) -> List[Dict]:
        """
        Compare supplier offers (supply-only $/unit) against the current rate.

        `current_price` is your blended all-in rate (used for display and as
        the comparison basis when `supply_reference_price` is unavailable).
        `supply_reference_price`, when provided, is the fair apples-to-apples
        basis (e.g. Duquesne's Price to Compare): distribution/transmission/riders
        don't change when you switch suppliers, so the real threshold check and
        the "true" projected savings should use the supply-only delta, not the
        blended rate.
        `monthly_usage` (kWh or therms, matching `rates`' unit), when given, lets
        `find_best_rates` rank by total cost (price + normalized monthly fee)
        instead of price alone.
        """
        filtered = self.apply_filters(rates)
        if not filtered:
            logger.warning("No rates remain after filtering for %s", utility_type)
            return []

        ranked = self.find_best_rates(filtered, monthly_usage=monthly_usage)
        if not ranked:
            return []

        best = ranked[0]
        best_price = best.get_numeric_price()
        best_monthly_fee = best.get_numeric_monthly_fee()

        comparison_basis = supply_reference_price if supply_reference_price else current_price
        savings = self.calculate_savings(comparison_basis, best_price)

        # Estimate the effect on your actual (blended) bill: swap only the
        # supply portion, holding distribution/transmission/riders constant.
        blended_savings = None
        if supply_reference_price and current_price:
            non_supply_portion = current_price - supply_reference_price
            estimated_new_blended = non_supply_portion + best_price
            blended_savings = self.calculate_savings(current_price, estimated_new_blended)

        if savings['percentage'] >= self.notification_threshold:
            opp = {
                'utility_type': utility_type,
                'current_price': current_price,
                'supply_reference_price': supply_reference_price,
                'best_price': best_price,
                'best_monthly_fee': best_monthly_fee,
                'best_supplier': best.supplier,
                'best_rate_type': best.type,
                'term_length': best.term_length,
                'cancellation_fee': best.cancellation_fee,
                'deposit_required': best.deposit_required,
                'more_info': best.more_info,
                'savings': savings,
                'blended_savings': blended_savings,
                'monthly_usage_used_for_ranking': monthly_usage,
                'timestamp': datetime.now().isoformat(),
            }
            logger.info("Found %s savings opportunity: %s", utility_type, savings['message'])
            return [opp]

        return []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_previous_rates(self) -> Dict:
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Error loading previous rates: %s", e)
        return {}

    def save_rates(self, electric_rates: List[Rate], gas_rates: List[Rate]):
        """Persist latest CSV dump. History is appended by recommendation_engine.save_snapshot."""
        try:
            from recommendation_engine import load_snapshot_file
            os.makedirs(os.path.dirname(self.storage_file) or '.', exist_ok=True)
            existing = load_snapshot_file(self.storage_file)
            existing["latest"] = {
                'electric': [asdict(r) for r in electric_rates],
                'gas': [asdict(r) for r in gas_rates],
                'timestamp': datetime.now().isoformat(),
            }
            existing.setdefault("history", [])
            with open(self.storage_file, 'w') as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.error("Error saving rates: %s", e)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def send_report_email(self, report, force: bool = False) -> None:
        """
        One multipart (plain + HTML) email for the unified check report.

        With EMAIL_MODE=change (default) it is skipped when the findings match the last
        email sent; force=True sends anyway.
        """
        if not all([self.smtp_host, self.smtp_username, self.smtp_password, self.notify_to]):
            logger.debug("Email not configured — skipping notification")
            return
        if not report.should_email():
            return
        if not force and not _email_state_changed(report):
            logger.info("No email: findings unchanged since the last email (EMAIL_MODE=change)")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = self.notify_from
            msg["To"] = self.notify_to
            msg["Subject"] = report.email_subject()
            msg.attach(MIMEText(report.format_text(), "plain"))
            msg.attach(MIMEText(report.format_html(), "html"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(msg)

            logger.info("Email sent to %s", self.notify_to)
            _record_email_state(report)
        except Exception as e:
            logger.error("Failed to send email: %s", e)

    def format_notification(self, opportunities: List[Dict]) -> str:
        """Legacy helper; the daily job uses CheckReport.format_text()."""
        if not opportunities:
            return "No significant savings opportunities found."
        lines = ["PA Utility Savings Opportunities Found!\n"]
        for opp in opportunities:
            label = "ELECTRIC" if opp['utility_type'] == 'electric' else "GAS"
            unit = "kWh" if opp['utility_type'] == 'electric' else "therm"
            lines += [
                f"--- {label} ---",
                f"  Current rate : ${opp['current_price']:.4f}/{unit}",
                f"  Best rate    : ${opp['best_price']:.4f}/{unit}",
                f"  Supplier     : {opp.get('best_supplier', '')}",
                "",
            ]
        return "\n".join(lines)

    def send_email(self, opportunities: List[Dict]):
        """Legacy wrapper used by older scripts; prefer send_report_email."""
        if not opportunities:
            return
        body = self.format_notification(opportunities)
        if not all([self.smtp_host, self.smtp_username, self.smtp_password, self.notify_to]):
            logger.debug("Email not configured — skipping notification")
            return
        try:
            msg = MIMEMultipart()
            msg['From'] = self.notify_from
            msg['To'] = self.notify_to
            msg['Subject'] = f"PA Utility Savings Alert — {len(opportunities)} opportunit{'y' if len(opportunities) == 1 else 'ies'} found"
            msg.attach(MIMEText(body, 'plain'))
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(msg)
            logger.info("Email sent to %s", self.notify_to)
            # No _record_email_state here: that fingerprints the report for EMAIL_MODE=change,
            # and this legacy email has no report (the call raised NameError after sending,
            # which logged a sent email as "Failed to send email").
        except Exception as e:
            logger.error("Failed to send email: %s", e)

    # ------------------------------------------------------------------
    # TOU analysis
    # ------------------------------------------------------------------

    def _get_tou_rates(self, current_electric_rate: float):
        """
        Resolve TOU rates using (in priority order):
          1. Scraped from the latest Duquesne Light DSS tariff PDF (cached on disk).
          2. Manual env var overrides (TOU_PEAK_RATE, TOU_OFF_PEAK_RATE, etc.).
          3. None — TOU analysis is skipped.
        """
        # Try PDF scraper / cache
        if scrape_duquesne_tou_rates is not None:
            try:
                rates = scrape_duquesne_tou_rates(
                    cache_path=self.tou_rates_cache,
                    pdf_url_override=self.tou_pdf_url or None,
                )
                if rates is not None:
                    return rates
            except Exception as e:
                logger.debug("TOU rate scraper failed: %s", e)

        # Fall back to manual env var overrides
        if all(v is not None for v in [
            self.tou_peak_override,
            self.tou_off_peak_override,
            self.tou_super_off_peak_override,
        ]):
            flat = (
                self.tou_flat_override
                or self._cached_ptc()
                or current_electric_rate
            )
            logger.info("Using manually configured TOU rates from env vars")
            return TOURates(
                standard_rate=flat,
                peak_rate=self.tou_peak_override,
                off_peak_rate=self.tou_off_peak_override,
                super_off_peak_rate=self.tou_super_off_peak_override,
                effective_start='',
                effective_end='9999-12-31',
                source='env vars',
            )

        return None

    def run_tou_analysis(self, current_electric_rate: float) -> Optional[str]:
        """
        Fetch latest hourly usage, merge into the persistent store, then
        evaluate TOU vs flat rate using all stored history.

        Returns a formatted report string, or None if analysis could not run.
        """
        tou_rates = self._get_tou_rates(current_electric_rate)
        if tou_rates is None:
            logger.info(
                "TOU rates unavailable (PDF scraper failed and no env var overrides set). "
                "Set TOU_PEAK_RATE, TOU_OFF_PEAK_RATE, TOU_SUPER_OFF_PEAK_RATE in ¢/kWh to enable."
            )
            return None

        # Update hourly usage store from Duquesne API
        store = HourlyUsageStore(self.hourly_usage_file)
        if DuquesneLightAnalyzer is not None:
            try:
                analyzer = DuquesneLightAnalyzer()
                new_records = analyzer.fetch_hourly_data()
                if new_records:
                    added = store.merge(new_records)
                    store.save()
                    logger.info(
                        "Hourly store: %d records total (added %d new)",
                        len(store), added,
                    )
            except Exception as e:
                logger.warning("Could not update hourly usage store: %s", e)

        all_records = store.all_records()
        if not all_records:
            logger.info(
                "No hourly usage data stored yet. "
                "Provide DUQUESNE_USERNAME and DUQUESNE_PASSWORD to populate historical data."
            )
            return None

        result = TOUAnalyzer().analyze(all_records, tou_rates)
        if result is None:
            return None

        report = TOUAnalyzer().format_report(result, tou_rates)
        return report

    # ------------------------------------------------------------------
    # Main check cycle
    # ------------------------------------------------------------------

    def run_check(self):
        """Collect rates + usage, build one report, print it, email if needed."""
        from recommendation_engine import build_check_report

        logger.info("=== Starting rate check ===")
        self._ptc_resolved = False
        self._ptc = None
        self._egs_bill_resolved = False
        self._last_egs_bill = None
        self._last_electric_analysis = None
        self._last_gas_analysis = None
        from recommendation_engine import report_failed_saves
        from usage_archive import take_failed_saves

        take_failed_saves()  # only this run's
        report = build_check_report(self)
        report_failed_saves(report)
        text = report.format_text()
        print("\n" + "=" * 72)
        print(text)
        print("=" * 72 + "\n")
        try:
            from usage_archive import save_report

            save_report(text, report.format_html())
        except Exception as e:
            logger.warning("Could not archive report: %s", e)

        if report.should_email():
            self.send_report_email(report)
        else:
            logger.info(
                "No email: no switch above %.1f%%, TOU not recommended, no error-level warnings",
                self.notification_threshold,
            )
        return report


def main():
    checker = RateChecker()
    interval_hours = float(os.getenv('CHECK_INTERVAL_HOURS', '24'))

    while True:
        checker.run_check()

        if interval_hours <= 0:
            logger.info("CHECK_INTERVAL_HOURS=0 — running once and exiting")
            break

        next_run = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        logger.info("Next check in %.1f hours (set CHECK_INTERVAL_HOURS=0 to run once)", interval_hours)
        time.sleep(interval_hours * 3600)


if __name__ == '__main__':
    main()

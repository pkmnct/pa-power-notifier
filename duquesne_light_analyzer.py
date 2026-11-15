#!/usr/bin/env python3
"""
Duquesne Light for the standalone notifier: energy_shopper_core.utilities.duquesne plus
credentials from the environment, the Puppeteer browser path, an offline file mode, and
bill archiving.

Env:
  DUQUESNE_USERNAME / DUQUESNE_PASSWORD      duquesnelight.com login
  DUQUESNE_CUSTOMER_UUID / _URN, DUQUESNE_UTILITY_ACCOUNT_UUID
                                             Opower identifiers (auto-discovered if unset)
  DUQUESNE_USE_PUPPETEER=1                   always try the browser for the token page
  DUQUESNE_USE_FILE=true                     read DUQUESNE_RESPONSE_FILE instead of the API
  DUQUESNE_RESPONSE_FILE                     latest raw daily response (default data/duquesne_response.json)
  DUQUESNE_BILL_PDF                          parse this bill PDF instead of downloading the latest
  HOURLY_LOOKBACK_DAYS                       hourly backfill depth (default 400)
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from energy_shopper_core.utilities import duquesne as _core
from energy_shopper_core.utilities.duquesne import (  # noqa: F401  (re-exported)
    CurrentEgsFromBill,
    DuquesneAuthError,
    DuquesneBlockedError,
    DuquesneClient,
    DuquesneError,
    RateInfo,
    UsageAnalysis,
    UsageData,
)

logger = logging.getLogger(__name__)

try:
    from browser_fetch import (
        fetch_html_via_puppeteer,
        fetch_duquesne_bill_history_via_puppeteer,
        fetch_duquesne_bill_pdf_via_puppeteer,
        puppeteer_eager_enabled,
    )
except ImportError:
    fetch_html_via_puppeteer = None  # type: ignore
    fetch_duquesne_bill_history_via_puppeteer = None  # type: ignore
    fetch_duquesne_bill_pdf_via_puppeteer = None  # type: ignore
    puppeteer_eager_enabled = lambda: False  # type: ignore


def client_from_env() -> DuquesneClient:
    """A core client with env credentials and the Puppeteer fetcher for bot-walled pages."""
    return DuquesneClient(
        os.getenv('DUQUESNE_USERNAME') or "",
        os.getenv('DUQUESNE_PASSWORD') or "",
        customer_uuid=os.getenv('DUQUESNE_CUSTOMER_UUID'),
        utility_account_uuid=os.getenv('DUQUESNE_UTILITY_ACCOUNT_UUID'),
        customer_urn=os.getenv('DUQUESNE_CUSTOMER_URN'),
        html_fetcher=fetch_html_via_puppeteer,
        prefer_browser=puppeteer_eager_enabled(),
    )


class DuquesneLightAnalyzer:
    """Analyzes Duquesne Light usage and rates; failures are logged and give empty results."""

    _MAX_HOURLY_DAYS = _core.MAX_HOURLY_DAYS
    _DEFAULT_HOURLY_LOOKBACK_DAYS = _core.DEFAULT_HOURLY_LOOKBACK_DAYS

    def __init__(self, client: Optional[DuquesneClient] = None):
        from env_bootstrap import data_dir

        self.api_url = _core.GRAPHQL_URL
        self.client = client or client_from_env()
        # Latest raw Opower response (debugging), and the input for DUQUESNE_USE_FILE
        self.response_file = os.getenv(
            'DUQUESNE_RESPONSE_FILE', str(data_dir() / 'duquesne_response.json')
        )
        self.use_file = os.getenv('DUQUESNE_USE_FILE', 'false').lower() == 'true'

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _ensure_authenticated(self) -> bool:
        try:
            self.client.ensure_authenticated()
            return True
        except DuquesneAuthError as exc:
            logger.warning("%s (set DUQUESNE_USERNAME and DUQUESNE_PASSWORD)", exc)
        except DuquesneError as exc:
            logger.error("%s", exc)
        return False

    def _has_account_ids(self) -> bool:
        return bool(self.client.customer_urn and self.client.utility_account_uuid)

    # ------------------------------------------------------------------
    # Daily usage
    # ------------------------------------------------------------------

    def fetch_usage_data(self, time_interval: Optional[str] = None) -> Dict:
        """
        Daily GraphQL response (empty dict on failure). time_interval is an ISO 8601
        interval, e.g. "2025-10-27T00:00:00-04:00/2025-12-15T00:00:00-05:00";
        defaults to the last 50 days.
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
        if not self._has_account_ids():
            logger.warning(
                "Account identifiers missing after auth. "
                "Set DUQUESNE_CUSTOMER_UUID and DUQUESNE_UTILITY_ACCOUNT_UUID."
            )
            return {}
        try:
            data = self.client.fetch_daily_response(time_interval)
        except DuquesneError as exc:
            logger.error("%s", exc)
            return {}

        if self.response_file:
            try:
                with open(self.response_file, 'w') as f:
                    json.dump(data, f, indent=2)
                logger.info("Saved response to %s", self.response_file)
            except Exception as exc:
                logger.warning("Could not save response file: %s", exc)
        return data

    parse_rates = staticmethod(_core.parse_rates)
    parse_rate_plan_label = staticmethod(_core.parse_rate_plan_label)
    parse_usage = staticmethod(_core.parse_usage)
    get_peak_demand = staticmethod(_core.parse_peak_demand)
    analyze_usage = staticmethod(_core.analyze_usage)
    calculate_projected_cost = staticmethod(_core.calculate_projected_cost)

    def get_current_effective_rate(self, data: Dict) -> float:
        usage_data = self.parse_usage(data)
        if not usage_data:
            return 0.0
        return self.analyze_usage(usage_data).average_effective_rate

    def run_analysis(self, time_interval: Optional[str] = None) -> Dict:
        """
        Fetch and analyze daily usage: rates, usage_data, usage_analysis,
        current_effective_rate, peak_demand, rate_plan_label ({} without data).
        """
        logger.info("Starting Duquesne Light analysis...")
        return _core.analyze_response(self.fetch_usage_data(time_interval))

    # ------------------------------------------------------------------
    # Hourly usage
    # ------------------------------------------------------------------

    def fetch_hourly_data(self, days: int = 29, end_date: Optional[datetime] = None) -> List:
        """One HOUR-resolution window (max 29 days) ending at end_date (default now)."""
        if self.use_file:
            logger.info("DUQUESNE_USE_FILE=true — skipping hourly API fetch")
            return []
        if not self._ensure_authenticated():
            logger.warning("Duquesne Light authentication failed — skipping hourly fetch")
            return []
        if not self._has_account_ids():
            logger.debug("Duquesne account identifiers missing; skipping hourly fetch")
            return []
        return self.client.fetch_hourly(days, end_date)

    def fetch_hourly_history(self, lookback_days: Optional[int] = None) -> List:
        """Page backward in ≤29-day windows to pull the AMI history Opower still has."""
        lookback = lookback_days
        if lookback is None:
            lookback = int(os.getenv("HOURLY_LOOKBACK_DAYS", str(self._DEFAULT_HOURLY_LOOKBACK_DAYS)))
        if self.use_file:
            return []
        if not self._ensure_authenticated():
            logger.warning("Duquesne Light authentication failed — skipping hourly history")
            return []
        if not self._has_account_ids():
            return []
        return self.client.fetch_hourly_history(lookback)

    def update_hourly_store(self, store, backfill: bool = True) -> int:
        """Fetch latest + optional historical hourly windows and merge into store; returns new records."""
        records = self.fetch_hourly_history() if backfill else self.fetch_hourly_data()
        if records:
            return store.merge(records)
        return 0

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------

    parse_egs_from_bill_text = staticmethod(_core.parse_egs_from_bill_text)
    parse_bill_meta = staticmethod(_core.parse_bill_meta)
    _bill_pdf_text = staticmethod(_core.pdf_text)

    def _billing_session(self):
        try:
            return self.client.billing_session()
        except DuquesneError as exc:
            logger.error("DLC login failed: %s", exc)
            return None

    def backfill_bill_archive(self, archive) -> int:
        """
        Download every Duquesne bill not yet in `archive` (a usage_archive.BillArchive)
        and record each bill's supplier, so supplier history survives Duquesne dropping
        old bills. Returns the number of bills added.
        """
        if fetch_duquesne_bill_history_via_puppeteer is None:
            return 0
        session = self._billing_session()
        if session is None:
            return 0

        import tempfile

        added = 0
        with tempfile.TemporaryDirectory() as tmp:
            rows = fetch_duquesne_bill_history_via_puppeteer(session, tmp, skip_ids=archive.known_ids())
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
        path = pdf_path or os.getenv("DUQUESNE_BILL_PDF", "").strip()
        meta_bill_id = bill_id or ""
        if not path:
            if fetch_duquesne_bill_pdf_via_puppeteer is None:
                logger.warning("browser_fetch bill PDF helper unavailable")
                return None
            session = self._billing_session()
            if session is None:
                return None

            from env_bootstrap import data_dir as _data_dir

            data_dir = _data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            path = str(data_dir / "latest_bill.pdf")
            meta = fetch_duquesne_bill_pdf_via_puppeteer(session, path, bill_id=bill_id)
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

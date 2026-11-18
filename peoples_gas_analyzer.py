#!/usr/bin/env python3
"""
People's Gas for the standalone notifier: energy_shopper_core.utilities.peoples_gas plus
credentials from the environment, an offline file mode, and bill archiving.

Env:
  PEOPLES_GAS_USERNAME / PEOPLES_GAS_PASSWORD  peopleseaccount.com login
  PEOPLES_GAS_USE_FILE=true                    read a saved LoadGasUsage response instead
  PEOPLES_GAS_RESPONSE_FILE                    that response (default gasResponse.json)
"""

import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

from energy_shopper_core.utilities import peoples_gas as _core
from energy_shopper_core.utilities.peoples_gas import (  # noqa: F401  (re-exported)
    BASE_URL,
    METER_READ_TYPES,
    GasUsageAnalysis,
    GasUsageData,
    PeoplesGasAuthError,
    analyze,
    bill_rows,
    calculate_projected_cost,
    cross_reference as _cross_reference,
    parse_amount as _parse_amount,
    parse_bill_pdf_text,
    parse_billing_history as _parse_billing_history,
    parse_compare_spending_previous as _parse_compare_spending_previous,
    parse_usage_response as _parse_usage_response,
    pdf_text as _pdf_text,
)

logger = logging.getLogger(__name__)


class PeoplesGasClient(_core.PeoplesGasClient):
    """The core client, with credentials defaulting to PEOPLES_GAS_USERNAME / _PASSWORD."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        super().__init__(
            username or os.environ.get("PEOPLES_GAS_USERNAME", ""),
            password or os.environ.get("PEOPLES_GAS_PASSWORD", ""),
        )

    def login(self) -> bool:
        if not self.username or not self.password:
            raise ValueError(
                "Credentials required. Set PEOPLES_GAS_USERNAME and PEOPLES_GAS_PASSWORD."
            )
        return super().login()


class PeoplesGasAnalyzer:
    """
    High-level interface that fetches, merges, and analyzes People's Gas data.

    Credentials:
      Pass username/password directly, or set:
        PEOPLES_GAS_USERNAME / PEOPLES_GAS_PASSWORD env vars.

    File-based mode (for testing without live credentials):
      Set PEOPLES_GAS_USE_FILE=true and provide PEOPLES_GAS_RESPONSE_FILE
      pointing to a saved LoadGasUsage JSON response.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.client = PeoplesGasClient(username, password)
        self._use_file = os.environ.get("PEOPLES_GAS_USE_FILE", "false").lower() == "true"
        self._response_file = os.environ.get("PEOPLES_GAS_RESPONSE_FILE", "gasResponse.json")

    def fetch_all_usage(self) -> List[GasUsageData]:
        if self._use_file:
            return self._usage_from_file()
        return _core.fetch_all_usage(self.client)

    def fetch_billing_history(self) -> List[Dict]:
        """Fetch all available bills going back as far as the account allows."""
        if self._use_file:
            return []
        bills = _parse_billing_history(self.client.fetch_billing_history())
        logger.info("Fetched %d billing records", len(bills))
        return bills

    def backfill_bill_archive(self, archive) -> int:
        """
        Download every People's Gas bill PDF not yet in `archive` (a usage_archive.BillArchive)
        with the commodity rate, Price to Compare and Mcf parsed from it. Returns bills added.
        """
        if self._use_file:
            return 0
        known = set(archive.known_ids())
        added = 0
        for row in bill_rows(self.client.fetch_billing_history()):
            invoice = str(row.get("InvoiceNumber") or "")
            if invoice in known:
                continue
            pdf = self.client.fetch_bill_pdf(row)
            if not pdf:
                continue
            details = parse_bill_pdf_text(_pdf_text(pdf))
            details["amount_usd"] = _parse_amount(row.get("Amount"))
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
                tf.write(pdf)
                tf.flush()
                saved = archive.archive_pdf(
                    tf.name,
                    invoice,
                    bill_date=(row.get("DBDate") or "")[:10],
                    supplier=details.pop("supplier", ""),
                    details=details,
                    update_latest=False,
                )
            if saved:  # not counted when the save failed (it's recorded and reported)
                added += 1
        if added:
            logger.info("Archived %d People's Gas bill PDFs", added)
        return added

    def enrich_with_bills(self, usage: List[GasUsageData]) -> List[GasUsageData]:
        """Cross-reference usage records with bill amounts to compute effective rates."""
        return _cross_reference(usage, self.fetch_billing_history())

    def analyze(self, usage: List[GasUsageData]) -> GasUsageAnalysis:
        return analyze(usage)

    def calculate_projected_cost(self, analysis: GasUsageAnalysis, rate_per_mcf: float) -> Dict:
        return calculate_projected_cost(analysis, rate_per_mcf)

    def run_analysis(self) -> Dict:
        """Convenience method: fetch everything, enrich, analyze, return results."""
        logger.info("Starting People's Gas analysis…")
        usage = self.fetch_all_usage()
        usage = self.enrich_with_bills(usage)
        analysis = self.analyze(usage)
        return {
            "usage_data": usage,
            "usage_analysis": analysis,
            "current_effective_rate": analysis.average_effective_rate_per_mcf,
        }

    # ------------------------------------------------------------------
    # File-based fallback
    # ------------------------------------------------------------------

    def _usage_from_file(self) -> List[GasUsageData]:
        if not os.path.exists(self._response_file):
            logger.error("Response file not found: %s", self._response_file)
            return []
        try:
            with open(self._response_file) as f:
                raw = json.load(f)
            return _parse_usage_response(raw)
        except Exception as exc:
            logger.error("Error loading response file: %s", exc)
            return []


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    try:
        from env_bootstrap import load_dotenv_if_present

        load_dotenv_if_present()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    analyzer = PeoplesGasAnalyzer()
    results = analyzer.run_analysis()

    if not results:
        print("No data retrieved.")
        return

    analysis: GasUsageAnalysis = results["usage_analysis"]

    print("\n" + "=" * 65)
    print("People's Gas Usage Analysis")
    print("=" * 65)
    print(f"Period:               {analysis.date_range[0]}  →  {analysis.date_range[1]}")
    print(f"Months analyzed:      {analysis.months_analyzed}")
    print(f"Total usage:          {analysis.total_usage_mcf:.1f} MCF")
    print(f"Average monthly:      {analysis.average_monthly_usage_mcf:.1f} MCF")
    if analysis.peak_monthly_usage_mcf:
        print(f"Peak monthly:         {analysis.peak_monthly_usage_mcf:.1f} MCF")

    if analysis.average_monthly_cost:
        print(f"Avg monthly bill:     ${analysis.average_monthly_cost:.2f}")
        print(f"Projected annual:     ${analysis.average_monthly_cost * 12:.2f}")

    if analysis.average_effective_rate_per_mcf:
        print(f"Effective rate:       ${analysis.average_effective_rate_per_mcf:.4f}/MCF")
    else:
        print("Effective rate:       N/A (billing data not available)")

    print("\nMonth-by-month breakdown:")
    print(f"  {'Period':<22} {'MCF':>6}  {'Bill':>8}  {'$/MCF':>8}  {'Read Type'}")
    print("  " + "-" * 60)
    for u in sorted(results["usage_data"], key=lambda x: (x.year, x.month)):
        bill_str = f"${u.bill_amount:.2f}" if u.bill_amount is not None else "—"
        rate_str = f"${u.effective_rate:.4f}" if u.effective_rate is not None else "—"
        read_label = METER_READ_TYPES.get(u.meter_read_type, u.meter_read_type)
        period = f"{u.from_date} – {u.to_date}" if u.from_date else u.date
        print(f"  {period:<22} {u.usage_mcf:>6.1f}  {bill_str:>8}  {rate_str:>8}  {read_label}")

    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()

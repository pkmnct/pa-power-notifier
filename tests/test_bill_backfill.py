"""Bill backfills count only the bills the archive actually saved."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duquesne_light_analyzer as dla  # noqa: E402
import peoples_gas_analyzer as pga  # noqa: E402


class FakeArchive:
    """archive_pdf succeeds for B1 and fails (returns None, as on a full disk) for B2."""

    def __init__(self):
        self.calls = []

    def known_ids(self):
        return []

    def archive_pdf(self, src, bill_id, **kw):
        self.calls.append(bill_id)
        return f"/bills/{bill_id}.pdf" if bill_id == "B1" else None

    def fill_bill_meta(self, *a, **kw):
        pass


class BackfillCountTests(unittest.TestCase):
    def test_duquesne_counts_only_saved_bills(self):
        analyzer = dla.DuquesneLightAnalyzer.__new__(dla.DuquesneLightAnalyzer)  # no env, no login
        rows = [{"billId": b, "pdfPath": f"/tmp/{b}.pdf", "date": "2026-10-01"} for b in ("B1", "B2")]
        archive = FakeArchive()
        with mock.patch.object(dla, "fetch_duquesne_bill_history_via_puppeteer", return_value=rows), \
                mock.patch.object(dla.DuquesneLightAnalyzer, "_billing_session", return_value=object()), \
                mock.patch.object(dla.DuquesneLightAnalyzer, "_bill_pdf_text", return_value="text"), \
                mock.patch.object(dla.DuquesneLightAnalyzer, "parse_bill_meta", return_value={"bill_date": ""}), \
                mock.patch.object(dla.DuquesneLightAnalyzer, "parse_egs_from_bill_text", return_value=None):
            added = analyzer.backfill_bill_archive(archive)
        self.assertEqual(archive.calls, ["B1", "B2"])
        self.assertEqual(added, 1)

    def test_peoples_gas_counts_only_saved_bills(self):
        analyzer = pga.PeoplesGasAnalyzer.__new__(pga.PeoplesGasAnalyzer)
        analyzer._use_file = False
        analyzer.client = mock.Mock()
        analyzer.client.fetch_bill_pdf.return_value = b"%PDF-1.4 test"
        rows = [{"InvoiceNumber": b, "DBDate": "2026-10-01T00:00:00", "Amount": "$10.00"} for b in ("B1", "B2")]
        archive = FakeArchive()
        with mock.patch.object(pga, "bill_rows", return_value=rows), \
                mock.patch.object(pga, "_pdf_text", return_value=""), \
                mock.patch.object(pga, "parse_bill_pdf_text", return_value={}):
            added = analyzer.backfill_bill_archive(archive)
        self.assertEqual(archive.calls, ["B1", "B2"])
        self.assertEqual(added, 1)


if __name__ == "__main__":
    unittest.main()

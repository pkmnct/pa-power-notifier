"""Archive files: atomic writes, and never saving over a file that couldn't be read."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from energy_shopper_core.tou import HourlyUsageRecord  # noqa: E402
from usage_archive import (  # noqa: E402
    BillArchive,
    DailyElectricUsageStore,
    GasUsageStore,
    HourlyUsageStore,
    write_json_atomic,
)

HALF_WRITTEN = '{"records": [{"dt": "2026-11-01T00:00:00-04:00", "kwh": 1.5'


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _half_written(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(HALF_WRITTEN)
        return path

    def test_unreadable_hourly_store_is_not_saved_over(self):
        path = self._half_written("hourly_usage.json")
        store = HourlyUsageStore(path)
        store.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        store.save()
        with open(path) as f:
            self.assertEqual(f.read(), HALF_WRITTEN)  # history it couldn't read is still there

    def test_unreadable_daily_and_gas_stores_are_not_saved_over(self):
        daily = self._half_written("daily_usage.json")
        store = DailyElectricUsageStore(daily)
        store.merge([{"date": "2026-11-02", "kwh": 20.0, "cost": 5.0}])
        store.save()
        gas = self._half_written("gas_usage.json")
        gstore = GasUsageStore(gas)
        gstore.merge([{"usage_mcf": 5.0, "month": 11, "year": 2026, "to_date": "11/01/2026"}])
        gstore.save()
        for path in (daily, gas):
            with open(path) as f:
                self.assertEqual(f.read(), HALF_WRITTEN)

    def test_unreadable_bill_index_is_not_saved_over(self):
        os.makedirs(os.path.join(self.dir, "bills"))
        index = os.path.join(self.dir, "bills", "index.json")
        with open(index, "w") as f:
            f.write(HALF_WRITTEN)
        archive = BillArchive(os.path.join(self.dir, "bills"))
        archive._save_index()  # noqa: SLF001
        with open(index) as f:
            self.assertEqual(f.read(), HALF_WRITTEN)

    def test_refused_save_says_so(self):
        path = self._half_written("hourly_usage.json")
        store = HourlyUsageStore(path)
        self.assertTrue(store.load_failed)
        store.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        with self.assertLogs("usage_archive", level="ERROR") as logs:
            self.assertIs(store.save(), False)  # a caller can tell nothing was archived
        self.assertIn("Not saving", "\n".join(logs.output))
        fine = HourlyUsageStore(os.path.join(self.dir, "other.json"))
        fine.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        self.assertIs(fine.save(), True)

    def test_notifier_report_says_when_a_save_was_refused(self):
        """A notifier-only setup must hear about it: an error in the report, so it's emailed."""
        from energy_shopper_core.report import CheckReport
        from recommendation_engine import report_failed_saves
        from usage_archive import take_failed_saves

        take_failed_saves()  # start clean
        hourly = self._half_written("hourly_usage.json")
        store = HourlyUsageStore(hourly)
        store.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        with self.assertLogs("usage_archive", level="ERROR"):
            store.save()
        os.makedirs(os.path.join(self.dir, "bills"))
        index = os.path.join(self.dir, "bills", "index.json")
        with open(index, "w") as f:
            f.write(HALF_WRITTEN)
        with self.assertLogs("usage_archive", level="ERROR"):
            BillArchive(os.path.join(self.dir, "bills"))._save_index()  # noqa: SLF001

        report = CheckReport()
        with self.assertLogs(level="ERROR"):
            report_failed_saves(report)
        errors = [w for w in report.warnings if w.level == "error" and w.source == "archive"]
        self.assertEqual(len(errors), 2)
        self.assertIn(hourly, errors[1].message + errors[0].message)
        self.assertIn(index, errors[1].message + errors[0].message)
        self.assertTrue(report.should_email())

        later = CheckReport()
        report_failed_saves(later)  # the next run starts from nothing
        self.assertEqual(later.warnings, [])

    def test_notifier_report_says_when_a_write_failed(self):
        """A failed write (disk full, permissions) is reported too, with advice that fits it."""
        from unittest import mock

        import usage_archive
        from energy_shopper_core.report import CheckReport
        from recommendation_engine import report_failed_saves
        from usage_archive import take_failed_saves

        take_failed_saves()
        path = os.path.join(self.dir, "hourly_usage.json")
        store = HourlyUsageStore(path)
        store.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        with mock.patch.object(usage_archive, "write_text_atomic", side_effect=OSError(28, "No space left on device")):
            with self.assertLogs("usage_archive", level="ERROR"):
                self.assertIs(store.save(), False)
        self.assertFalse(store.load_failed)
        report = CheckReport()
        report_failed_saves(report)
        (error,) = [w for w in report.warnings if w.source == "archive"]
        self.assertEqual(error.level, "error")
        self.assertIn(path, error.message)
        self.assertIn("writing it failed", error.message)
        self.assertNotIn("moving it aside starts", error.message)  # that advice is for unreadable files
        self.assertIn("don't move the file aside", error.message)

    def test_bill_archive_write_failures_are_recorded(self):
        """The bill index and PDF copies: callers swallow the error, so it must be recorded."""
        from unittest import mock

        import usage_archive
        from usage_archive import WRITE_FAILED, take_failed_saves

        take_failed_saves()
        src = os.path.join(self.dir, "bill.pdf")
        with open(src, "wb") as f:
            f.write(b"%PDF-1.4 test")
        bills = os.path.join(self.dir, "bills")
        archive = BillArchive(bills)
        with mock.patch.object(usage_archive, "write_text_atomic", side_effect=OSError(13, "Permission denied")):
            with self.assertLogs("usage_archive", level="ERROR"):
                archive.archive_pdf(src, "B1", bill_date="2026-11-01", update_latest=False)
        self.assertEqual(take_failed_saves(), [(os.path.join(bills, "index.json"), WRITE_FAILED)])

        with mock.patch.object(usage_archive.shutil, "copy2", side_effect=OSError(28, "No space left on device")):
            with self.assertLogs("usage_archive", level="ERROR"):
                self.assertIsNone(archive.archive_pdf(src, "B2", update_latest=False))
        self.assertEqual(take_failed_saves(), [(os.path.join(bills, "B2.pdf"), WRITE_FAILED)])

    def test_a_bug_is_not_reported_as_a_disk_problem(self):
        from unittest import mock

        import usage_archive
        from usage_archive import INTERNAL_ERROR, save_failure_advice, take_failed_saves

        take_failed_saves()
        path = os.path.join(self.dir, "hourly_usage.json")
        store = HourlyUsageStore(path)
        store.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        with mock.patch.object(usage_archive, "write_json_atomic", side_effect=TypeError("Decimal is not JSON serializable")):
            with self.assertLogs("usage_archive", level="ERROR"):
                self.assertIs(store.save(), False)
        ((failed_path, cause),) = take_failed_saves()
        self.assertEqual((failed_path, cause), (path, INTERNAL_ERROR))
        advice = save_failure_advice(failed_path, cause)
        self.assertIn("internal error", advice)
        self.assertNotIn("disk full", advice)

    def test_write_is_flushed_to_disk_before_the_rename(self):
        """Otherwise a power cut soon after a save can leave the renamed file empty."""
        import usage_archive

        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def replace(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        usage_archive.os.fsync, usage_archive.os.replace = fsync, replace
        try:
            write_json_atomic(os.path.join(self.dir, "x.json"), {"a": 1})
        finally:
            usage_archive.os.fsync, usage_archive.os.replace = real_fsync, real_replace
        self.assertEqual(events[:2], ["fsync", "replace"])  # the data, then the rename
        self.assertIn("fsync", events[2:])  # and the directory, so the rename sticks

    def test_readable_store_saves_atomically(self):
        path = os.path.join(self.dir, "sub", "hourly_usage.json")
        store = HourlyUsageStore(path)
        store.merge([HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.0)])
        store.save()
        with open(path) as f:
            self.assertEqual(json.load(f)["record_count"], 1)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["hourly_usage.json"])  # no temp left

    def test_write_json_atomic_keeps_the_old_file_on_failure(self):
        path = os.path.join(self.dir, "x.json")
        write_json_atomic(path, {"a": 1})
        with self.assertRaises(TypeError):
            write_json_atomic(path, {"a": object()})  # not serialisable: fails mid-write
        with open(path) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["x.json"])


if __name__ == "__main__":
    unittest.main()

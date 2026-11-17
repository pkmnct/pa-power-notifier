"""Offline tests for collector/collector.py: range parsing, API bodies, schedule, auth."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "collector"))

import collector as col  # noqa: E402

EDT = timezone(timedelta(hours=-4))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.hourly = os.path.join(d, "hourly.json")
        self.daily = os.path.join(d, "daily.json")
        with open(self.hourly, "w") as f:
            json.dump({"records": [
                {"dt": "2026-10-31T23:00:00-04:00", "kwh": 1.0, "cost": 0.3},
                {"dt": "2026-11-01T00:00:00-04:00", "kwh": 2.0, "cost": 0.0},  # not priced yet
                {"dt": "2026-11-01T01:00:00-05:00", "kwh": 0.5, "cost": 0.1},
                {"dt": "2026-11-02T00:00:00-05:00", "kwh": 9.0, "cost": 1.0},
            ]}, f)
        with open(self.daily, "w") as f:
            json.dump({"records": [{"date": "2026-10-31", "kwh": 20, "cost": 5},
                                   {"date": "2026-11-01", "kwh": 2.5, "cost": 0.1}]}, f)

    def tearDown(self):
        self.tmp.cleanup()
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_date_range_is_local_and_dst_aware(self):
        start, end = col.parse_range("2026-11-01", "2026-11-01")
        self.assertEqual((start.isoformat(), end.isoformat()),
                         ("2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00"))

    def test_range_errors_and_default(self):
        with self.assertRaises(col.BadRequest):
            col.parse_range("nope", None)
        with self.assertRaises(col.BadRequest):
            col.parse_range("2026-11-02", "2026-11-01")
        now = datetime(2026, 11, 15, 12, tzinfo=EDT)
        start, end = col.parse_range(None, None, now=now)
        self.assertEqual((end - start).days, 30)

    def test_usage_response(self):
        start, end = col.parse_range("2026-11-01", "2026-11-01")
        body = col.usage_response(self.hourly, self.daily, start, end)
        self.assertEqual([h["start"] for h in body["hourly"]],
                         ["2026-11-01T00:00:00-04:00", "2026-11-01T01:00:00-05:00"])
        self.assertIsNone(body["hourly"][0]["cost"])
        self.assertEqual(body["daily"], [{"date": "2026-11-01", "kwh": 2.5, "cost": 0.1}])

    def test_bills_response(self):
        from usage_archive import BillArchive

        d = os.path.join(self.tmp.name, "bills")
        archive = BillArchive(d)
        archive._index = {
            "b2": {"bill_id": "b2", "bill_date": "2026-10-27", "supplier": "Example Energy",
                   "details": {"amount_due_usd": 239.14},
                   "egs": {"supply_rate_per_kwh": 0.1371, "price_to_compare": 0.1414, "kwh_on_bill": 907.0}},
            "b1": {"bill_id": "b1", "bill_date": "2025-10-26", "supplier": "", "details": {"amount_due_usd": 178.1}},
        }
        archive._save_index()
        body = col.bills_response(d)
        self.assertEqual([b["bill_id"] for b in body["bills"]], ["b1", "b2"])
        self.assertTrue(body["bills"][0]["default_service"])
        self.assertIsNone(body["bills"][0]["supplier"])
        self.assertEqual(body["current"]["supplier"], "Example Energy")
        self.assertEqual(body["current"]["supply_rate_per_kwh"], 0.1371)

    def test_first_start_survives_restarts(self):
        os.environ["DATA_DIR"] = self.tmp.name
        try:
            first = col.Collector()
            again = col.Collector()  # a restart: same archive, new process
            status = again.status()
        finally:
            os.environ.pop("DATA_DIR", None)
        self.assertEqual(status["first_started_at"], first.state["first_started_at"])
        self.assertIsNotNone(first.state["first_started_at"])

    def test_upgrade_keeps_a_failing_collector_stale(self):
        """A status file from before first_started_at, with runs but no success."""
        os.environ["DATA_DIR"] = self.tmp.name
        try:
            col._write_json(col.status_path(), {
                "last_success": None,
                "last_error": {"at": "2026-11-15T04:31:00-05:00", "type": "blocked", "message": "bot wall"},
                "last_run": {"started_at": "2026-11-15T04:30:00-05:00", "ok": False},
            })
            status = col.Collector().status()
        finally:
            os.environ.pop("DATA_DIR", None)
        self.assertEqual(status["first_started_at"], col.FIRST_START_UNKNOWN)

    def test_upgrade_of_a_working_collector_starts_now(self):
        os.environ["DATA_DIR"] = self.tmp.name
        try:
            col._write_json(col.status_path(), {
                "last_success": "2026-11-15T04:40:00-05:00",
                "last_error": None,
                "last_run": {"started_at": "2026-11-15T04:30:00-05:00", "ok": True},
            })
            collector = col.Collector()
        finally:
            os.environ.pop("DATA_DIR", None)
        self.assertEqual(collector.state["first_started_at"], col._iso(collector.started_at))

    def test_api_token_is_generated_saved_configured_or_off(self):
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ.pop("COLLECTOR_API_TOKEN", None)
        try:
            token, source = col.resolve_api_token()
            self.assertEqual(source, "generated")
            self.assertGreaterEqual(len(token), 30)
            path = os.path.join(self.tmp.name, col.TOKEN_FILE)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(col.resolve_api_token(), (token, "saved"))  # same after a restart
            os.environ["COLLECTOR_API_TOKEN"] = "mine"
            self.assertEqual(col.resolve_api_token(), ("mine", "configured"))
            os.environ["COLLECTOR_API_TOKEN"] = "None"
            self.assertEqual(col.resolve_api_token(), ("", "disabled"))
        finally:
            os.environ.pop("DATA_DIR", None)
            os.environ.pop("COLLECTOR_API_TOKEN", None)

    def test_run_fails_when_no_hourly_usage_comes_back(self):
        import duquesne_light_analyzer as dla

        class FakeClient:
            last_hourly_errors = 0

            def ensure_authenticated(self):
                pass

            def fetch_hourly_history(self, lookback):
                self.last_hourly_errors = 3  # every window failed
                return []

            def fetch_daily_response(self):
                return {}

        class FakeAnalyzer:
            def __init__(self):
                self.client = FakeClient()
                self.response_file = os.path.join(os.environ["DATA_DIR"], "duquesne_response.json")

        os.environ.update(DATA_DIR=self.tmp.name, COLLECTOR_BILLS="0")
        original = dla.DuquesneLightAnalyzer
        dla.DuquesneLightAnalyzer = FakeAnalyzer
        try:
            collector = col.Collector()
            self.assertFalse(collector.run_once())
            status = collector.status()
        finally:
            dla.DuquesneLightAnalyzer = original
            os.environ.pop("DATA_DIR", None)
            os.environ.pop("COLLECTOR_BILLS", None)
        self.assertIsNone(status["last_success"])
        self.assertEqual(status["last_error"]["type"], "hourly")
        self.assertIn("3 request(s) failed", status["last_error"]["message"])

    def test_run_fails_when_an_archive_file_cant_be_saved(self):
        """An unreadable archive file is never saved over; the run must say so, not succeed."""
        import duquesne_light_analyzer as dla
        from energy_shopper_core.tou import HourlyUsageRecord

        class FakeClient:
            last_hourly_errors = 0

            def ensure_authenticated(self):
                pass

            def fetch_hourly_history(self, lookback):
                return [HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.2)]

            def fetch_daily_response(self):
                return {}

        class FakeAnalyzer:
            def __init__(self):
                self.client = FakeClient()
                self.response_file = os.path.join(os.environ["DATA_DIR"], "duquesne_response.json")

        damaged = os.path.join(self.tmp.name, "hourly_usage.json")
        with open(damaged, "w") as f:
            f.write('{"records": [{"dt": "2026-11-01T00:00:00-04:00", "kwh": 1.5')  # cut off
        os.environ.update(DATA_DIR=self.tmp.name, COLLECTOR_BILLS="0")
        original = dla.DuquesneLightAnalyzer
        dla.DuquesneLightAnalyzer = FakeAnalyzer
        try:
            collector = col.Collector()
            self.assertFalse(collector.run_once())
            status = collector.status()
        finally:
            dla.DuquesneLightAnalyzer = original
            os.environ.pop("DATA_DIR", None)
            os.environ.pop("COLLECTOR_BILLS", None)
        self.assertIsNone(status["last_success"])
        self.assertEqual(status["last_error"]["type"], "archive")
        self.assertIn("hourly_usage.json", status["last_error"]["message"])
        with open(damaged) as f:
            self.assertTrue(f.read().endswith('"kwh": 1.5'))  # left as it was

    def test_run_failure_advice_fits_a_failed_write(self):
        """A full disk is not a damaged file: don't tell the user to move a healthy archive aside."""
        from unittest import mock

        import duquesne_light_analyzer as dla
        import usage_archive
        from energy_shopper_core.tou import HourlyUsageRecord

        class FakeClient:
            last_hourly_errors = 0

            def ensure_authenticated(self):
                pass

            def fetch_hourly_history(self, lookback):
                return [HourlyUsageRecord(dt="2026-11-02T00:00:00-05:00", kwh=1.0, cost=0.2)]

            def fetch_daily_response(self):
                return {}

        class FakeAnalyzer:
            def __init__(self):
                self.client = FakeClient()
                self.response_file = os.path.join(os.environ["DATA_DIR"], "duquesne_response.json")

        real_write = usage_archive.write_text_atomic

        def disk_full_for_hourly(path, text):
            if str(path).endswith("hourly_usage.json"):
                raise OSError(28, "No space left on device")
            return real_write(path, text)

        os.environ.update(DATA_DIR=self.tmp.name, COLLECTOR_BILLS="0")
        original = dla.DuquesneLightAnalyzer
        dla.DuquesneLightAnalyzer = FakeAnalyzer
        try:
            with mock.patch.object(usage_archive, "write_text_atomic", side_effect=disk_full_for_hourly):
                collector = col.Collector()
                self.assertFalse(collector.run_once())
            status = collector.status()
        finally:
            dla.DuquesneLightAnalyzer = original
            os.environ.pop("DATA_DIR", None)
            os.environ.pop("COLLECTOR_BILLS", None)
        message = status["last_error"]["message"]
        self.assertEqual(status["last_error"]["type"], "archive")
        self.assertIn("writing it failed", message)
        self.assertNotIn("moving it aside starts", message)

    def test_generated_token_is_never_logged(self):
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ.pop("COLLECTOR_API_TOKEN", None)
        try:
            token, source = col.resolve_api_token()
            with self.assertLogs("collector", level="INFO") as logs:
                col.announce_api_token(source)
        finally:
            os.environ.pop("DATA_DIR", None)
        output = "\n".join(logs.output)
        self.assertEqual(source, "generated")
        self.assertNotIn(token, output)
        self.assertIn(col.TOKEN_FILE, output)  # where to find it instead

    def test_next_run(self):
        now = datetime(2026, 11, 15, 5, 0).astimezone()  # local (America/New_York, EST)
        self.assertEqual(col.Collector.next_after(now, [(4, 30), (16, 0)]).hour, 16)
        nxt = col.Collector.next_after(now, [(4, 30)])
        self.assertEqual((nxt.day, nxt.hour, nxt.minute), (16, 4, 30))

    def test_next_run_across_dst(self):
        # `now` carries a fixed offset, as _now() returns; the next run must use the
        # offset of its own date.
        fall = datetime(2026, 10, 31, 23, 0).astimezone()  # EDT; DST ends Nov 1, 02:00
        nxt = col.Collector.next_after(fall, [(4, 30)])
        self.assertEqual((nxt.month, nxt.day, nxt.hour, nxt.minute), (11, 1, 4, 30))
        self.assertEqual(nxt.utcoffset(), timedelta(hours=-5))
        self.assertEqual(nxt - fall, timedelta(hours=6, minutes=30))

        spring = datetime(2026, 3, 7, 23, 0).astimezone()  # EST; DST starts Mar 8, 02:00
        nxt = col.Collector.next_after(spring, [(4, 30)])
        self.assertEqual((nxt.day, nxt.hour, nxt.minute, nxt.utcoffset()), (8, 4, 30, timedelta(hours=-4)))
        self.assertEqual(nxt - spring, timedelta(hours=4, minutes=30))

    def test_bearer_token(self):
        os.environ["DATA_DIR"] = self.tmp.name
        try:
            server = col.ThreadingHTTPServer(("127.0.0.1", 0), col.make_handler(col.Collector(), "s3cret"))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def get(path, token=None):
                req = urllib.request.Request(base + path)
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code

            self.assertEqual(get("/health"), 200)
            self.assertEqual(get("/status"), 401)
            self.assertEqual(get("/status", "wrong"), 401)
            self.assertEqual(get("/status", "s3cret"), 200)
            self.assertEqual(get("/usage?start=x", "s3cret"), 400)
            server.shutdown()
        finally:
            os.environ.pop("DATA_DIR", None)


if __name__ == "__main__":
    unittest.main()

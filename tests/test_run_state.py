"""Run-state files (previous_rates.json, rate_alerts.json) and the Price to Compare history."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import recommendation_engine as re_  # noqa: E402
from energy_shopper_core.models import Rate  # noqa: E402
from usage_archive import UNREADABLE, MarketArchive, take_failed_saves  # noqa: E402

SNAPSHOT = {"latest": {"electric": [], "gas": []}, "history": [{"timestamp": f"t{i}"} for i in range(5)]}


def rate(price) -> Rate:
    return Rate(price=price, supplier="S", type="Fixed", cancellation_fee="", deposit_required="",
                discounts_incentives="", introductory_price="", monthly_fee="", more_info="",
                service_type="Residential", term_end_date="", term_length="12", utility_type="electric")


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "previous_rates.json")
        take_failed_saves()

    def tearDown(self):
        self.tmp.cleanup()
        take_failed_saves()

    def test_snapshot_survives_a_write_that_fails_partway(self):
        """A failure while writing (here a value JSON can't encode) must leave the old file whole."""
        with open(self.path, "w") as f:
            json.dump(SNAPSHOT, f)
        with self.assertLogs(level="ERROR"):
            re_.save_snapshot(self.path, [rate(object())], [], None, None)
        with open(self.path) as f:
            self.assertEqual(json.load(f), SNAPSHOT)  # the 5-run history is still there

    def test_unreadable_snapshot_is_not_saved_over(self):
        with open(self.path, "w") as f:
            f.write('{"latest": {}, "history": [{"timestamp": "t0"}')  # cut off
        with self.assertLogs(level="ERROR"):
            re_.save_snapshot(self.path, [rate("$0.10")], [], None, None)
        with open(self.path) as f:
            self.assertTrue(f.read().endswith('"t0"}'))
        self.assertEqual(take_failed_saves(), [(self.path, UNREADABLE)])

    def test_snapshot_saves_normally(self):
        with open(self.path, "w") as f:
            json.dump(SNAPSHOT, f)
        re_.save_snapshot(self.path, [rate("$0.10")], [], None, None)
        with open(self.path) as f:
            saved = json.load(f)
        self.assertEqual(len(saved["history"]), 6)
        self.assertEqual(take_failed_saves(), [])

    def test_rate_alert_state_is_never_left_half_written(self):
        """A write that dies partway (disk full, killed run) mustn't leave unparseable state."""
        import pathlib

        alerts = os.path.join(self.tmp.name, "rate_alerts.json")
        with open(alerts, "w") as f:
            json.dump({"electric": "OLD"}, f)

        def dies_partway(self, data, *a, **kw):  # writes half, then fails, as a full disk does
            with open(self, "w") as f:
                f.write(data[: len(data) // 2])
            raise OSError(28, "No space left on device")

        with mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name}), \
                mock.patch.object(pathlib.Path, "write_text", dies_partway):
            re_._rate_alert_is_new("electric", "NEW")  # noqa: SLF001
        with open(alerts) as f:
            state = json.load(f)  # parseable: the old state or the new one, never half
        self.assertIn(state, ({"electric": "OLD"}, {"electric": "NEW"}))

    def test_price_to_compare_line_that_isnt_a_record_is_unreadable(self):
        market = MarketArchive(self.tmp.name)
        history = os.path.join(self.tmp.name, "price_to_compare.jsonl")
        with open(history, "w") as f:
            f.write('{"date": "2026-11-01", "utility": "gas", "price": 3.5}\n42\n')
        with self.assertLogs("usage_archive", level="ERROR"):
            market.log_price_to_compare("gas", 3.6, "mcf", "test")
        self.assertEqual(take_failed_saves(), [(history, UNREADABLE)])
        with open(history) as f:
            self.assertTrue(f.read().endswith("42\n"))  # left as it was


if __name__ == "__main__":
    unittest.main()

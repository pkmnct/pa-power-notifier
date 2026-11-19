"""The vendored energy_shopper_core copies under custom_components/ match the source."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import vendor_core  # noqa: E402


class VendoredCoreTests(unittest.TestCase):
    def test_copies_match_source(self):
        stale = [
            str(dst.relative_to(vendor_core.ROOT))
            for src, dst in vendor_core.vendored_pairs()
            if not dst.exists() or dst.read_bytes() != src.read_bytes()
        ]
        self.assertEqual(stale, [], "run: python3 scripts/vendor_core.py")


if __name__ == "__main__":
    unittest.main()

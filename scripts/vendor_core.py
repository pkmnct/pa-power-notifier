#!/usr/bin/env python3
"""
Copy the parts of energy_shopper_core that Home Assistant integrations need into each
integration's vendor/ folder, byte for byte.

Home Assistant installs an integration's requirements from PyPI, and energy_shopper_core
is not published yet, so each integration ships its own copy. tests/test_vendored_core.py
fails when a copy drifts from the source; run this script to refresh it:

    python3 scripts/vendor_core.py

Once the core is on PyPI, delete vendor/, add it to manifest.json "requirements" and
import energy_shopper_core directly.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "energy_shopper_core"

# integration folder -> core files it needs (paths relative to energy_shopper_core/)
VENDORED = {
    "peoples_gas": [
        "__init__.py",
        "gas.py",
        "utilities/__init__.py",
        "utilities/peoples_gas.py",
    ],
    "energy_shopper": [
        "__init__.py",
        "decision.py",
        "eligibility.py",
        "gas.py",
        "markets/__init__.py",
        "markets/base.py",
        "markets/pa.py",
        "models.py",
        "ranking.py",
        "report.py",
        "seasons.py",
        "tou.py",
        "usage.py",
    ],
}


def vendored_pairs():
    """(source, copy) paths for every vendored file."""
    for integration, files in VENDORED.items():
        dest = ROOT / "custom_components" / integration / "vendor" / "energy_shopper_core"
        for rel in files:
            yield CORE / rel, dest / rel


def main() -> int:
    for src, dst in vendored_pairs():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"{src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")
    for integration in VENDORED:
        init = ROOT / "custom_components" / integration / "vendor" / "__init__.py"
        if not init.exists():
            init.write_text('"""Vendored copy of energy_shopper_core (see scripts/vendor_core.py)."""\n')
    return 0


if __name__ == "__main__":
    sys.exit(main())

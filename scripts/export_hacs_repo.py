#!/usr/bin/env python3
"""
Build a standalone, HACS-shaped repository for one of the Home Assistant integrations.

HACS manages one integration per repository, so each integration in custom_components/
is published from its own repo. This repo stays the source of truth; this script writes
the publishable tree:

    python3 scripts/export_hacs_repo.py energy_shopper /tmp/ha-energy-shopper

The output holds custom_components/<domain>/ (with its vendored core, which must match
energy_shopper_core: the export stops if it has drifted), hacs.json, README.md, LICENSE, a validation workflow (hassfest + HACS)
and, for energy_shopper, the notify blueprint. It never pushes anything: commit and push
the output yourself, then tag a release (HACS wants at least one).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import vendor_core  # noqa: E402  (scripts/vendor_core.py: what each integration vendors)
SOURCE_REPO = "https://github.com/pkmnct/pa-power-notifier"
MIN_HA = "2026.2.0"  # the version the offline tests run against

INTEGRATIONS = {
    "peoples_gas": {
        "repo": "ha-peoples-gas",
        "title": "Peoples Gas (Pennsylvania) for Home Assistant",
        "anchor": "peoples-gas",
        "summary": (
            "Imports your Peoples Natural Gas usage and bill cost into the Energy dashboard, "
            "with sensors for the Price to Compare, commodity rate, last bill and balance."
        ),
    },
    "energy_shopper": {
        "repo": "ha-energy-shopper",
        "title": "Energy Shopper for Home Assistant",
        "anchor": "energy-shopper",
        "summary": (
            "Ranks Pennsylvania gas and electric supplier offers (PA Gas Switch, PA Power Switch) "
            "against your own usage and current rate, including Duquesne Light Time-of-Use, and "
            "tells you when switching pays: sensors, repair issues and an event for notifications."
        ),
        "extra": ["blueprints"],
    },
    "duquesne_collector": {
        "repo": "ha-duquesne-light-collector",
        "title": "Duquesne Light (collector) for Home Assistant",
        "anchor": "duquesne-light-collector",
        "summary": (
            "Reads the Duquesne Light collector add-on's local API: hourly usage and cost for the "
            "Energy dashboard, and your supplier, supply rate and Price to Compare from your bills."
        ),
    },
}

VALIDATE_WORKFLOW = """name: Validate

on:
  push:
  pull_request:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:

jobs:
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: home-assistant/actions/hassfest@master

  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: hacs/action@main
        with:
          category: integration
          # Icons need a home-assistant/brands PR; drop this once it's merged.
          ignore: brands
"""


def readme(domain: str, info: dict, manifest: dict) -> str:
    return f"""# {info['title']}

{info['summary']}

**Unofficial.** Not affiliated with the utilities or the PA PUC. See the
[source repository]({SOURCE_REPO}) for how it works, the full setup guide and issues.

## Install

With [HACS](https://hacs.xyz): HACS → ⋮ → Custom repositories → add this repository's URL
as an **Integration**, then install **{manifest['name']}** and restart Home Assistant.

Manually: copy `custom_components/{domain}/` into your Home Assistant `config/custom_components/`
and restart.

Then Settings → Devices & services → Add integration → **{manifest['name']}**.

Setup guide: [{SOURCE_REPO}/blob/main/docs/home-assistant.md#{info['anchor']}]({SOURCE_REPO}/blob/main/docs/home-assistant.md#{info['anchor']})

Requires Home Assistant {MIN_HA} or newer. Version {manifest['version']}.

This repository is generated from [{SOURCE_REPO.rsplit('/', 1)[1]}]({SOURCE_REPO}); please open
issues and pull requests there.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain", choices=sorted(INTEGRATIONS))
    parser.add_argument("out", type=Path, help="output directory (created; must be empty if it exists)")
    args = parser.parse_args()

    out: Path = args.out.resolve()
    if out.exists() and any(p.name != ".git" for p in out.iterdir()):
        print(f"{out} is not empty (a .git folder is fine)", file=sys.stderr)
        return 1

    domain, info = args.domain, INTEGRATIONS[args.domain]
    # Ship exactly what's committed and tested here: a vendored copy that drifted from
    # energy_shopper_core is an error to fix in this repo, not something to patch quietly.
    drifted = [
        str(copy.relative_to(ROOT))
        for src, copy in vendor_core.vendored_pairs()
        if copy.is_relative_to(ROOT / "custom_components" / domain)
        and (not copy.exists() or copy.read_bytes() != src.read_bytes())
    ]
    if drifted:
        print(
            "Vendored core is out of date: " + ", ".join(drifted)
            + "\nRun python3 scripts/vendor_core.py, test and commit, then export again.",
            file=sys.stderr,
        )
        return 1

    src = ROOT / "custom_components" / domain
    manifest = json.loads((src / "manifest.json").read_text())
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(src, out / "custom_components" / domain, ignore=ignore)
    for extra in info.get("extra", []):
        shutil.copytree(ROOT / extra, out / extra, ignore=ignore)
    shutil.copyfile(ROOT / "LICENSE", out / "LICENSE")
    (out / "hacs.json").write_text(
        json.dumps({"name": manifest["name"], "homeassistant": MIN_HA, "render_readme": True}, indent=2) + "\n"
    )
    (out / "README.md").write_text(readme(domain, info, manifest))
    workflows = out / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "validate.yml").write_text(VALIDATE_WORKFLOW)
    print(f"Wrote {out} for {domain} {manifest['version']} (suggested repo: pkmnct/{info['repo']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

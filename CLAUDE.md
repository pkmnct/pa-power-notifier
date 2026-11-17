# CLAUDE.md

Guidance for Claude sessions on this repo. What it is: [README.md](README.md). The Home
Assistant plan and how far each step got: [docs/home-assistant-design.md](docs/home-assistant-design.md).
Update a step's progress section there whenever you change that step's code.

## Layout

| Path | What |
|---|---|
| `energy_shopper_core/` | Shared library: ranking, `decision.decide_gas` / `decide_electric`, markets, utility clients. No env reads, no file paths, no imports from the rest of the repo |
| `*.py` at the root | The standalone notifier (CLI + email) and its env-reading wrappers |
| `usage_archive.py` | The merge-only archive stores under `data/` |
| `collector/` | Duquesne collector: headless-browser fetch + JSON API; HA add-on and Compose service |
| `custom_components/` | HA integrations: `peoples_gas`, `energy_shopper`, `duquesne_collector` |
| `tests/` | stdlib suite at the top; one HA suite per integration in its own folder |

## Tests and checks

Two environments:

- `.venv`: requirements + the stdlib suite and the CLI. CI also runs it on Python 3.11, the
  Docker image's version, so don't use newer syntax in code the CLI or collector imports.
  `make unit-test` in a fresh worktree creates an empty `.venv` and fails on `requests`:
  `make venv` first, or point at another checkout's `.venv`.
- `.venv-ha`: Python 3.13 with `pytest-homeassistant-custom-component==0.13.316` (HA 2026.2.3),
  `beautifulsoup4`, `pdfplumber` (see `tests/peoples_gas/README.md`).

```bash
.venv/bin/python -m unittest discover -s tests
cd tests/energy_shopper && ../../.venv-ha/bin/pytest -q    # likewise peoples_gas, duquesne_collector
docker run --rm --platform linux/amd64 -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest:latest   # from the repo root
```

hassfest has no arm64 image (hence `--platform`), and must see the repo root: mounting a
single integration folder gives a false "Domain does not match dir name".

### A bug fix comes with a test that fails without it

Prove it: stash the source change (`git stash push <source files>`), run the new test, see it
fail, `git stash pop`. Twice in this repo a "regression test" passed on the old code too: the
old code never reached the buggy path. If a test can't fail on the old code, say so.

### Refactors must not change the CLI's output

Changes to `energy_shopper_core/`, `recommendation_engine.py`, `usage_archive.py` or the
utility wrappers: run the notifier on the old and new code against **copies** of `data/`,
with email off and the utilities read from saved files, and compare. Reports must match
apart from timestamps; archive JSON must match apart from `last_updated`.

```bash
S=$(mktemp -d)   # or your scratchpad directory
PY="$PWD/.venv/bin/python"   # resolved here: run() also runs from inside "$S/old"
mkdir -p "$S/old" && git archive HEAD | tar -x -C "$S/old" && cp .env "$S/old/.env"
cp -R data "$S/dold" && cp -R data "$S/dnew"
rm -f "$S"/dold/reports/* "$S"/dnew/reports/*   # so the only report is this run's
run() { env SMTP_HOST= SMTP_USERNAME= SMTP_PASSWORD= NOTIFY_TO_EMAIL= \
  DUQUESNE_USERNAME= DUQUESNE_PASSWORD= PEOPLES_GAS_USERNAME= PEOPLES_GAS_PASSWORD= \
  DUQUESNE_USE_FILE=true PEOPLES_GAS_USE_FILE=true BILL_HISTORY_BACKFILL=0 \
  CHECK_INTERVAL_HOURS=0 TZ=America/New_York DATA_DIR="$1" "$PY" pa_power_notifier.py > "$2" 2>&1 \
  || { echo "run failed, see $2"; tail -5 "$2"; return 1; }; }
(cd "$S/old" && run "$S/dold" "$S/old.log") && run "$S/dnew" "$S/new.log"
o=$(ls "$S"/dold/reports/*.txt); n=$(ls "$S"/dnew/reports/*.txt)
for ext in txt html; do
  diff <(sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.-]+//g' "${o%.txt}.$ext") \
       <(sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.-]+//g' "${n%.txt}.$ext") && echo "$ext same"
done
```

Run it from the repo root with the change in the working tree (`git archive HEAD` is the
"before"). PA Power/Gas Switch offers are fetched live by both runs, so run them back to back.

## Invariants

### Vendored core

Each integration ships a copy of the core files it needs in `custom_components/*/vendor/`
(HA installs requirements from PyPI; the core isn't published). Never edit `vendor/`: change
`energy_shopper_core/`, run `python3 scripts/vendor_core.py`, commit both.
`tests/test_vendored_core.py` and CI fail on drift. A new core module an integration imports
goes in `VENDORED` in `scripts/vendor_core.py`.

### The archive in `data/`

`data/` is the user's real history (bills, usage, market snapshots) that the utilities have
since dropped: it can't be re-fetched. Never delete it, commit it, or run anything that writes
to it while experimenting: copy it to a scratch folder and set `DATA_DIR`.

- Stores are merge-only: existing records keep their first-seen values; blanks (cost) fill in.
- Write through `usage_archive.write_json_atomic`. The notifier and the collector share
  `data/` under Compose, so a reader must never see half a file.
- A store whose file exists but can't be read must not save: saving would replace history it
  couldn't load with what it fetched.

### Collector → HA statistics (`custom_components/duquesne_collector/coordinator.py`)

This took many review rounds; read the `merge_rebuild` and `_import_usage` docstrings first.

- Never clear or delete statistics. A rebuild lays the archive over what HA has, keeping HA's
  rows before the archive's first day and after its last row, and continuing the sums.
- A midnight row with no 01:00 row beside it is an aggregate (a daily total, or a first day's
  remainder) standing for 01:00 up to its series' next row that day. Individual hours beat an
  aggregate in both directions; no hour may count twice.
- The imported-hours count (`_known_hours`, saved in the entry's Store with the archive's
  newest hour) decides rebuilds: only a shortfall or a shrink triggers one, never ordinary new
  hours. Count from the same parsed hours you import (`_hourly_rows`).
- Don't use the recorder's `day` / `month` roll-ups: they drop rows that start on a DST change
  day. Read `hour` rows and aggregate yourself (see `energy_shopper`'s `monthly_usage_cycles`).
- Wall-clock times (the collector's schedule, local midnights) go through the real time zone
  (`.astimezone()` on a naive local time, or `zoneinfo`), never a fixed UTC offset.
- Test the reinstall cases: archive behind HA, archive smaller than HA's history, archive
  holding only daily totals where HA has hours. They're where every bug was.

### Energy Shopper (`custom_components/energy_shopper/`)

- A configured input sensor (rate, supplier, Price to Compare) with no value is a missing
  input: the recommendation is `unknown` with a note, not a quiet fallback that flips back
  and notifies twice. `ShopperData.compared` is the gate.
- Repair issues and the rate history change only on a refresh that compared something.
- Never delete a repair issue the user dismissed: deleting drops the dismissal.
- Gas and electric share code paths; an existing gas entry (no `fuel` in its data) must keep
  its entity and issue ids.

## HA test pitfalls

- Any test that sets up, reloads or changes the options of a real entry must finish with
  `await hass.async_block_till_done()` and `await async_wait_recording_done(hass)`.
  Config-flow tests that don't need the entry running patch `async_setup_entry`. Otherwise
  teardown closes the recorder's database under a statistics import: a segfault, often only
  on CI's slower runners.
- `strings.json` and `translations/en.json` must stay identical; hassfest rejects
  `<...>` in strings (write `HOST`, not `<host>`).
- `NumberSelector` needs `step >= 0.001` or `"any"`, and no `max=None`.

## Safety

- `.env` holds real credentials. Read single keys with python-dotenv; never print or log
  them. Values contain spaces (`ELECTRIC_RATE_TYPE=RS - Regular Residential Service`), so
  don't `export`/`eval` `.env` lines in a shell.
- Never let a run send email: blank the `SMTP_*` / `NOTIFY_TO_EMAIL` variables as above.
- Ask the owner first before anything that publishes: a `collector-v*` tag (CI pushes the
  add-on image to GHCR), creating the per-integration HACS repos
  (`scripts/export_hacs_repo.py` only writes a local tree), releases, a home-assistant/brands
  PR, or making a package public.
- Don't build or run code downloaded for checking (linters, actions); validate against their
  schemas as data instead.
- Several sessions may work on the same branch: `git fetch` before starting and before
  pushing, and look at commits others pushed before building on them.

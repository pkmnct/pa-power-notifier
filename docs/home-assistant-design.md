# Home Assistant integration: design draft

Status: draft for discussion, 2026-11-14. Step 1 of the build order is partly done (see "Step 1 progress"); steps 2 and 3 are built and tested offline (see "Step 2 progress" and "Step 3 progress"), not yet run in a live Home Assistant. Step 4's Duquesne collector and its `duquesne_collector` integration are built (see "Step 4 progress").

## Goal

Move the notifier's outputs into Home Assistant (HA) so that:

- electric and gas usage land in HA's Energy dashboard and long-term statistics,
- the current supplier, supply rate and best available offer are HA entities with history,
- "time to switch" and "your rate jumped" become HA notifications that stay open until dealt with,
- the standalone Docker + email version keeps working for people without HA.

Pennsylvania is the first target. Other states should be addable without touching the ranking logic.

## What we learned (tested 2026-11-13/14)

| Finding | Evidence | Consequence |
|---|---|---|
| Duquesne's site blocks plain HTTP | Login POST succeeds; every `www.duquesnelight.com` page returns the Incapsula bot wall, so no Opower token, bill list or PDF | Duquesne needs a real browser. HA's core Opower integration, which makes the same requests, is likely to fail from this network (not run directly) |
| Supplier is only in the bill PDF | No account JSON endpoint in the October captures mentions it | PDF download and parsing is required for Duquesne, not optional |
| Peoples Gas works over plain HTTP | Login, usage, balance, 24-bill history and a PDF download all succeeded with `requests` | Peoples can run inside HA with no browser |
| Peoples has no Green Button or API | One third-party guide; not confirmed with Peoples | Scraping the portal is the only route |
| `tronikos/opower` forbids headless browsers | Project contribution rules | Our Duquesne work can't go upstream; we build our own |

## Architecture

Three pieces, with one shared library underneath.

```
                     ┌──────────────────────────────────────────┐
                     │ Home Assistant                           │
 PA Power Switch ───▶│  energy_shopper   (decision layer)       │──▶ sensors, notifications,
 PA Gas Switch   ───▶│    reads usage statistics + rate sensors │    repair issues
                     │                                          │
 peopleseaccount ───▶│  peoples_gas      (plain HTTP)           │──▶ gas statistics, PTC, bills
                     │                                          │
                     │  duquesne_light   (thin client)          │──▶ electric statistics,
                     └──────────▲───────────────────────────────┘    supplier, supply rate
                                │ local HTTP (JSON)
                     ┌──────────┴───────────────────────────────┐
 duquesnelight.com ─▶│ Duquesne collector (add-on / container)  │
 duq.opower.com   ──▶│   headless Chromium, bill PDF parsing    │
                     └──────────────────────────────────────────┘
```

### 1. Shared library (`energy_shopper_core`, plain Python package)

No HA imports, no environment-variable reads, no file paths baked in. Everything else depends on it.

| Today | Becomes |
|---|---|
| `recommendation_engine.py` ranking, categories, lock-in notes, rate-jump alert | `core.ranking`, `core.alerts` (the snapshot/archive side effects move out) |
| `supplier_history.py` | `core.eligibility` |
| `tou_analyzer.py` | `core.tou` |
| `rate_scrapers.py` + offer fetching in `pa_power_notifier.py` | `core.markets.pa` behind a `MarketProvider` interface |
| `peoples_gas_analyzer.py` client and parsers | `core.utilities.peoples_gas` |
| `duquesne_light_analyzer.py` Opower calls and bill parser | `core.utilities.duquesne` (browser fetch stays in the collector) |

`recommendation_engine.py` currently mixes ranking with archiving and env lookups (`_archive_*`, `_backfill_gas_bills`, `checker` objects). Separating those is the bulk of the refactor.

**Interfaces that make other states possible**

- `MarketProvider`: `list_distributors(zip)`, `list_offers(distributor, rate_class)`, `price_to_compare(distributor)`. One implementation per state site (PA Power Switch / PA Gas Switch first; Ohio's Apples to Apples and Texas's Power to Choose are the obvious next ones).
- `Offer`: supplier, price, unit, term, monthly fee, cancellation fee, intro price, renewable %, new-customer-only flag. The ranking code only ever sees this.
- `UtilityAccount`: `usage(start, end)`, `bills()`, `current_supply()` (supplier + rate). Implemented by Peoples and Duquesne; other utilities can come from HA statistics alone.

### 2. `peoples_gas` integration (runs inside HA)

- UI config flow: username and password, validated by a login.
- A coordinator polls once or twice a day.
- Imports monthly usage as long-term statistics (gas, CCF) so the Energy dashboard can use it; backfills the ~24 months the portal exposes on first run.
- Sensors: Price to Compare, commodity rate, last bill amount, last bill date, current balance.
- Option (off by default): save bill PDFs to a folder under HA's config directory.

This is the smallest self-contained piece and has no existing HA equivalent, so it is the one to build first.

### 3. Duquesne collector + `duquesne_light` integration

**Collector**: the existing Docker image, trimmed to Duquesne and given a small read-only HTTP API on the local network:

- `GET /usage?start=&end=` hourly and daily kWh and cost
- `GET /bills` date, amount, supplier, supply rate per bill
- `GET /status` last successful run, last error

It keeps its own schedule and its merge-only archive (usage JSON, bill PDFs), as today. It is packaged two ways from the same image: an HA add-on for HA OS / Supervised installs, and plain Docker Compose for everyone else.

**Integration**: config flow asks for the collector's URL. It imports hourly usage and cost as statistics and exposes current supplier, supply rate, and last bill. Credentials live only in the collector.

Why an HTTP API rather than MQTT or shared files: it needs no broker, works identically for add-on and Compose installs, and the standalone email version can use the same collector.

### 4. `energy_shopper` integration (decision layer)

- Config flow: ZIP, then pick distributor and rate class from the market provider's lists (replaces pasting a results URL); pick the usage source (a statistic) and the current-rate source (a sensor, or a typed value) for each fuel; thresholds and filters as options.
- Because inputs are "any statistic" and "any sensor", it works with our two utility integrations, with core Opower where that works, or with manual values.
- Entities per fuel: best offer (supplier, price, term as attributes), projected annual savings, savings %, current supply rate, Price to Compare, recommendation (`switch` / `stay`), offers ranked (count, with the leaderboard as an attribute or a service response).
- Events and notifications:
  - a repair issue "Cheaper supplier available" that stays open until the supplier changes or the user dismisses it; this replaces `EMAIL_MODE=change` and `EMAIL_REMIND_DAYS`,
  - a repair issue for a supply-rate jump between bills,
  - an event fired on recommendation change, so users can route it to any notify service (phone push, email) with their own automation. A blueprint ships with the integration.
- A daily market snapshot stays available as an optional CSV archive, since HA's recorder isn't suited to a few hundred offers per day.

## What happens to the standalone version

`pa_power_notifier.py` becomes a thin CLI over the shared library plus the collector: same report, same email, same `.env`. HA users don't need it; non-HA users lose nothing.

## Repository layout

One repository to start, split later only if a piece gets its own users:

```
core/                      energy_shopper_core (published to PyPI)
custom_components/
  peoples_gas/
  duquesne_light/
  energy_shopper/
collector/                 Dockerfile, add-on config, browser/ (existing Puppeteer code)
cli/                       today's standalone notifier
docs/
```

The HA marketplace expects one integration per repository, so publishing there will probably mean splitting `custom_components/*` into separate repos at release time. To be confirmed against current marketplace rules.

## Build order

1. **Extract the core library** from the existing modules, with the current CLI still passing on top of it. No behaviour change.
2. **`peoples_gas` integration.** Proves the HA plumbing (config flow, coordinator, statistics import) on the easy utility.
3. **`energy_shopper` integration** for gas only, fed by step 2. First end-to-end "switch" notification in HA.
4. **Duquesne collector API**, then the thin `duquesne_light` integration.
5. **Electric in `energy_shopper`**, including Time-of-Use.
6. Packaging: add-on, marketplace listing, blueprint, docs.
7. Second state behind `MarketProvider` (Ohio or Texas).

## Step 1 progress

Done, with the standalone notifier's report byte-for-byte unchanged on an offline run
against archived data, and live PA Power/Gas Switch fetches returning identical offers:

| Core module | Came from |
|---|---|
| `energy_shopper_core.models` (`Rate`, alias `Offer`) | `pa_power_notifier.py` |
| `energy_shopper_core.markets.pa` (`PaPowerSwitch`, `PaGasSwitch`, CSV parsing, gas Price to Compare) and `markets.base` (`MarketProvider`) | `pa_power_notifier.py`, `rate_scrapers.py` |
| `energy_shopper_core.ranking`, `usage`, `alerts`, `seasons` | the pure parts of `recommendation_engine.py` |
| `energy_shopper_core.eligibility` | `supplier_history.py` (configured suppliers are now an argument, not an env read) |
| `energy_shopper_core.gas` | `gas_usage_forecast.py` (heat content is now an argument; the old module remains as the env-reading wrapper) |
| `energy_shopper_core.tou` | `tou_analyzer.py` (`HourlyUsageStore` moved to `usage_archive.py`) |
| `energy_shopper_core.report` | `check_report.py` |
| `energy_shopper_core.utilities.peoples_gas` (client, parsers, `fetch_all_usage`, `analyze`; login failures raise `PeoplesGasAuthError`) | `peoples_gas_analyzer.py`, which remains as the wrapper for env credentials, file mode and bill archiving. Checked live: usage, analysis, bill list and two re-downloaded bill PDFs identical to `main` |
| `energy_shopper_core.utilities.duquesne` (`DuquesneClient`: login, Opower token, daily and hourly GraphQL, account discovery; response parsers; bill-PDF EGS parser. Bad or missing credentials raise `DuquesneAuthError`, a token kept behind the bot wall raises `DuquesneBlockedError`; the browser fetch for the token page is passed in as `html_fetcher`) | `duquesne_light_analyzer.py`, which remains as the wrapper for env credentials, the Puppeteer path (`browser_fetch.py`), file mode, bill-PDF downloads and bill archiving. Offline notifier runs (file mode, and no credentials) give byte-identical reports, logs and archives to the branch base |

The package has no environment reads, file access or imports from the rest of the repo.
`make unit-test` runs its offline tests.

Still to do in step 1:

- **Orchestration.** Done for gas: `energy_shopper_core.decision.decide_gas` is the
  "inputs in, decision out" middle, and both the CLI and the `energy_shopper` integration
  call it. Electric (with Time-of-Use) is still inline in
  `recommendation_engine.build_check_report`; it moves with step 5.
- **Utility names in the ranking code.** "Duquesne Light" and "People's Gas" labels are
  hard-coded in `ranking.py` (default-service detection, Time-of-Use rows). They need to
  become parameters before a second utility or state.
- **Blocking HTTP.** `markets.pa` uses `requests`. HA integrations can call it in an
  executor; an async client is optional later.
- **Layout.** The package sits at the repo root as `energy_shopper_core/` rather than
  under `core/`, and the standalone scripts have not moved to `cli/`.

## Step 2 progress

`custom_components/peoples_gas/` is built, 2026-11-15, and passes offline Home Assistant
tests (`tests/peoples_gas/`, pytest-homeassistant-custom-component against HA 2026.2.3,
fake portal, no credentials). Not yet run against a live HA instance or the real portal.

| Piece | What it does |
|---|---|
| Config flow | Username and password, validated by a portal login. `invalid_auth` on `PeoplesGasAuthError`, `cannot_connect` on network/portal errors. Unique ID is the account number (hidden input on the bill dashboard). Reauth flow, which refuses a login for a different account |
| Coordinator | Polls every 12 hours with a fresh login, running the `requests` client in the executor. `ConfigEntryAuthFailed` on an auth error (starts reauth), `UpdateFailed` otherwise. Downloads the newest bill PDF only when the newest invoice changes |
| Statistics | External statistics `peoples_gas:<account>_gas_consumption` (CCF, converted from Mcf) and `peoples_gas:<account>_gas_cost` (bill amount), following core Opower: `get_last_statistics`, then backfill everything on first run (both portal pages, ~24 cycles) and only newer cycles after that, continuing the sums. One row per cycle at local midnight (America/New_York) of the read date, which the portal sets to the 1st of the billing month. Cost has its own "last row", so a bill that arrives after its usage is still imported |
| Sensors | Price to Compare and commodity rate (USD/CCF, with `per_mcf` attributes, parsed from the newest bill PDF), last bill amount (monetary), last bill date (date), current balance (monetary, `due_date` attribute). One service device per account |
| Bill PDFs | Option, off by default: saves every bill as `<config>/peoples_gas/<account>/<date>_<invoice>.pdf`, downloading each once |

**How the integration gets the core library:** a vendored, byte-for-byte copy of the
four files it needs (`energy_shopper_core/__init__.py`, `gas.py`, `utilities/__init__.py`,
`utilities/peoples_gas.py`) under `custom_components/peoples_gas/vendor/`. HA installs
requirements from PyPI and the core isn't published yet; a symlink doesn't survive being
copied into `config/custom_components/` or packaged for HACS. `scripts/vendor_core.py`
refreshes the copy, and `tests/test_vendored_core.py` (part of `make unit-test`) fails if
it drifts. Once the core is on PyPI: delete `vendor/`, add it to the manifest's
`requirements`, and change the imports.

Core change: `energy_shopper_core.gas` gained `CCF_PER_MCF`, `mcf_to_ccf` and
`usd_per_mcf_to_usd_per_ccf` (the "gas units" open question below, for volume).

Still to check live: the statistics in the Energy dashboard; whether
`_account_number()` (a private client method the integration calls) finds the hidden
input on every account; the portal's behaviour with two polls a day; hassfest/HACS
validation (not run).

## Step 3 progress

`custom_components/energy_shopper/` is built for gas, 2026-11-15, and passes offline Home
Assistant tests (`tests/energy_shopper/`, 20 tests, same harness as step 2: fake PA Gas
Switch, real recorder with an imported usage statistic). A live check fetched the real PA
Gas Switch offers and Price to Compare through the integration's own code. Not yet run in
a live HA instance.

| Piece | What it does |
|---|---|
| Core | `energy_shopper_core.decision`: `decide_gas(offers, unit, usage, baseline, threshold, prior suppliers, filters)` returns ranked options, recommendation, savings, best-by-category, lock-in note and notes; `Baseline`, `UsageCycle`; gas price unit conversion (Mcf, ccf, therm, ft³, m³, and HA spellings like `USD/CCF`). The CLI's gas section now calls it; its text and HTML reports are unchanged against archived data and live PA Gas Switch |
| Config flow | Step 1: ZIP, PA Gas Switch distributor id, service type, optional price unit; validated by fetching offers (`cannot_connect`, `no_offers`, `unknown_unit` when no Price to Compare tells the unit). Step 2: usage statistic, current-rate sensor or typed rate, threshold, no-cancellation-fee filter, prior suppliers. Options flow edits step 2 plus rate-jump % and heat content |
| Usage | Monthly `change` of any volume or energy statistic (so `peoples_gas`, core Opower or a meter's own), read over 3 years, converted to Mcf; the current partial month is skipped |
| Baseline | The rate sensor (converted from its unit), else the typed rate, else PA Gas Switch's Price to Compare; an unavailable sensor falls back with a note |
| Sensors | Recommendation (`switch` / `stay` / `unknown`, with notes, lock-in and usage summary), best offer (supplier, details as attributes), best offer price, annual savings, savings %, current supply rate, Price to Compare, projected annual use, offers ranked (leaderboard and best-by-category as attributes) |
| Repair issues | "Cheaper gas supplier: X" per recommended supplier, deleted when it stops being worth it; ignoring it silences that supplier only. "Your gas supply rate rose N%" when the sensor/typed rate jumps by the option's % (persistent, replaced by a newer jump) |
| Event | `energy_shopper_recommendation_changed` when switch/stay or the recommended supplier changes (not for a first-run "stay"); state survives restarts in `.storage` |
| Blueprint | `blueprints/automation/energy_shopper/notify_on_recommendation.yaml`: event → any notify action, "stay" optional; tested as a real automation |

The integration vendors the core like `peoples_gas` (`scripts/vendor_core.py` now lists
both). The daily market CSV archive is not in the integration yet.

Still to check live: the flow and sensors in a real HA; the statistic picker with
`peoples_gas` external statistics (the "Statistic IDs" question below); hassfest/HACS
validation. Distributor and rate class are typed ids, not picked from lists: PA Gas
Switch has no distributor list endpoint we use yet.

## Step 4 progress

The Duquesne collector is built in `collector/` (2026-11-15), and so is the thin
integration that consumes it, `custom_components/duquesne_collector/` (see below).

- `collector/collector.py`: one process with the daily fetch (hourly backfill, daily
  usage, newest bill plus any not yet archived, into the existing merge-only archive)
  and a read-only stdlib `http.server` API: `GET /usage?start=&end=`, `/bills`,
  `/status`, `/health`. Optional bearer token (`COLLECTOR_API_TOKEN`). Shapes are in
  `collector/README.md`.
- One image: the root `Dockerfile` gained a `collector` target. The default target is
  still the notifier, unchanged (same offline report from the built image).
- Packaging: Compose service `duquesne-collector` (profile `collector`, so plain
  `docker compose up` is unchanged) and a Home Assistant add-on (`collector/config.yaml`,
  `run.sh` maps `/data/options.json` to env, root `repository.yaml`).

Checked: unit tests; every endpoint against archived data, locally and in the image run
the way Supervisor runs an add-on (root start, `/data/options.json`, token); one live
fetch in the container through headless Chromium. Not checked: a real Supervisor install.
The add-on's `image` (`ghcr.io/pkmnct/pa-power-notifier-collector`) is not published yet.

**Integration (`duquesne_collector`).** The domain matches the add-on slug; `duquesne_light`
is taken by the core stub that points at Opower. Plain aiohttp to the collector, no
vendored core. Offline Home Assistant tests in `tests/duquesne_collector/` (15 tests: fake
collector behind `aioclient_mock`); the client and statistics code were also run against
the real collector serving a copy of the archive.

| Piece | What it does |
|---|---|
| Config flow | Collector URL (default `http://localhost:8765`, which reaches the add-on's published port from HA OS) and optional API token, checked against `/status` (`cannot_connect`, `invalid_auth`, `not_collector`). Reconfigure changes URL or token and keeps the statistic ids, which are fixed at setup (`duquesne_light`, then `duquesne_light_2`, …) |
| Statistics | External `duquesne_collector:<key>_energy_consumption` (kWh) and `_energy_cost` (USD), polled hourly. First run backfills every archived hour, plus daily totals at local midnight for older days with no hourly data. Each statistic continues from its own last row; cost waits at the first hour Opower hasn't priced, and gives up on hours still unpriced after 7 days |
| Sensors | From the newest bill: supplier (`Duquesne Light default service` when there's none), supply rate and Price to Compare ($/kWh), bill amount, date, kWh and supply charges. Diagnostics: collector last success (status as attributes) and usage data through |
| Repair issues | Collector login failing (`last_error.type == "auth"` newer than the last success); no success for 36 h; collector unreachable for 24 h. Each clears itself on recovery |

Live against the archive copy: 10,290 hourly rows (Aug 2025 → Nov 2026, 12,849 kWh), 9,978
priced (312 old hours Opower never priced are skipped), newest bill's supplier and rates
read correctly. Not run in a live HA.

## Open questions

- **Names.** `energy_shopper` is a placeholder. Duquesne's integration is `duquesne_collector` (matches the add-on slug; `duquesne_light` is the core Opower stub).
- **Duquesne usage: ours or Opower's?** If core Opower works for some users, should our integration skip usage and only add supplier and bills? Simplest is to always provide both from the collector.
- **Statistic IDs.** External statistics (`peoples_gas:...`) versus entity-backed ones. External is what Opower does and avoids fake "sensor" state; needs a check that the decision layer's source picker handles them.
- **Gas units.** The portal reports Mcf, HA's Energy dashboard wants CCF or m³, offers are priced per Mcf/ccf/therm depending on the utility. Conversion belongs in the core library, once. (Mcf → CCF is now in `core.gas`; therms were already there.)
- **Collector reliability.** The browser path has been stable for us, but the site can change. The integration should surface "collector hasn't succeeded in N days" as a repair issue.
- **Terms of use.** Both utility integrations log in to the user's own account at a low rate (daily). The README disclaimer carries over; worth a second read of both sites' terms before publishing to a marketplace.
- **Other states.** Only a light check so far: Texas, Ohio and New York have official comparison sites; Illinois, New Jersey and Maryland are unconfirmed.

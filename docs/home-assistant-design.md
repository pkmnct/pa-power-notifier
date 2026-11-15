# Home Assistant integration: design draft

Status: draft for discussion, 2026-11-14. Step 1 of the build order is partly done (see "Step 1 progress"); nothing else is built yet.

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

The package has no environment reads, file access or imports from the rest of the repo.
`make unit-test` runs its offline tests.

Still to do in step 1:

- **Duquesne client.** The Opower and bill-parsing parts of `duquesne_light_analyzer.py`
  are not in the package yet. They mix HTTP, the browser fallback, environment reads and
  archive writes; the browser part stays out of the package (it belongs to the collector).
- **Orchestration.** `recommendation_engine.build_check_report` still gathers data,
  archives it and builds the report in one function. The HA decision layer needs the
  "inputs in, sections out" middle of it as a core function.
- **Utility names in the ranking code.** "Duquesne Light" and "People's Gas" labels are
  hard-coded in `ranking.py` (default-service detection, Time-of-Use rows). They need to
  become parameters before a second utility or state.
- **Blocking HTTP.** `markets.pa` uses `requests`. HA integrations can call it in an
  executor; an async client is optional later.
- **Layout.** The package sits at the repo root as `energy_shopper_core/` rather than
  under `core/`, and the standalone scripts have not moved to `cli/`.

## Open questions

- **Names.** `energy_shopper` is a placeholder. `duquesne_light` collides with the core stub domain of the same name that points at Opower; ours needs a different domain (e.g. `duquesne_light_bills`).
- **Duquesne usage: ours or Opower's?** If core Opower works for some users, should our integration skip usage and only add supplier and bills? Simplest is to always provide both from the collector.
- **Statistic IDs.** External statistics (`peoples_gas:...`) versus entity-backed ones. External is what Opower does and avoids fake "sensor" state; needs a check that the decision layer's source picker handles them.
- **Gas units.** The portal reports Mcf, HA's Energy dashboard wants CCF or m³, offers are priced per Mcf/ccf/therm depending on the utility. Conversion belongs in the core library, once.
- **Collector reliability.** The browser path has been stable for us, but the site can change. The integration should surface "collector hasn't succeeded in N days" as a repair issue.
- **Terms of use.** Both utility integrations log in to the user's own account at a low rate (daily). The README disclaimer carries over; worth a second read of both sites' terms before publishing to a marketplace.
- **Other states.** Only a light check so far: Texas, Ohio and New York have official comparison sites; Illinois, New Jersey and Maryland are unconfirmed.

# PA Power & Gas Rate Notifier

Monitors Pennsylvania electric and gas supplier rates from [PA Power Switch](https://www.papowerswitch.com) and [PA Gas Switch](https://www.pagasswitch.com), ranks them using your actual usage (including Duquesne time-of-use), and emails you when a better deal is available.

Runs fully self-contained in a single Docker image. All configuration is via environment variables.

## Features

- Checks both electric (Duquesne Light) and gas (People's Natural Gas) rates
- Ranks offers by **projected annual supply cost** using your usage history, not sticker price alone
- Puts Duquesne **time-of-use** on the same electric leaderboard as fixed supplier offers
- Seasonal lock-in notes (summer AC, winter heat) and term / intro / cancellation-fee flags
- Prints a full CLI report every run, including warnings and unexpected data
- Emails a plain-text + HTML report when a switch beats your threshold, TOU is recommended, or a data fetch errors
- Auto-detects your current rate via account login or public scrapes — no manual entry needed
- Optional filters: no cancellation fee, renewable/solar plans only
- Runs on a configurable interval (default: every 24 hours)

## What the daily report contains

Every check prints (and may email) one report covering:

- **Warnings** — empty CSVs, failed logins, Incapsula/PTC scrape misses, sparse seasonal usage, unparsed term lengths, supply-vs-blended caveats
- **Electric** — current DLC plan label when the account API provides it, blended all-in vs Price to Compare (supply-only), usage window, TOU vs fixed vs PA Power Switch offers ranked by annual supply $, lock-in note, market trend vs last snapshot
- **Gas** — People's usage forecast (median MCF per calendar month → therms) vs PA Gas Switch offers, same lock-in / trend notes

Supplier prices are **supply/commodity only**. Distribution, transmission, and riders stay on your utility bill; the report uses Duquesne's Price to Compare (or `TOU_FLAT_RATE`) so savings are not overstated.

## Quick Start

### Run once (manual check)

```bash
docker build -t pa-power-notifier .
docker run --rm \
  -e CHECK_INTERVAL_HOURS=0 \
  -e CURRENT_ELECTRIC_PRICE=0.12 \
  -e CURRENT_GAS_PRICE=0.45 \
  pa-power-notifier
```

Locally (recommended for Duquesne/People's login):

```bash
make venv
CHECK_INTERVAL_HOURS=0 make test
```

### Run continuously with Docker Compose

1. Create a `.env` file (copy the example below)
2. Run:

```bash
docker-compose up -d
docker-compose logs -f
```

---

## Environment Variables

### Scheduling

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL_HOURS` | `24` | How often to check rates. Set to `0` to run once and exit. |

### Electric (Duquesne Light)

| Variable | Default | Description |
|---|---|---|
| `ELECTRIC_ID` | `1180` | Your electric utility ID on PA Power Switch |
| `ELECTRIC_SERVICE_TYPE` | `residential` | Service type |
| `ELECTRIC_RATE_TYPE` | `RS - Regular Residential Service` | Rate type |
| `INCLUDE_DUQUESNE_AS_OPTION` | `true` | Add Duquesne's "Price to Compare" as a synthetic option in the comparison |

### Gas (People's Natural Gas)

| Variable | Default | Description |
|---|---|---|
| `GAS_ID` | `4423` | Your gas utility ID on PA Gas Switch |
| `GAS_SERVICE_TYPE` | `residential` | Service type (offers listed under another service type are dropped) |
| `GAS_ZIPCODE` | `ELECTRIC_ZIPCODE` | ZIP used to read the utility's supply-only Price to Compare and price unit from the PA Gas Switch shop page |
| `GAS_PRICE_TO_COMPARE` | `0` | Fallback supply-only Price to Compare, in `GAS_PRICE_UNIT` |
| `GAS_PRICE_UNIT` | `mcf` | Fallback unit PA Gas Switch quotes offers in (`mcf`, `ccf` or `therm`; Peoples uses Mcf) |

### Current Rate Resolution

Your current rate is resolved in this priority order — you only need to set the fallback if the higher-priority sources fail:

**Electric current supply (EGS):** latest Duquesne bill PDF → `ENERGY CHARGE` + supplier name; PTC on the same bill (or scrape / `TOU_FLAT_RATE`) is a competing “return to DLC default” option, not the stay baseline.

**Electric blended (all-in):** Duquesne Opower usage API → bill PTC → `CURRENT_ELECTRIC_PRICE` (context only; not used as the switch baseline)

**Gas supply (stay baseline):** PA Gas Switch shop-page Price to Compare (needs `GAS_ZIPCODE`) → `GAS_PRICE_TO_COMPARE`. Gas is ranked in the unit PA Gas Switch quotes (Mcf for Peoples), with forecast usage converted via `THERMS_PER_MCF`.

**Gas blended (all-in):** People's Gas portal (login) → scraped People's Gas rate page → `CURRENT_GAS_PRICE` (context only; used as the baseline only when no Price to Compare is available)

| Variable | Default | Description |
|---|---|---|
| `CURRENT_ELECTRIC_PRICE` | `0` | Fallback current electric rate ($/kWh) |
| `CURRENT_GAS_PRICE` | `0` | Fallback current gas rate ($/therm) |
| `DUQUESNE_USERNAME` | _(none)_ | Duquesne Light web login (preferred) |
| `DUQUESNE_PASSWORD` | _(none)_ | Duquesne Light web password |
| `DUQUESNE_CUSTOMER_UUID` | _(none)_ | Optional Opower customer UUID (auto-discovered after login) |
| `DUQUESNE_UTILITY_ACCOUNT_UUID` | _(none)_ | Optional utility account UUID (auto-discovered after login) |
| `DUQUESNE_CUSTOMER_URN` | _(none)_ | Optional override if you already have the Opower URN |

### Notifications

| Variable | Default | Description |
|---|---|---|
| `NOTIFICATION_THRESHOLD` | `5.0` | Minimum % savings to trigger an alert |
| `SMTP_HOST` | _(none)_ | SMTP server hostname (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USERNAME` | _(none)_ | SMTP login username |
| `SMTP_PASSWORD` | _(none)_ | SMTP login password |
| `NOTIFY_TO_EMAIL` | _(none)_ | Email address to send alerts to |
| `NOTIFY_FROM_EMAIL` | _(SMTP_USERNAME)_ | From address (defaults to username) |

All four SMTP variables plus `NOTIFY_TO_EMAIL` must be set to enable email. If any are missing, the check still runs and the full report is printed to stdout. Email is sent when a switch beats `NOTIFICATION_THRESHOLD`, when TOU is recommended, or when an **error-level** warning was recorded (so silent scrape failures are visible).

### Filters

| Variable | Default | Description |
|---|---|---|
| `FILTER_NO_CANCELLATION_FEE` | `false` | Exclude plans with a cancellation fee |
| `FILTER_RENEWABLE_ONLY` | `false` | Only consider green/solar/renewable plans |

### Storage

| Variable | Default | Description |
|---|---|---|
| `STORAGE_FILE` | `/data/previous_rates.json` | Latest rate dump plus a compact history of cheapest offers (for the trend line) |
| `HOURLY_USAGE_FILE` | `/data/hourly_usage.json` | Accumulating Duquesne hourly kWh (merge-only; never truncated) |
| `DAILY_USAGE_FILE` | `/data/daily_usage.json` | Accumulating Duquesne daily kWh/cost |
| `GAS_USAGE_FILE` | `/data/gas_usage.json` | Accumulating People's Gas monthly cycles |
| `BILL_ARCHIVE_DIR` | `/data/bills` | One PDF + EGS metadata per Duquesne bill id (`index.json`) |

Local runs default these under `./data/` when the env vars are unset. Archives are **merge-only**: each check adds new days/months/bills; older rows are kept even after the utility drops them from their API window.

---

## Example `.env` File

```bash
# How often to check (hours); 0 = run once
CHECK_INTERVAL_HOURS=24

# Electric utility IDs (find yours at papowerswitch.com)
ELECTRIC_ID=1180
ELECTRIC_SERVICE_TYPE=residential
ELECTRIC_RATE_TYPE=RS - Regular Residential Service

# Gas utility IDs (find yours at pagasswitch.com)
GAS_ID=4423
GAS_SERVICE_TYPE=residential
GAS_ZIPCODE=15xxx

# Fallback rates — only needed if scraper and account APIs fail
CURRENT_ELECTRIC_PRICE=0.12
CURRENT_GAS_PRICE=0.45

# Duquesne Light login (preferred — usage, current plan, hourly TOU)
DUQUESNE_USERNAME=you@example.com
DUQUESNE_PASSWORD=your-password
# Optional if auto-discovery fails:
# DUQUESNE_CUSTOMER_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# DUQUESNE_UTILITY_ACCOUNT_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# People's Gas e-Account (usage + effective commodity rate)
PEOPLES_GAS_USERNAME=your-username
PEOPLES_GAS_PASSWORD=your-password

# Alert threshold
NOTIFICATION_THRESHOLD=5.0

# Email alerts (all five required to enable)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-app-password
NOTIFY_TO_EMAIL=you@gmail.com

# Filters (optional)
FILTER_NO_CANCELLATION_FEE=false
FILTER_RENEWABLE_ONLY=false
```

---

## Finding Your Utility IDs

### Electric ID and Rate Type

1. Visit [PA Power Switch](https://www.papowerswitch.com)
2. Enter your zip code and select your utility
3. Open browser DevTools → Network tab, look for the `RateCSV` request
4. The query parameters contain your `id` and `ratetype`

### Gas ID

1. Visit [PA Gas Switch](https://www.pagasswitch.com)
2. Same process — find the `RateCSV` request for your `id`

---

## Duquesne Light Account Integration (Optional)

Set `DUQUESNE_USERNAME` and `DUQUESNE_PASSWORD` (the same login as duquesnelight.com). After login the notifier discovers Opower account IDs automatically. `DUQUESNE_CUSTOMER_UUID` / `DUQUESNE_UTILITY_ACCOUNT_UUID` / `DUQUESNE_CUSTOMER_URN` are optional overrides.

This enables: blended effective rate, tariff name when GraphQL provides it, daily/hourly usage, and **current EGS name + supply rate from your bill PDF** (via Puppeteer: `GetAccountBillHistory` → `DisplayBillPdf/{id}`).

If Duquesne's public rate pages are blocked (Incapsula), the bill PDF still provides PTC. You can also set `TOU_FLAT_RATE` / `TOU_PEAK_RATE` / `TOU_OFF_PEAK_RATE` / `TOU_SUPER_OFF_PEAK_RATE` in ¢/kWh. Puppeteer (`make browser-install` + Chrome for Testing) is required for bill-PDF and Incapsula-blocked pages and is **not** bundled in the Docker image yet.

Offline bill parse: save a PDF and set `DUQUESNE_BILL_PDF=/path/to/bill.pdf`.

### File mode (offline / dev)

If the live API is inaccessible, you can save the GraphQL response to `response.json` and set:

```bash
DUQUESNE_USE_FILE=true
DUQUESNE_RESPONSE_FILE=response.json  # default
```

Hourly TOU fetch has no file-mode path — it needs a live login.

---

## People's Gas Integration (Optional)

Set `PEOPLES_GAS_USERNAME` and `PEOPLES_GAS_PASSWORD` for e-Account usage + billing history (seasonal MCF forecast). The public rates page is used as a fallback for the current commodity rate.

For file mode, capture the POST response from `Usages.aspx/LoadGasUsage` in browser DevTools, save it as `gasResponse.json`, and set:

```bash
PEOPLES_GAS_USE_FILE=true
PEOPLES_GAS_RESPONSE_FILE=gasResponse.json  # default
```

File mode does not include billing history, so the effective $/MCF may be missing.

---

## Extra comparison CLIs

The daily job already uses usage-weighted ranking. These scripts print a longer table of the same engine:

```bash
make electric-plan-check   # PA Power Switch ranked by your kWh
make gas-plan-check        # People's forecast + PA Gas Switch
```

`example_duquesne_integration.py` / `example_peoples_gas_integration.py` are older demos and may mix blended vs supply-only rates — prefer the daily job or the `make *-plan-check` targets.

---

## Troubleshooting

**No rates found** — Verify `ELECTRIC_ID` / `GAS_ID`. Check that `papowerswitch.com` and `pagasswitch.com` are reachable from the container.

**Current rate shows 0** — Set `CURRENT_ELECTRIC_PRICE` / `CURRENT_GAS_PRICE` as a fallback, or provide Duquesne account credentials. Without a current rate, no savings comparison is possible.

**No savings alert** — Lower `NOTIFICATION_THRESHOLD`. Check logs to confirm rates were fetched successfully and what the current best rate is.

**Email not sending** — Confirm all five email variables are set. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833), not your regular password.

**Filters removing all results** — If `FILTER_RENEWABLE_ONLY=true` returns nothing, no renewable plans may be listed in the current CSV from PA Power/Gas Switch.

---

## License

MIT

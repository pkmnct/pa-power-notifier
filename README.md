# PA Power & Gas Rate Notifier

Checks Pennsylvania electric and gas supplier offers from [PA Power Switch](https://www.papowerswitch.com) and [PA Gas Switch](https://www.pagasswitch.com) on a schedule, ranks them against **your actual usage and current rate**, and emails you when switching is worth it. Along the way it keeps a permanent local archive of your bills, usage and the market, which utilities otherwise drop after a year or two.

> **Unofficial.** Not affiliated with Duquesne Light, Peoples Natural Gas, Opower or the PA PUC. The utility integrations log in to *your own* account the way a browser does and read pages and private APIs that can change without notice; automated access may be restricted by those sites' terms. Use with your own account, at a sensible interval (the default is once a day).

## Supported utilities

| | Market offers | Account integration (optional) |
|---|---|---|
| **Electric** | Any PA Power Switch distributor | **Duquesne Light**: hourly/daily usage, every bill PDF, current supplier + supply rate from the bill, Time-of-Use |
| **Gas** | Any PA Gas Switch distributor | **Peoples Natural Gas**: monthly usage, every bill PDF, Price to Compare history |

Without an account integration the checker still ranks offers, using `CURRENT_ELECTRIC_PRICE` / `CURRENT_GAS_PRICE` as your current rate.

## Home Assistant

The same ranking runs inside Home Assistant: usage in the Energy dashboard, switch/stay
sensors, repair issues and a phone notification blueprint, for gas and electric
(including Duquesne Light Time-of-Use). See **[docs/home-assistant.md](docs/home-assistant.md)**
for the add-on and the three integrations. The standalone Docker + email version below
keeps working without it.

## What you get

Each run prints a report (and keeps a copy) and emails it when there's something to act on:

- **Recommendation**: switch or stay, with annual savings vs. your current *supply* rate. Supplier offers are supply-only, so they're compared with the supply line of your bill, never the all-in rate.
- **Best by category**: overall, new-customer offers you're still eligible for, offers open to existing customers, fixed, fixed ≥12 months, variable, 100% renewable, other renewable, non-renewable, utility default, Time-of-Use on your usage (plus 25% / 50% load-shifted variants), and the best offer your filters removed. Each shows savings and the supplier's phone number.
- **Leaderboard** of offers by projected annual cost including monthly fees, with term, intro-price and cancellation-fee flags.
- **Alerts**: your own supply rate jumping between bills (variable-rate creep), new-customer-only offers from suppliers you've already used (skipped, with the reason), and any data that failed to load.

Email goes out when a switch beats `NOTIFICATION_THRESHOLD`, Time-of-Use is recommended, your rate jumps, or an error-level problem occurs.

## Quick start (Docker)

```bash
cp .env.example .env      # fill in your shop URLs, logins and email settings
docker compose up -d      # checks now, then every CHECK_INTERVAL_HOURS
docker compose logs -f
```

To run once and exit: `CHECK_INTERVAL_HOURS=0 docker compose run --rm pa-power-notifier`.

The image bundles headless Chromium (Duquesne's site blocks plain HTTP clients) and runs as an unprivileged user. On Linux hosts make sure `./data` is writable by uid 1000.

### Local (without Docker)

```bash
make venv
make browser-install      # Puppeteer + Chrome, needed for Duquesne
make test                 # one check, using .env
```

## Configuration

Everything is set with environment variables (`.env` locally or with Compose). [`.env.example`](.env.example) lists the common ones.

### Finding your offers

Search your ZIP on [papowerswitch.com](https://www.papowerswitch.com), pick your utility and rate class, and paste the results page URL into `ELECTRIC_SHOP_RESULTS_URL`. It supplies the distributor id, rate type and ZIP. For gas, take `distributor=` from your [pagasswitch.com](https://www.pagasswitch.com) results URL as `GAS_ID`.

| Variable | Default | Description |
|---|---|---|
| `ELECTRIC_SHOP_RESULTS_URL` | | PA Power Switch results URL (sets the three below) |
| `ELECTRIC_ID` | | Distributor id (`distributor=` in the URL) |
| `ELECTRIC_RATE_TYPE` | | Rate class (`distributorrate=`), e.g. `RS - Regular Residential Service`. Required: PA Power Switch returns no offers without it |
| `ELECTRIC_ZIPCODE` | | ZIP |
| `ELECTRIC_SERVICE_TYPE` | `residential` | |
| `GAS_ID` | | PA Gas Switch distributor id |
| `GAS_SERVICE_TYPE` | `residential` | Offers listed under another service type are dropped |
| `GAS_ZIPCODE` | `ELECTRIC_ZIPCODE` | Used to read your gas utility's Price to Compare and price unit (Mcf / ccf / therm) |
| `GAS_PRICE_TO_COMPARE`, `GAS_PRICE_UNIT` | | Fallbacks if that page can't be read |
| `THERMS_PER_MCF` | `10.37` | Gas heat content, for converting usage between units |

### Accounts

| Variable | Description |
|---|---|
| `DUQUESNE_USERNAME`, `DUQUESNE_PASSWORD` | duquesnelight.com login. Opower account ids are discovered automatically |
| `DUQUESNE_USE_PUPPETEER` | `1` to go straight to headless Chrome for Duquesne pages (recommended) |
| `PEOPLES_GAS_USERNAME`, `PEOPLES_GAS_PASSWORD` | peopleseaccount.com login |
| `CURRENT_ELECTRIC_PRICE`, `CURRENT_GAS_PRICE` | Your current rate ($/kWh, $/therm) when there's no account integration |
| `TOU_FLAT_RATE`, `TOU_PEAK_RATE`, `TOU_OFF_PEAK_RATE`, `TOU_SUPER_OFF_PEAK_RATE` | Duquesne Time-of-Use rates in ¢/kWh, used when the tariff PDF can't be scraped (they change each Jun 1 / Dec 1) |

### Recommendations and alerts

| Variable | Default | Description |
|---|---|---|
| `NOTIFICATION_THRESHOLD` | `5.0` | Minimum % supply savings to recommend switching |
| `FILTER_NO_CANCELLATION_FEE` | `false` | Skip offers with a cancellation fee |
| `FILTER_RENEWABLE_ONLY` | `false` | Only renewable offers |
| `RATE_JUMP_ALERT_PCT` | `10` | Email once when your supply rate rises this much between bills |
| `EMAIL_MODE` | `change` | `change`: email only when the verdict, recommended offer or errors differ from the last email (new rate jumps always email). `daily`: email every run that has something to report |
| `EMAIL_REMIND_DAYS` | `0` | With `EMAIL_MODE=change`, resend an unchanged alert after this many days (0 = never) |
| `PRIOR_ELECTRIC_SUPPLIERS`, `PRIOR_GAS_SUPPLIERS` | | Suppliers used before your oldest archived bill, `Name@YYYY-MM-DD; Other Name`, so their new-customer-only offers are skipped |
| `INCLUDE_DUQUESNE_AS_OPTION` | `true` | List Duquesne's default service (Price to Compare) as an option |

New-customer-only offers (flagged by PA Power/Gas Switch or described that way) are skipped for suppliers you've used at this address, unless the offer's stated re-eligibility window has passed. Electric supplier history comes from your Duquesne bills automatically.

### Email

Set `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD` and `NOTIFY_TO_EMAIL` (`NOTIFY_FROM_EMAIL` defaults to the username). Without them the report is still printed and archived. For Gmail use an [app password](https://support.google.com/accounts/answer/185833).

### Schedule

`CHECK_INTERVAL_HOURS` (default `24`; `0` runs once). `TZ` (default `America/New_York`) sets report names and daily archive dates.

## Data archive

Everything persists under `DATA_DIR` (`/data` in the image, `./data/` locally). Archives are merge-only: each run adds what's new and nothing is removed, even after the utility stops serving it.

| Path | Contents |
|---|---|
| `hourly_usage.json`, `daily_usage.json` | Duquesne usage and cost (hourly history is backfilled ~400 days on each run) |
| `gas_usage.json` | Peoples monthly usage cycles |
| `bills/` | Every Duquesne bill PDF; `index.json` has date, amount, supplier and supply rate |
| `gas_bills/` | Every Peoples bill PDF; `index.json` has Price to Compare, commodity $/Mcf, Mcf used, amount |
| `market/electric/`, `market/gas/` | The full PA Power/Gas Switch offer list, one CSV per day |
| `market/price_to_compare.jsonl` | Default-service prices over time |
| `reports/` | Every run's report (`.txt` and `.html`) |
| `previous_rates.json`, `rate_alerts.json`, `last_email.json`, `tou_rates.json` | Run state |

Single paths can be overridden (`HOURLY_USAGE_FILE`, `BILL_ARCHIVE_DIR`, `GAS_BILL_ARCHIVE_DIR`, `MARKET_ARCHIVE_DIR`, `REPORT_ARCHIVE_DIR`, `STORAGE_FILE`, …). Set `BILL_HISTORY_BACKFILL=0` to stop downloading older bills.

## Extra commands

```bash
make electric-plan-check   # longer electric ranking against your usage
make gas-plan-check        # gas usage forecast + ranking
make unit-test             # offline tests for the shared ranking library (energy_shopper_core/)
```

Offline debugging: `DUQUESNE_BILL_PDF=/path/to/bill.pdf` parses a saved bill; `DUQUESNE_USE_FILE=true` reads the last saved Opower response (`DATA_DIR/duquesne_response.json`) instead of logging in; `PEOPLES_GAS_USE_FILE=true` reads a saved `LoadGasUsage` response (`PEOPLES_GAS_RESPONSE_FILE`). `PA_POWER_DEBUG=1` turns on debug logging.

## Duquesne collector (Home Assistant)

An optional service that fetches Duquesne usage and bills daily and serves them as a local JSON API for the Home Assistant integrations ([docs/home-assistant.md](docs/home-assistant.md)). It runs as a Home Assistant add-on or with `docker compose --profile collector up -d duquesne-collector`. See [collector/README.md](collector/README.md).

## Troubleshooting

- **No offers**: check `ELECTRIC_RATE_TYPE` (or use `ELECTRIC_SHOP_RESULTS_URL`) and `GAS_ID`.
- **Duquesne login works but no usage or bill**: the site's bot protection blocked the request. Make sure Chrome is installed (`make browser-install`) and `DUQUESNE_USE_PUPPETEER=1`. Bot protection can be stricter from cloud/datacenter IPs.
- **"Unknown gas price unit"**: set `GAS_ZIPCODE`, or `GAS_PRICE_UNIT` (`mcf`, `ccf` or `therm`).
- **Time-of-Use uses fallback rates**: Duquesne's tariff page can't currently be scraped; keep the `TOU_*` variables current.

## License

[MIT](LICENSE)

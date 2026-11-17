# Home Assistant setup

Three integrations and one add-on, each optional, that together put your Pennsylvania
gas and electric usage in the Energy dashboard and tell you when switching supplier
(or to Duquesne Light Time-of-Use) pays:

| Piece | What it gives you | Needs |
|---|---|---|
| [Duquesne Light collector](#duquesne-light-collector) add-on + integration | Hourly electricity use and cost in the Energy dashboard; your supplier, supply rate and Price to Compare from your bills | A duquesnelight.com login |
| [Peoples Gas](#peoples-gas) integration | Monthly gas use and cost in the Energy dashboard; Price to Compare, commodity rate, last bill, balance | A peopleseaccount.com login |
| [Energy Shopper](#energy-shopper) integration | Ranks PA Power Switch / PA Gas Switch offers against your usage and rate; switch/stay sensors, repair issues and a notification blueprint | A usage statistic, from the above or any other source |

Energy Shopper works without the other two: any gas or energy statistic (core Opower, a
meter, Emporia Vue) can be its usage source, and rates can be typed in.

> **Unofficial.** Not affiliated with Duquesne Light, Peoples Natural Gas, Opower or the
> PA PUC. The utility pieces log in to *your* account once or twice a day, the way a
> browser does, and read pages that can change without notice.

These are design-doc steps 1–5 ([home-assistant-design.md](home-assistant-design.md)).
They pass offline Home Assistant tests (HA 2026.2) but are new: please report problems
in [issues](https://github.com/pkmnct/pa-power-notifier/issues).

## Installing the integrations

Until each integration has its own HACS repository, install by copying:

1. Copy the folder(s) you want from this repository's `custom_components/` into your
   Home Assistant `config/custom_components/` (for example with the File editor or
   Samba add-on): `duquesne_collector/`, `peoples_gas/`, `energy_shopper/`.
2. Restart Home Assistant.
3. Settings → Devices & services → **Add integration**, and search for the name.

Each folder is self-contained (it carries the copy of the shared ranking code it needs).
Requires Home Assistant 2026.2 or newer.

## Duquesne Light collector

duquesnelight.com blocks plain HTTP clients and prints your supplier and supply rate only
on bill PDFs, so a small **collector** with headless Chromium does the fetching and serves
the results on your network. The integration only talks to the collector.

**1. Run the collector**, either way:

- *Home Assistant OS / Supervised:* Settings → Add-ons → Add-on Store → ⋮ →
  Repositories → add `https://github.com/pkmnct/pa-power-notifier`. Install **Duquesne
  Light collector**, set your username and password, start it. See the add-on's
  Documentation tab ([collector/DOCS.md](../collector/DOCS.md)).
- *Docker Compose* (any install): set `DUQUESNE_USERNAME` / `DUQUESNE_PASSWORD` in `.env`
  and run `docker compose --profile collector up -d duquesne-collector`
  ([collector/README.md](../collector/README.md)).

**2. Add the integration** **Duquesne Light (collector)**: URL `http://localhost:8765` for
the add-on (or `http://HOST:8765` for a Compose collector on another machine), plus the
API token you set with the add-on's `api_token` option, which is required (the add-on
won't start without it). Under Compose, `COLLECTOR_API_TOKEN` sets it; left empty there,
the collector generates one, never logs it, and saves it as `data/collector_api_token` on
the host.
Reconfigure later keeps your history if the address changes.

What you get:

- **Statistics** `duquesne_collector:duquesne_light_energy_consumption` (kWh) and
  `…_energy_cost` (USD), hourly, with every archived hour backfilled on first run. Add
  them in Settings → Dashboards → Energy → Electricity grid → Add consumption, with the
  cost statistic as "Use an entity tracking the total costs". Opower prices hours a day or
  two after the reading, so cost lags usage.
- **Sensors** from your newest bill: Electric supplier, Electric supply rate, Electric
  Price to Compare, Last bill amount, date, usage and supply charges; plus diagnostics
  (Collector last success, Usage data through).
- **Repair issues** when the collector can't log in, hasn't succeeded for 36 hours, or
  can't be reached for a day.

## Peoples Gas

Add **Peoples Gas (Pennsylvania)** with your peopleseaccount.com login.

- **Statistics** `peoples_gas:<account>_gas_consumption` (CCF) and `…_gas_cost` (USD), one
  row per billing cycle, about 24 months backfilled. Add them under Energy → Gas
  consumption.
- **Sensors:** Price to Compare and commodity rate (USD/CCF, from the newest bill),
  last bill amount and date, current balance.
- **Option:** save every bill PDF under `config/peoples_gas/<account>/`.

## Energy Shopper

Add **Energy Shopper (PA energy offers)** once per fuel; the first step asks which.

### Gas

1. **Market:** your ZIP and the PA Gas Switch distributor id (search your ZIP on
   [pagasswitch.com](https://www.pagasswitch.com) and copy `distributor=` from the results
   URL). The price unit is read from the site; Peoples quotes per Mcf.
2. **Usage and rate:** the gas usage statistic (Peoples Gas consumption, or any gas
   volume or energy statistic), and your current supply rate: a sensor (such as Peoples
   Gas Price to Compare) or a typed value. With neither, the utility's Price to Compare
   from PA Gas Switch is used.

### Electric

1. **Market:** search your ZIP on [papowerswitch.com](https://www.papowerswitch.com),
   pick your utility and rate class, and paste the results page URL.
2. **Usage and rate:** the hourly electricity statistic (the collector's consumption, or
   core Opower), and for each of supplier, supply rate and Price to Compare a sensor (the
   collector's) or a typed value. A supplier of "default service" or blank means you're on
   Duquesne's default supply; your current supplier counts as one you've used, so its
   new-customer-only offers are skipped.
3. **Time-of-Use (optional):** Duquesne's peak, off-peak and super-off-peak supply rates
   in ¢/kWh. They change every Jun 1 and Dec 1, so update them then. Only days with real
   hourly readings count toward your peak share.

### Both

Settings in either flow (and later under **Configure**): the savings threshold (default
5%), skip offers with a cancellation fee, renewable only (electric), suppliers you've used
before (`Name@YYYY-MM-DD; Other Name`), the rate-jump alert % and gas heat content.

Sensors per fuel: **recommendation** (`switch` / `stay`, with notes and lock-in advice),
**best offer** (supplier, with plan, price, term, fees and phone as attributes), best
offer price, annual savings ($ and %), current supply rate, Price to Compare, projected
annual use, and **offers ranked** (leaderboard and best-by-category in attributes).
Electric adds **Time-of-Use annual savings** against your current plan, with your
peak / off-peak / super-off-peak shares and what moving 25% or 50% of peak use overnight
would save.

Repair issues: **Cheaper supplier: X** (goes away on its own; ignoring it silences that
supplier only), **Time-of-Use would save about $N a year**, and **Your supply rate rose
N%** when your rate sensor or value jumps.

### Notifications

Energy Shopper fires `energy_shopper_recommendation_changed` when the recommendation or
the recommended supplier changes. The blueprint sends it to any notify action, such as
your phone:

[Import the blueprint](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fpkmnct%2Fpa-power-notifier%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fenergy_shopper%2Fnotify_on_recommendation.yaml)
([source](../blueprints/automation/energy_shopper/notify_on_recommendation.yaml)): pick
the notify action (for example `notify.mobile_app_your_phone`), which fuels, and whether
to hear about "stay" too.

Event data: `fuel`, `recommendation`, `previous`, `supplier`, `plan_type`,
`time_of_use`, `price`, `unit`, `current_price`, `annual_savings`, `savings_pct`, `term`,
`phone`, `more_info`, `entry_id`.

## Troubleshooting

- **Energy Shopper says "Usage statistic … not found (yet)":** the source hasn't
  imported anything yet; it fixes itself on the next update after the first import.
- **"Ranked by listed price":** no usage statistic, so monthly fees and seasonality
  aren't counted.
- **Electric recommendation ignores your supplier:** check the supplier and supply-rate
  sensors have values; without a rate the comparison falls back to the Price to Compare
  (it says so in the recommendation's notes).
- Debug logs: add `custom_components.energy_shopper` (or `.peoples_gas`,
  `.duquesne_collector`) under `logger:` in `configuration.yaml`.

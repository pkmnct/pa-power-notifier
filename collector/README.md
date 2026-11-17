# Duquesne Light collector

Fetches your Duquesne Light usage and bills once a day and serves them as a small,
read-only JSON API on your local network. It exists because duquesnelight.com sits
behind an Incapsula bot wall (it needs headless Chromium), and the supplier and supply
rate are printed only on the bill PDFs. Home Assistant can't do either, so this runs
beside it, as a Home Assistant add-on or a Docker Compose service, from one image.
See `docs/home-assistant-design.md` for how it fits with the rest.

Credentials live only in the collector. The archive is merge-only, the same files the
standalone notifier keeps in `data/`, so history survives Opower dropping old data:
`hourly_usage.json`, `daily_usage.json`, `bills/` (PDFs + `index.json`), plus
`collector_status.json`.

## Install

### Home Assistant add-on

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/pkmnct/pa-power-notifier`.
2. Install **Duquesne Light collector**, set the username and password, and start it.
3. The API is on port 8765. Change the host port in the add-on's **Network** section
   rather than in the options: the container always listens on 8765.

Options:

| Option | Default | Meaning |
|---|---|---|
| `username`, `password` | — | duquesnelight.com login |
| `run_at` | `04:30` | Local time(s) of the daily fetch, `HH:MM[,HH:MM]` |
| `api_token` | generated | Clients must send `Authorization: Bearer <token>`. Empty: generated on first start, logged once and saved as `collector_api_token` in the data folder. `none` turns authentication off |
| `download_bills` | `true` | Download new bill PDFs each run (supplier + supply rate) |
| `hourly_lookback_days` | `400` | How far back to page hourly usage |
| `log_level` | `info` | |

The add-on pulls `ghcr.io/pkmnct/pa-power-notifier-collector:<version>`. That image is
built from the root `Dockerfile`'s `collector` target and has to be published for each
release, for both architectures:

```bash
docker buildx build --platform linux/amd64,linux/arm64 --target collector -t ghcr.io/pkmnct/pa-power-notifier-collector:0.1.0 --push .
```

### Docker Compose

Set `DUQUESNE_USERNAME` and `DUQUESNE_PASSWORD` in `.env` (and optionally
`COLLECTOR_API_TOKEN`, `COLLECTOR_RUN_AT`, `COLLECTOR_PORT`), then:

```bash
docker compose --profile collector up -d duquesne-collector
```

The service is in a Compose profile, so a plain `docker compose up` still starts only
the notifier. It shares `./data` with the notifier.

### Without Docker

From the repo root, with `make venv browser-install` done:

```bash
.venv/bin/python collector/collector.py
```

`--once` fetches once and exits (exit code 1 on failure). `--no-schedule` serves the API
over the existing archive and never fetches.

## API

Every response is JSON. Every route except `/health` needs `Authorization: Bearer <token>`
(`401` without it), unless the token is set to `none`. Other methods get `405`. Your usage
and bills say a lot about a household, so the API isn't open by default: with no token
configured, one is generated on first start, logged once ("Generated an API token"), and
saved as `collector_api_token` in the data folder.

### `GET /usage?start=&end=`

`start` and `end` are dates (`YYYY-MM-DD`, local time) or ISO 8601 datetimes. A date
`end` includes that whole day; a datetime `end` is exclusive. The default is the last 30
days. Hours are matched by their start time.

```json
{
  "start": "2026-11-01T00:00:00-04:00",
  "end": "2026-11-02T00:00:00-05:00",
  "unit": "kWh",
  "currency": "USD",
  "hourly": [{"start": "2026-11-01T00:00:00-04:00", "kwh": 1.62, "cost": 0.4396}],
  "daily": [{"date": "2026-11-01", "kwh": 31.2, "cost": 8.11}]
}
```

`cost` is `null` while Opower hasn't priced a period yet (it fills in $ a day or two
after the kWh read). Daylight-saving days have 23 or 25 hours.

### `GET /bills`

Oldest bill first. `supplier` is `null` (and `default_service` true) for bills on
Duquesne default service. `current` is the newest dated bill.

```json
{
  "bills": [
    {
      "bill_id": "100000000001",
      "date": "2026-10-27",
      "amount_due": 210.55,
      "supplier": "Example Energy of PA LLC",
      "default_service": false,
      "supply_rate_per_kwh": 0.12,
      "price_to_compare_per_kwh": 0.1414,
      "kwh": 900.4,
      "supply_charges": 108.05,
      "rate_schedule": "RS-Residential Service"
    }
  ],
  "current": {"bill_id": "100000000001", "date": "2026-10-27", "...": "..."}
}
```

### `GET /status`

```json
{
  "service": "duquesne-collector",
  "version": "0.1.0",
  "started_at": "2026-11-15T13:06:01-05:00",
  "first_started_at": "2026-11-10T09:12:44-05:00",
  "running": false,
  "next_run": "2026-11-16T04:30:00-05:00",
  "last_success": "2026-11-15T13:08:07-05:00",
  "last_error": {"at": "...", "type": "auth", "message": "Duquesne Light login rejected (HTTP 401)"},
  "last_run": {"started_at": "...", "finished_at": "...", "ok": true, "hourly_added": 264,
               "daily_added": 11, "bills_added": 0, "warnings": []},
  "archive": {"hourly_records": 10554, "hourly_latest": "2026-11-14T23:00:00-05:00",
              "daily_records": 90, "daily_latest": "2026-11-13", "bills": 13,
              "latest_bill_date": "2026-10-27"}
}
```

`started_at` is this process's start; `first_started_at` is the first start this
archive saw, kept across restarts (`1970-01-01T00:00:00+00:00` for a collector that ran
without success before this field existed: its real first start is unknown). `last_error.type` is `auth` (bad or missing credentials), `blocked` (logged in, but the
bot wall kept the usage token out of reach), or an exception name. `last_error` stays
set after a later success, so compare its time with `last_success`.

### `GET /health`

`{"ok": true}`. No token needed; the image's Docker healthcheck uses it.

## Environment

| Variable | Default | |
|---|---|---|
| `DUQUESNE_USERNAME`, `DUQUESNE_PASSWORD` | — | Login |
| `COLLECTOR_PORT` / `COLLECTOR_HOST` | `8765` / `0.0.0.0` | API bind |
| `COLLECTOR_API_TOKEN` | generated | Bearer token; empty generates one, `none` turns auth off |
| `COLLECTOR_RUN_AT` | `04:30` | Daily fetch time(s), local (`TZ`) |
| `COLLECTOR_RUN_ON_START` | `auto` | `auto` runs at start unless a run succeeded in the last 20 h; `1` / `0` |
| `COLLECTOR_BILLS` | `1` | Download bill PDFs |
| `DATA_DIR`, `HOURLY_USAGE_FILE`, `DAILY_USAGE_FILE`, `BILL_ARCHIVE_DIR`, `HOURLY_LOOKBACK_DAYS`, `DUQUESNE_PUPPETEER_*` | | As for the notifier |

The image starts as root only so `run.sh` can map add-on options and make `/data`
writable for uid 1000. It then runs the collector as that user.

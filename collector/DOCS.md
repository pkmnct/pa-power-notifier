# Duquesne Light collector

Fetches your Duquesne Light usage and bills once a day and serves them on your network
as a small read-only JSON API. The **Duquesne Light (collector)** integration reads that
API to put your hourly usage and cost in the Energy dashboard and your supplier, supply
rate and Price to Compare in sensors.

It runs as an add-on because duquesnelight.com sits behind a bot wall that needs a real
(headless) browser, and your supplier and supply rate appear only on the bill PDFs.
Home Assistant itself can do neither.

> **Unofficial.** Not affiliated with Duquesne Light or Opower. It logs in to *your*
> account once a day, the way a browser does.

## Setup

1. Set **username** and **password** to your duquesnelight.com login, and **api_token**
   to a long random value of your choosing (it guards your usage and bills; it's never
   written to the log). Start the add-on. The first run fetches about 400 days of hourly
   usage and every bill, which takes a few minutes; watch the **Log** tab.
2. Add the integration: Settings → Devices & services → Add integration →
   **Duquesne Light (collector)**, with URL `http://localhost:8765` and that same token.

## Options

| Option | Default | |
|---|---|---|
| `username`, `password` | | duquesnelight.com login |
| `run_at` | `04:30` | Local time(s) of the daily fetch, `HH:MM` or `HH:MM,HH:MM` |
| `api_token` | generated | The API token clients must send (`Authorization: Bearer <token>`). Set your own and enter the same value in the Duquesne Light (collector) integration. Empty: one is generated and kept inside the add-on (never logged), which keeps the API closed but can't be read from Home Assistant. `none` turns authentication off |
| `download_bills` | on | Download new bill PDFs (where the supplier and supply rate come from) |
| `hourly_lookback_days` | `400` | How far back hourly usage is paged on each run |
| `log_level` | `info` | |

The API listens on port 8765 inside the container. To use another host port, change
it in the **Network** section, not in the options.

## Data

Everything is kept under the add-on's `/data`, merge-only, so history survives after
Opower stops serving it: hourly and daily usage JSON, bill PDFs with an index, and the
collector's status. It's included in Home Assistant backups.

## Troubleshooting

- **"Duquesne collector can't log in"** repair issue, or `last_error.type` `auth` in
  `/status`: check the username and password, then restart the add-on.
- **`blocked`** in the log: the login worked but the bot wall kept the usage token out
  of reach. It usually clears on the next run; if it persists, Duquesne may have changed
  its site.
- `http://localhost:8765/status` (from Home Assistant's host) shows the last run, the
  last error and what's archived.

Full API reference: [collector/README.md](https://github.com/pkmnct/pa-power-notifier/blob/main/collector/README.md).

#!/usr/bin/env python3
"""
Duquesne Light collector: a daily fetch into the merge-only archive, plus a small
read-only JSON API over that archive, in one process.

Duquesne's site needs headless Chromium (Puppeteer, browser/) and the supplier and
supply rate exist only in the bill PDFs, so this runs beside Home Assistant as an
add-on or a container rather than inside it. See collector/README.md.

  GET /usage?start=&end=   hourly and daily kWh and cost
  GET /bills               date, amount, supplier and supply rate per bill
  GET /status              last successful run, last error, archive counts
  GET /health              liveness (never needs the token)

The HTTP server is the standard library's (http.server): four GET routes over local
JSON files need no framework, and the image stays free of extra dependencies.

Env:
  DUQUESNE_USERNAME / DUQUESNE_PASSWORD   login (required for fetching)
  COLLECTOR_PORT        API port (default 8765)
  COLLECTOR_HOST        bind address (default 0.0.0.0)
  COLLECTOR_API_TOKEN   if set, every route but /health needs "Authorization: Bearer <token>"
  COLLECTOR_RUN_AT      local time(s) for the daily fetch, "HH:MM[,HH:MM...]" (default 04:30)
  COLLECTOR_RUN_ON_START  auto (default: run if no success in the last 20 h), 1, or 0
  COLLECTOR_BILLS       1 (default) to download new bill PDFs each run, 0 to skip
  DATA_DIR, HOURLY_USAGE_FILE, DAILY_USAGE_FILE, BILL_ARCHIVE_DIR, HOURLY_LOOKBACK_DAYS,
  DUQUESNE_* browser options — as for the notifier.

Run:  python collector/collector.py [--once | --no-schedule]
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env_bootstrap import configure_logging_from_env, data_dir  # noqa: E402

logger = logging.getLogger("collector")

VERSION = "0.1.0"
DEFAULT_PORT = 8765
DEFAULT_RUN_AT = "04:30"
DEFAULT_USAGE_DAYS = 30
RECENT_SUCCESS = timedelta(hours=20)
# first_started_at for a pre-existing collector that never succeeded: "long ago".
FIRST_START_UNKNOWN = "1970-01-01T00:00:00+00:00"

# Held while the fetch job writes an archive file and while a request reads one, so
# the API never sees a half-written JSON file.
DATA_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _truthy(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Paths (same defaults and overrides as the notifier)
# ---------------------------------------------------------------------------

def hourly_path() -> str:
    return os.getenv("HOURLY_USAGE_FILE") or str(data_dir() / "hourly_usage.json")


def daily_path() -> str:
    return os.getenv("DAILY_USAGE_FILE") or str(data_dir() / "daily_usage.json")


def bills_dir() -> str:
    return os.getenv("BILL_ARCHIVE_DIR") or str(data_dir() / "bills")


def status_path() -> str:
    return str(data_dir() / "collector_status.json")


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Could not read %s: %s", path, e)
        return {}


def _write_json(path: str, data: dict) -> None:
    """Write via a temp file and rename, so readers see the old or the new file, never half."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# API responses (pure: archive files in, JSON-ready dicts out)
# ---------------------------------------------------------------------------

class BadRequest(ValueError):
    pass


def parse_range(start: Optional[str], end: Optional[str], now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """
    [start, end) as aware datetimes. Each bound is a date (YYYY-MM-DD, local time) or
    an ISO datetime; a date `end` includes that whole day. Default: the last 30 days.
    """
    now = now or _now()

    def one(value: str, is_end: bool) -> datetime:
        value = value.strip()
        try:
            if len(value) == 10:
                d = date.fromisoformat(value)
                if is_end:
                    d += timedelta(days=1)
                # Naive → local, with that date's own UTC offset (DST-aware via TZ)
                return datetime(d.year, d.month, d.day).astimezone()
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise BadRequest(f"invalid {'end' if is_end else 'start'}: {value!r} (use YYYY-MM-DD or ISO 8601)")
        return dt if dt.tzinfo else dt.astimezone()

    end_dt = one(end, True) if end else now
    start_dt = one(start, False) if start else end_dt - timedelta(days=DEFAULT_USAGE_DAYS)
    if start_dt >= end_dt:
        raise BadRequest("start must be before end")
    return start_dt, end_dt


def _cost(kwh: float, cost: Any) -> Optional[float]:
    # Opower fills in $ a day or two after the kWh read; 0 with usage means "not priced yet".
    cost = float(cost or 0)
    return None if cost == 0 and kwh > 0 else round(cost, 6)


def usage_response(hourly_file: str, daily_file: str, start_dt: datetime, end_dt: datetime) -> dict:
    hourly = []
    for r in _read_json(hourly_file).get("records") or []:
        try:
            dt = datetime.fromisoformat(r["dt"])
        except (KeyError, ValueError):
            continue
        if start_dt <= dt < end_dt:
            kwh = float(r.get("kwh") or 0)
            hourly.append((dt, {"start": r["dt"], "kwh": round(kwh, 6), "cost": _cost(kwh, r.get("cost"))}))
    hourly.sort(key=lambda t: t[0])

    first_day = start_dt.date().isoformat()
    # A day is in range when it starts before end_dt.
    last_day = (end_dt - timedelta(microseconds=1)).date().isoformat()
    daily = []
    for r in _read_json(daily_file).get("records") or []:
        d = r.get("date") or ""
        if first_day <= d <= last_day:
            kwh = float(r.get("kwh") or 0)
            daily.append({"date": d, "kwh": round(kwh, 6), "cost": _cost(kwh, r.get("cost"))})
    daily.sort(key=lambda r: r["date"])

    return {
        "start": _iso(start_dt),
        "end": _iso(end_dt),
        "unit": "kWh",
        "currency": "USD",
        "hourly": [h for _, h in hourly],
        "daily": daily,
    }


def _bill_row(entry: dict) -> dict:
    egs = entry.get("egs") or {}
    supplier = entry.get("supplier")
    if supplier is None:
        supplier = egs.get("supplier") or ""
    details = entry.get("details") or {}
    return {
        "bill_id": entry.get("bill_id"),
        "date": entry.get("bill_date") or egs.get("bill_date") or None,
        "amount_due": details.get("amount_due_usd"),
        # null supplier = Duquesne default service (no EGS on the bill)
        "supplier": supplier or None,
        "default_service": not supplier,
        "supply_rate_per_kwh": egs.get("supply_rate_per_kwh"),
        "price_to_compare_per_kwh": egs.get("price_to_compare"),
        "kwh": egs.get("kwh_on_bill"),
        "supply_charges": egs.get("supply_charges_usd"),
        "rate_schedule": egs.get("rate_schedule") or None,
    }


def bills_response(archive_dir: str) -> dict:
    from usage_archive import BillArchive

    rows = [_bill_row(e) for e in BillArchive(archive_dir).entries()]
    dated = [r for r in rows if r["date"]]
    return {"bills": rows, "current": dated[-1] if dated else None}


def _archive_summary() -> dict:
    hourly = _read_json(hourly_path())
    daily = _read_json(daily_path())
    bills = _read_json(os.path.join(bills_dir(), "index.json")).get("bills") or {}
    dates = sorted(b.get("bill_date") for b in bills.values() if b.get("bill_date"))
    return {
        "hourly_records": hourly.get("record_count", 0),
        "hourly_latest": hourly.get("latest"),
        "daily_records": daily.get("record_count", 0),
        "daily_latest": daily.get("latest"),
        "bills": len(bills),
        "latest_bill_date": dates[-1] if dates else None,
    }


# ---------------------------------------------------------------------------
# Fetch job
# ---------------------------------------------------------------------------

class Collector:
    def __init__(self):
        self.state = _read_json(status_path())
        self.state.setdefault("last_success", None)
        self.state.setdefault("last_error", None)
        self.state.setdefault("last_run", None)
        self.running = False
        self.next_run: Optional[datetime] = None
        self.started_at = _now()
        # Kept across restarts, so a collector that has never succeeded can't look fresh
        # just because it was restarted.
        if not self.state.get("first_started_at"):
            if self.state.get("last_run") and not self.state.get("last_success"):
                # Runs from before this field existed, none successful: it has been failing
                # for an unknown time, so don't grant it a fresh grace period.
                self.state["first_started_at"] = FIRST_START_UNKNOWN
            else:
                self.state["first_started_at"] = _iso(self.started_at)
            self._save_state()
        self._lock = threading.Lock()

    def _save_state(self) -> None:
        try:
            _write_json(status_path(), self.state)
        except Exception as e:
            logger.warning("Could not save collector status: %s", e)

    def status(self) -> dict:
        with DATA_LOCK:
            archive = _archive_summary()
        return {
            "service": "duquesne-collector",
            "version": VERSION,
            "started_at": _iso(self.started_at),
            "first_started_at": self.state.get("first_started_at"),
            "running": self.running,
            "next_run": _iso(self.next_run),
            "last_success": self.state.get("last_success"),
            "last_error": self.state.get("last_error"),
            "last_run": self.state.get("last_run"),
            "archive": archive,
        }

    def run_once(self) -> bool:
        """One fetch-and-archive pass. Returns True on success; never raises."""
        if not self._lock.acquire(blocking=False):
            logger.info("A run is already in progress")
            return False
        self.running = True
        started = _now()
        run: Dict[str, Any] = {"started_at": _iso(started), "warnings": []}
        ok = False
        try:
            self._fetch(run)
            ok = True
        except Exception as e:
            from energy_shopper_core.utilities.duquesne import DuquesneAuthError, DuquesneBlockedError

            kind = (
                "auth" if isinstance(e, DuquesneAuthError)
                else "blocked" if isinstance(e, DuquesneBlockedError)
                else type(e).__name__
            )
            logger.error("Collector run failed (%s): %s", kind, e, exc_info=kind not in ("auth", "blocked"))
            self.state["last_error"] = {"at": _iso(_now()), "type": kind, "message": str(e)}
        finally:
            run["finished_at"] = _iso(_now())
            run["ok"] = ok
            if ok:
                self.state["last_success"] = run["finished_at"]
            self.state["last_run"] = run
            self._save_state()
            self.running = False
            self._lock.release()
        return ok

    def _fetch(self, run: Dict[str, Any]) -> None:
        from duquesne_light_analyzer import DuquesneLightAnalyzer
        from energy_shopper_core.utilities import duquesne as core
        from usage_archive import BillArchive, DailyElectricUsageStore, HourlyUsageStore

        analyzer = DuquesneLightAnalyzer()
        client = analyzer.client
        # Raises DuquesneAuthError / DuquesneBlockedError: the run's main failure modes.
        client.ensure_authenticated()

        lookback = int(os.getenv("HOURLY_LOOKBACK_DAYS") or core.DEFAULT_HOURLY_LOOKBACK_DAYS)
        hourly = client.fetch_hourly_history(lookback)
        with DATA_LOCK:
            store = HourlyUsageStore(hourly_path())
            run["hourly_added"] = store.merge(hourly)
            store.save()
        run["hourly_fetched"] = len(hourly)
        if not hourly:
            run["warnings"].append("Hourly fetch returned no records")

        response = client.fetch_daily_response()
        analysis = core.analyze_response(response)
        rows = (analysis or {}).get("usage_data") or []
        with DATA_LOCK:
            _write_json(analyzer.response_file, response)
            daily = DailyElectricUsageStore(daily_path())
            run["daily_added"] = daily.merge(rows)
            daily.save()
        run["daily_fetched"] = len(rows)

        if _truthy(os.getenv("COLLECTOR_BILLS"), True):
            # Same order as the notifier: refresh the newest bill, then fill in older ones.
            # Not under DATA_LOCK: downloads take minutes, and the bill index is written atomically.
            egs = analyzer.fetch_current_egs_from_bill()
            run["bills_added"] = analyzer.backfill_bill_archive(BillArchive(bills_dir()))
            if egs is None:
                run["warnings"].append(
                    "Latest bill: no EGS supply rate (download failed, or the bill is on default service)"
                )
        logger.info("Collector run finished: %s", {k: v for k, v in run.items() if k != "warnings"})

    # -- schedule ----------------------------------------------------------

    @staticmethod
    def run_times() -> List[Tuple[int, int]]:
        spec = os.getenv("COLLECTOR_RUN_AT") or DEFAULT_RUN_AT
        out = []
        for part in spec.split(","):
            try:
                hh, mm = part.strip().split(":")
                h, m = int(hh), int(mm)
                if not (0 <= h < 24 and 0 <= m < 60):
                    raise ValueError
            except ValueError:
                raise SystemExit(f"COLLECTOR_RUN_AT: invalid time {part!r} (use HH:MM)")
            out.append((h, m))
        return sorted(set(out))

    @staticmethod
    def next_after(now: datetime, times: List[Tuple[int, int]]) -> datetime:
        """
        The next HH:MM local time after `now`. Wall-clock times are built naive and then
        given the local zone's offset for *that* date, so a run after a DST change
        isn't an hour off (`now` carries a fixed offset).
        """
        wall = now.astimezone().replace(tzinfo=None)
        for days in (0, 1, 2):
            day = wall + timedelta(days=days)
            for h, m in times:
                t = day.replace(hour=h, minute=m, second=0, microsecond=0).astimezone()
                if t > now:
                    return t
        raise AssertionError("unreachable")

    def should_run_on_start(self) -> bool:
        mode = (os.getenv("COLLECTOR_RUN_ON_START") or "auto").strip().lower()
        if mode != "auto":
            return _truthy(mode, True)
        last = self.state.get("last_success")
        if not last:
            return True
        try:
            return _now() - datetime.fromisoformat(last) > RECENT_SUCCESS
        except ValueError:
            return True

    def schedule_loop(self, stop: threading.Event) -> None:
        times = self.run_times()
        if self.should_run_on_start():
            self.run_once()
        while not stop.is_set():
            self.next_run = self.next_after(_now(), times)
            logger.info("Next Duquesne fetch at %s", _iso(self.next_run))
            while not stop.is_set() and _now() < self.next_run:
                stop.wait(min(60.0, max(1.0, (self.next_run - _now()).total_seconds())))
            if not stop.is_set():
                self.run_once()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(collector: Collector, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"duquesne-collector/{VERSION}"

        def _send(self, code: int, body: dict, headers: Optional[dict] = None) -> None:
            data = json.dumps(body, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization") or ""
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip().encode(), token.encode())

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            route = url.path.rstrip("/") or "/"
            if route == "/health":
                return self._send(200, {"ok": True})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            try:
                if route == "/usage":
                    q = parse_qs(url.query)
                    start_dt, end_dt = parse_range((q.get("start") or [None])[0], (q.get("end") or [None])[0])
                    with DATA_LOCK:
                        body = usage_response(hourly_path(), daily_path(), start_dt, end_dt)
                    return self._send(200, body)
                if route == "/bills":
                    with DATA_LOCK:
                        body = bills_response(bills_dir())
                    return self._send(200, body)
                if route == "/status":
                    return self._send(200, collector.status())
                return self._send(404, {"error": "not found", "routes": ["/usage", "/bills", "/status", "/health"]})
            except BadRequest as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                logger.exception("Request failed: %s", self.path)
                return self._send(500, {"error": f"internal error: {type(e).__name__}"})

        def do_POST(self):  # noqa: N802
            self._send(405, {"error": "read-only API"}, {"Allow": "GET"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt, *args):
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="fetch once and exit (no API)")
    group.add_argument("--no-schedule", action="store_true", help="serve the API only, never fetch")
    args = parser.parse_args(argv)

    configure_logging_from_env()
    os.environ.setdefault("DUQUESNE_USE_PUPPETEER", "1")  # the site always needs the browser
    collector = Collector()
    if args.once:
        return 0 if collector.run_once() else 1

    token = (os.getenv("COLLECTOR_API_TOKEN") or "").strip()
    host = os.getenv("COLLECTOR_HOST") or "0.0.0.0"
    port = int(os.getenv("COLLECTOR_PORT") or DEFAULT_PORT)
    server = ThreadingHTTPServer((host, port), make_handler(collector, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    logger.info(
        "Duquesne collector API on %s:%d (%s); data in %s",
        host, port, "bearer token required" if token else "no auth token set", data_dir(),
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if args.no_schedule:
        stop.wait()
    else:
        collector.schedule_loop(stop)
    server.shutdown()
    logger.info("Collector stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

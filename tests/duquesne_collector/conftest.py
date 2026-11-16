"""Fixtures for the duquesne_collector tests: a fake collector API behind aioclient_mock."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.duquesne_collector.const import CONF_STATISTIC_KEY, DOMAIN
from homeassistant.const import CONF_URL
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

URL = "http://collector.test:8765"
HOURS = 72          # hourly archive length
UNPRICED = 30       # newest hours Opower hasn't priced yet
DAILY_ONLY = 10     # older days that only have daily totals
KWH = 1.5           # every hour
RATE = 0.25         # $/kWh


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The integration depends on recorder, which must start before hass is set up."""
    yield


def _iso(dt: datetime) -> str:
    return dt_util.as_local(dt).isoformat()


class FakeCollector:
    def __init__(self) -> None:
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        self.first_hour = now - timedelta(hours=HOURS)
        self.hourly: list[dict] = []
        for i in range(HOURS):
            start = self.first_hour + timedelta(hours=i)
            priced = i < HOURS - UNPRICED
            self.hourly.append({"start": _iso(start), "kwh": KWH, "cost": KWH * RATE if priced else None})
        first_day = dt_util.as_local(self.first_hour).date()
        self.daily = [
            {"date": (first_day - timedelta(days=n)).isoformat(), "kwh": 30.0, "cost": 7.5}
            for n in range(DAILY_ONLY, 0, -1)
        ]
        self.last_success = dt_util.now() - timedelta(hours=2)
        self.first_started: datetime | None = None
        self.last_error: dict | None = None
        self.reject_auth = False
        self.down = False
        self.usage_starts: list[str] = []
        self.bills = [
            {"bill_id": "1", "date": "2026-09-26", "amount_due": 190.0, "supplier": None,
             "default_service": True, "supply_rate_per_kwh": None, "price_to_compare_per_kwh": 0.1414,
             "kwh": 850.0, "supply_charges": 120.19, "rate_schedule": "RS-Residential Service"},
            {"bill_id": "2", "date": "2026-10-27", "amount_due": 210.55, "supplier": "Example Energy of PA LLC",
             "default_service": False, "supply_rate_per_kwh": 0.12, "price_to_compare_per_kwh": 0.1414,
             "kwh": 900.4, "supply_charges": 108.05, "rate_schedule": "RS-Residential Service"},
        ]

    def price_all(self) -> None:
        for h in self.hourly:
            h["cost"] = h["kwh"] * RATE

    def add_hours(self, n: int, priced: bool = False) -> None:
        last = dt_util.parse_datetime(self.hourly[-1]["start"])
        for i in range(1, n + 1):
            self.hourly.append({"start": _iso(last + timedelta(hours=i)), "kwh": KWH, "cost": KWH * RATE if priced else None})

    def status(self) -> dict:
        return {
            "service": "duquesne-collector",
            "version": "0.1.0",
            "started_at": _iso(dt_util.now() - (timedelta(hours=1) if self.first_started else timedelta(days=5))),
            "first_started_at": _iso(self.first_started) if self.first_started else None,
            "running": False,
            "next_run": _iso(dt_util.now() + timedelta(hours=10)),
            "last_success": _iso(self.last_success) if self.last_success else None,
            "last_error": self.last_error,
            "last_run": {"ok": True},
            "archive": {"hourly_records": len(self.hourly), "hourly_latest": self.hourly[-1]["start"],
                        "daily_records": len(self.daily), "bills": len(self.bills)},
        }

    def usage(self, start: str) -> dict:
        self.usage_starts.append(start)
        if len(start) == 10:
            begin = dt_util.as_utc(datetime.fromisoformat(start).replace(tzinfo=dt_util.get_default_time_zone()))
        else:
            begin = dt_util.as_utc(datetime.fromisoformat(start.replace("Z", "+00:00")))
        hourly = [h for h in self.hourly if dt_util.parse_datetime(h["start"]) >= begin]
        daily = [d for d in self.daily if d["date"] >= dt_util.as_local(begin).date().isoformat()]
        return {"unit": "kWh", "currency": "USD", "hourly": hourly, "daily": daily}

    async def respond(self, method, url, data):
        if self.down:
            raise __import__("aiohttp").ClientConnectionError("down")
        if self.reject_auth:
            return AiohttpClientMockResponse(method, url, status=401, json={"error": "unauthorized"})
        path = url.path
        if path == "/status":
            body = self.status()
        elif path == "/bills":
            body = {"bills": self.bills, "current": self.bills[-1]}
        elif path == "/usage":
            body = self.usage(url.query.get("start", ""))
        else:
            return AiohttpClientMockResponse(method, url, status=404, json={"error": "not found"})
        return AiohttpClientMockResponse(method, url, json=body)



@pytest.fixture
def collector(aioclient_mock: AiohttpClientMocker) -> FakeCollector:
    fake = FakeCollector()
    for path in ("/status", "/bills", "/usage"):
        aioclient_mock.get(f"{URL}{path}", side_effect=fake.respond)
    return fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Duquesne Light",
        unique_id="http_collector_test_8765",
        data={CONF_URL: URL, CONF_STATISTIC_KEY: "duquesne_light"},
    )


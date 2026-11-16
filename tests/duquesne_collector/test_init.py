"""Setup, statistics import, sensors and collector-health repair issues."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.duquesne_collector.api import CollectorError
from custom_components.duquesne_collector.const import CONF_STATISTIC_KEY, COST_GRACE, DOMAIN
from custom_components.duquesne_collector.coordinator import (
    Point,
    build_statistics,
    statistic_ids,
    usage_points,
)
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_API_TOKEN
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from conftest import DAILY_ONLY, HOURS, KWH, RATE, UNPRICED, FakeCollector

CONSUMPTION_ID, COST_ID = statistic_ids("duquesne_light")
S = "sensor.duquesne_light_"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _stats(hass: HomeAssistant) -> dict:
    return await hass.async_add_executor_job(
        statistics_during_period, hass, datetime(2020, 1, 1, tzinfo=dt_util.UTC), None,
        {CONSUMPTION_ID, COST_ID}, "hour", None, {"state", "sum"},
    )


def issues(hass: HomeAssistant) -> list[str]:
    return sorted(i.rsplit("_", 1)[0] for d, i in ir.async_get(hass).issues if d == DOMAIN)


async def test_backfill_then_incremental(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    stats = await _stats(hass)
    cons, cost = stats[CONSUMPTION_ID], stats[COST_ID]
    # Daily-only days first (one row at local midnight), then every hour.
    assert len(cons) == DAILY_ONLY + HOURS
    assert cons[0]["state"] == pytest.approx(30.0)
    assert cons[-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + HOURS * KWH)
    # Cost waits at the first unpriced hour.
    assert len(cost) == DAILY_ONLY + HOURS - UNPRICED
    assert cost[-1]["sum"] == pytest.approx(DAILY_ONLY * 7.5 + (HOURS - UNPRICED) * KWH * RATE)
    assert collector.usage_starts == ["2000-01-01"]

    # Next day: Opower priced the waiting hours and three new hours arrived.
    collector.price_all()
    collector.add_hours(3, priced=True)
    await _poll(hass, config_entry)
    stats = await _stats(hass)
    cons, cost = stats[CONSUMPTION_ID], stats[COST_ID]
    assert len(cons) == DAILY_ONLY + HOURS + 3
    assert len(cost) == len(cons)
    assert cost[-1]["sum"] == pytest.approx(DAILY_ONLY * 7.5 + (HOURS + 3) * KWH * RATE)
    # It asked only from the older of the two statistics' last rows, in UTC.
    assert collector.usage_starts[-1].endswith("Z")
    assert dt_util.parse_datetime(collector.usage_starts[-1]) == dt_util.as_utc(
        datetime.fromtimestamp(stats[COST_ID][DAILY_ONLY + HOURS - UNPRICED - 1]["start"], dt_util.UTC)
    ) + timedelta(hours=1)

    # Nothing new: nothing imported, sums unchanged.
    await _poll(hass, config_entry)
    assert (await _stats(hass))[CONSUMPTION_ID] == cons


async def test_sensors(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    await _setup(hass, config_entry)
    supplier = hass.states.get(S + "electric_supplier")
    assert supplier.state == "Example Energy of PA LLC"
    assert supplier.attributes["default_service"] is False
    rate = hass.states.get(S + "electric_supply_rate")
    assert float(rate.state) == pytest.approx(0.12)
    assert rate.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD/kWh"
    assert float(hass.states.get(S + "electric_price_to_compare").state) == pytest.approx(0.1414)
    assert float(hass.states.get(S + "last_bill_amount").state) == pytest.approx(210.55)
    assert hass.states.get(S + "last_bill_date").state == "2026-10-27"
    assert float(hass.states.get(S + "last_bill_usage").state) == pytest.approx(900.4)
    assert hass.states.get(S + "collector_last_success").state == dt_util.as_utc(collector.last_success).isoformat(timespec="seconds")
    assert hass.states.get(S + "usage_data_through").state != "unknown"
    assert issues(hass) == []


async def test_default_service(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    collector.bills = collector.bills[:1]
    await _setup(hass, config_entry)
    assert hass.states.get(S + "electric_supplier").state == "Duquesne Light default service"
    assert hass.states.get(S + "electric_supply_rate").state == "unknown"


async def test_login_failing_and_stale(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    collector.last_success = dt_util.now() - timedelta(days=3)
    collector.last_error = {"at": dt_util.now().isoformat(), "type": "auth", "message": "Duquesne Light login rejected (HTTP 401)"}
    await _setup(hass, config_entry)
    assert issues(hass) == ["collector_auth", "collector_stale"]
    stale = ir.async_get(hass).async_get_issue(DOMAIN, f"collector_stale_{config_entry.entry_id}")
    assert "login rejected" in stale.translation_placeholders["error"]

    # The user fixes the add-on; the next run succeeds.
    collector.last_success = dt_util.now()
    await _poll(hass, config_entry)
    assert issues(hass) == []


async def test_old_error_is_not_current(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    collector.last_error = {"at": (dt_util.now() - timedelta(days=4)).isoformat(), "type": "auth", "message": "old"}
    await _setup(hass, config_entry)
    assert issues(hass) == []


async def test_unreachable(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    await _setup(hass, config_entry)
    collector.down = True
    await _poll(hass, config_entry)
    assert hass.states.get(S + "electric_supplier").state == "unavailable"
    assert issues(hass) == []  # not yet: give it a day
    config_entry.runtime_data._failing_since = dt_util.utcnow() - timedelta(hours=25)  # noqa: SLF001
    await _poll(hass, config_entry)
    assert issues(hass) == ["collector_unreachable"]
    collector.down = False
    await _poll(hass, config_entry)
    assert issues(hass) == []
    assert hass.states.get(S + "electric_supplier").state == "Example Energy of PA LLC"


async def test_token_rejected_at_setup_starts_reauth(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    collector.reject_auth = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_token_rotated_later_is_reauth_not_unreachable(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    await _setup(hass, config_entry)
    config_entry.runtime_data._failing_since = dt_util.utcnow() - timedelta(hours=25)  # noqa: SLF001
    collector.reject_auth = True
    await _poll(hass, config_entry)
    assert issues(hass) == []  # it answers; it isn't unreachable
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]

    collector.reject_auth = False
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {CONF_API_TOKEN: "rotated"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "rotated"
    assert config_entry.data[CONF_STATISTIC_KEY] == "duquesne_light"
    assert config_entry.state is ConfigEntryState.LOADED


async def test_no_cost_yet_does_not_refetch_everything_hourly(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    for h in collector.hourly:
        h["cost"] = None  # this account's hours are never priced
    collector.daily = [dict(d, cost=None) for d in collector.daily]
    await _setup(hass, config_entry)
    assert COST_ID not in await _stats(hass)
    collector.add_hours(2)
    await _poll(hass, config_entry)
    assert collector.usage_starts[0] == "2000-01-01"
    assert collector.usage_starts[1].endswith("Z")  # only new hours
    stats = await _stats(hass)
    assert len(stats[CONSUMPTION_ID]) == DAILY_ONLY + HOURS + 2
    assert COST_ID not in stats

    # Opower finally prices the archive: the next daily full fetch backfills cost from the start.
    collector.price_all()
    collector.daily = [dict(d, cost=7.5) for d in collector.daily]
    config_entry.runtime_data._last_full_fetch = dt_util.utcnow() - timedelta(hours=25)  # noqa: SLF001
    await _poll(hass, config_entry)
    assert collector.usage_starts[-1] == "2000-01-01"
    stats = await _stats(hass)
    assert len(stats[COST_ID]) == len(stats[CONSUMPTION_ID])


async def test_remove_clears_issues(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    collector.last_success = dt_util.now() - timedelta(days=3)
    await _setup(hass, config_entry)
    assert issues(hass) == ["collector_stale"]
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issues(hass) == []


def test_cost_grace_skips_long_unpriced_hours() -> None:
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    old = now - COST_GRACE - timedelta(days=1)
    points = [
        Point(old, 1.0, 0.2),
        Point(old + timedelta(hours=1), 1.0, None),  # never priced: skipped
        Point(old + timedelta(hours=2), 1.0, 0.3),
        Point(now - timedelta(hours=2), 1.0, None),  # recent: wait here
        Point(now - timedelta(hours=1), 1.0, 0.4),
    ]
    cons, cost = build_statistics(points, None, 0.0, None, 0.0, now)
    assert [r["sum"] for r in cons] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [r["sum"] for r in cost] == pytest.approx([0.2, 0.5])


def test_usage_points_ignores_bad_hours_and_daily_overlap() -> None:
    body = {
        "hourly": [
            {"start": "2026-11-10T05:00:00-05:00", "kwh": 1, "cost": 0.1},
            {"start": "2026-11-10T05:30:00-05:00", "kwh": 9, "cost": 9},  # not an hour start
            {"start": "garbage", "kwh": 9},
        ],
        "daily": [
            {"date": "2026-11-09", "kwh": 20, "cost": 5},
            {"date": "2026-11-10", "kwh": 24, "cost": 6},  # has hours: not added
        ],
    }
    pts = usage_points(body, backfill=True)
    assert [p.kwh for p in pts] == [20, 1]
    assert [p.kwh for p in usage_points(body, backfill=False)] == [1]


async def test_failed_full_fetch_is_retried_next_poll(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    for h in collector.hourly:
        h["cost"] = None
    collector.daily = [dict(d, cost=None) for d in collector.daily]
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    coordinator._last_full_fetch = dt_util.utcnow() - timedelta(hours=25)  # noqa: SLF001
    original = coordinator.client.usage

    async def failing_full(start):
        if start is None:
            raise CollectorError("timed out")
        return await original(start)

    coordinator.client.usage = failing_full
    await _poll(hass, config_entry)
    coordinator.client.usage = original
    starts_before = list(collector.usage_starts)
    await _poll(hass, config_entry)
    assert collector.usage_starts[len(starts_before):] == ["2000-01-01"]  # tried again, not skipped for a day


async def test_collector_backfill_after_first_import_is_rebuilt(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """The integration is added while the collector's first run is still filling the archive."""
    full_hourly, full_daily = collector.hourly, collector.daily
    collector.hourly, collector.daily = full_hourly[-6:], []  # only the newest hours so far
    await _setup(hass, config_entry)
    assert len((await _stats(hass))[CONSUMPTION_ID]) == 6

    collector.hourly, collector.daily = full_hourly, full_daily  # the run finishes its backfill
    await _poll(hass, config_entry)
    cons = (await _stats(hass))[CONSUMPTION_ID]
    assert len(cons) == DAILY_ONLY + HOURS
    assert cons[-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + HOURS * KWH)
    sums = [r["sum"] for r in cons]
    assert sums == sorted(sums)  # one consistent running total


async def test_gap_filled_later_is_imported(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    missing = collector.hourly.pop(10)
    await _setup(hass, config_entry)
    assert len((await _stats(hass))[CONSUMPTION_ID]) == DAILY_ONLY + HOURS - 1
    collector.hourly.insert(10, missing)  # Opower served the hour on a later run
    await _poll(hass, config_entry)
    cons = (await _stats(hass))[CONSUMPTION_ID]
    assert len(cons) == DAILY_ONLY + HOURS
    assert cons[-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + HOURS * KWH)


async def test_new_hours_alone_dont_rebuild(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    await _setup(hass, config_entry)
    collector.add_hours(2, priced=True)
    await _poll(hass, config_entry)
    assert collector.usage_starts[0] == "2000-01-01"
    assert all(s.endswith("Z") for s in collector.usage_starts[1:])


async def test_never_succeeded_is_stale_across_restarts(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    collector.last_success = None
    collector.first_started = dt_util.now() - timedelta(days=3)  # started_at is recent: restarted
    await _setup(hass, config_entry)
    assert issues(hass) == ["collector_stale"]

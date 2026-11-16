"""Setup, statistics import, sensors and collector-health repair issues."""

from __future__ import annotations

from datetime import datetime, timedelta

from unittest.mock import patch

import pytest

from custom_components.duquesne_collector.api import CollectorError
from custom_components.duquesne_collector.const import CONF_STATISTIC_KEY, COST_GRACE, DOMAIN
from custom_components.duquesne_collector import coordinator as coordinator_module
from custom_components.duquesne_collector.coordinator import (
    Point,
    build_statistics,
    merge_rebuild,
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


async def test_rebuild_clears_rows_the_new_import_doesnt_cover(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """A daily-total row whose day later gets hourly data without a 00:00 reading."""
    await _setup(hass, config_entry)
    day = dt_util.as_local(collector.first_hour).date() - timedelta(days=1)  # newest daily-only day
    midnight = datetime(day.year, day.month, day.day, tzinfo=dt_util.get_default_time_zone())
    cons = (await _stats(hass))[CONSUMPTION_ID]
    assert any(r["start"] == midnight.timestamp() for r in cons)  # imported as one daily row

    # Opower serves that day's hours, all but 00:00.
    extra = [
        {"start": dt_util.as_local(midnight + timedelta(hours=h)).isoformat(), "kwh": KWH, "cost": KWH * RATE}
        for h in range(1, 24)
    ]
    collector.hourly = extra + collector.hourly
    await _poll(hass, config_entry)

    cons = (await _stats(hass))[CONSUMPTION_ID]
    stale = [r for r in cons if r["start"] == midnight.timestamp()]
    assert [r["state"] for r in stale] == [0.0]  # the old daily total no longer counts
    assert len(cons) == (DAILY_ONLY - 1) + 1 + len(extra) + HOURS
    assert cons[-1]["sum"] == pytest.approx((DAILY_ONLY - 1) * 30.0 + (len(extra) + HOURS) * KWH)
    sums = [r["sum"] for r in cons]
    assert sums == sorted(sums)


async def test_first_import_still_fails_without_a_full_read(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """With nothing stored yet there's nothing to fall back to."""
    collector.full_read_fails = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_cost_backfill_does_not_rewrite_consumption(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    for h in collector.hourly:
        h["cost"] = None  # never priced so far
    collector.daily = [dict(d, cost=None) for d in collector.daily]
    await _setup(hass, config_entry)

    collector.add_hours(2)
    collector.price_all()
    collector.daily = [dict(d, cost=7.5) for d in collector.daily]
    config_entry.runtime_data._last_full_fetch = dt_util.utcnow() - timedelta(hours=25)  # noqa: SLF001
    imported: dict[str, int] = {}
    real = coordinator_module.async_add_external_statistics

    def record(hass, metadata, rows):
        imported[metadata["statistic_id"]] = len(rows)
        return real(hass, metadata, rows)

    with patch.object(coordinator_module, "async_add_external_statistics", side_effect=record):
        await _poll(hass, config_entry)
    assert collector.usage_starts[-1] == "2000-01-01"  # the daily full read, for cost
    assert imported[CONSUMPTION_ID] == 2  # only the new hours, not the whole archive again
    assert imported[COST_ID] == DAILY_ONLY + HOURS + 2
    stats = await _stats(hass)
    assert len(stats[COST_ID]) == len(stats[CONSUMPTION_ID]) == DAILY_ONLY + HOURS + 2
    assert stats[CONSUMPTION_ID][-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + (HOURS + 2) * KWH)


async def test_restart_stays_incremental(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    """The imported count is saved, so a restart doesn't rebuild to learn it again."""
    await _setup(hass, config_entry)
    collector.add_hours(2, priced=True)
    starts_before = len(collector.usage_starts)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    new_starts = collector.usage_starts[starts_before:]
    assert new_starts and all(s.endswith("Z") for s in new_starts)
    stats = await _stats(hass)
    assert len(stats[CONSUMPTION_ID]) == DAILY_ONLY + HOURS + 2


async def test_failed_full_read_keeps_sensors(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    """When the count has to be learned again and the full read times out, import new hours only."""
    await _setup(hass, config_entry)
    collector.add_hours(2, priced=True)
    collector.full_read_fails = True
    await coordinator_module.CollectorCoordinator.async_remove_state(hass, config_entry.entry_id)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(S + "electric_supplier").state == "Example Energy of PA LLC"
    stats = await _stats(hass)
    assert len(stats[CONSUMPTION_ID]) == DAILY_ONLY + HOURS + 2
    assert stats[CONSUMPTION_ID][-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + (HOURS + 2) * KWH)
    assert collector.usage_starts[-1].endswith("Z")  # fell back to new hours

    collector.full_read_fails = False
    await _poll(hass, config_entry)
    assert collector.usage_starts[-1] == "2000-01-01"  # the full read is tried again


async def test_rebuild_keeps_history_the_archive_no_longer_holds(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """The add-on is reinstalled (its archive wiped) and its first run has only a few hours."""
    await _setup(hass, config_entry)
    before = (await _stats(hass))[CONSUMPTION_ID]
    total_before = before[-1]["sum"]

    collector.add_hours(3, priced=True)
    collector.hourly, collector.daily = collector.hourly[-5:], []  # 2 known hours + 3 new
    await _poll(hass, config_entry)
    cons = (await _stats(hass))[CONSUMPTION_ID]
    assert len(cons) == len(before) + 3  # nothing deleted
    assert [r["start"] for r in cons[: len(before)]] == [r["start"] for r in before]
    assert cons[-1]["sum"] == pytest.approx(total_before + 3 * KWH)
    sums = [r["sum"] for r in cons]
    assert sums == sorted(sums)


async def test_status_usage_race_does_not_rebuild(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """Hours archived between the /status and /usage reads make the import larger, not smaller."""
    await _setup(hass, config_entry)
    real_status = collector.status

    def status_then_new_hours() -> dict:
        body = real_status()
        collector.add_hours(2, priced=True)  # the collector's run merges right after /status
        return body

    collector.status = status_then_new_hours
    starts_before = len(collector.usage_starts)
    await _poll(hass, config_entry)
    collector.status = real_status
    assert all(s.endswith("Z") for s in collector.usage_starts[starts_before:])  # no full read
    stats = await _stats(hass)
    assert len(stats[CONSUMPTION_ID]) == DAILY_ONLY + HOURS + 2

    starts_before = len(collector.usage_starts)
    await _poll(hass, config_entry)  # and the count is right afterwards
    assert all(s.endswith("Z") for s in collector.usage_starts[starts_before:])


def test_merge_rebuild_only_zeroes_superseded_daily_totals() -> None:
    tz = dt_util.get_default_time_zone()
    day = datetime(2026, 10, 5, tzinfo=tz)
    h = lambda n: dt_util.as_utc(day + timedelta(hours=n))  # noqa: E731
    stored = [  # what HA has
        {"start": h(-24).timestamp(), "state": 20.0, "sum": 20.0},  # older history
        {"start": h(0).timestamp(), "state": 24.0, "sum": 44.0},  # day 1: a daily total only
        {"start": h(24).timestamp(), "state": 1.0, "sum": 45.0},  # day 2: hourly, incl. 00:00
        {"start": h(29).timestamp(), "state": 1.0, "sum": 46.0},  # 05:00, the archive lacks it
        {"start": h(40).timestamp(), "state": 2.0, "sum": 48.0},  # newer than the archive
    ]
    # The archive: day 1 hourly from 01:00 (no 00:00 reading), day 2 without 00:00 or 05:00.
    new = [{"start": h(n), "state": 1.0, "sum": 0.0} for n in (1, 2, 3, 25, 26, 30)]
    merged = merge_rebuild(new, stored, tz)
    by_start = {r["start"]: r["state"] for r in merged}
    assert h(-24) not in by_start  # history before the archive's first day: untouched
    assert by_start[h(0)] == 0.0  # superseded daily total, though before the first hour
    assert by_start[h(24)] == 1.0  # a real 00:00 reading (day 2 has other hours): kept
    assert by_start[h(29)] == 1.0  # an hour HA has and this archive lacks: kept
    assert by_start[h(40)] == 2.0  # past the archive's end: kept
    sums = [r["sum"] for r in merged]
    assert sums == sorted(sums)
    assert sums[0] == pytest.approx(20.0)  # continues from the older history
    assert sums[-1] == pytest.approx(20.0 + 6 * 1.0 + 1.0 + 1.0 + 2.0)


def test_merge_rebuild_keeps_ha_hours_over_an_archive_daily_total() -> None:
    tz = dt_util.get_default_time_zone()
    day = datetime(2026, 10, 5, tzinfo=tz)
    h = lambda n: dt_util.as_utc(day + timedelta(hours=n))  # noqa: E731
    stored = [{"start": h(n).timestamp(), "state": 1.0, "sum": float(n + 1)} for n in range(24)]  # day 1 hourly
    new = [{"start": h(0), "state": 24.0, "sum": 0.0}]  # the archive has day 1 as a daily total
    new += [{"start": h(24 + n), "state": 2.0, "sum": 0.0} for n in range(3)]  # day 2 hourly
    merged = merge_rebuild(new, stored, tz)
    by_start = {r["start"]: r["state"] for r in merged}
    assert by_start[h(0)] == 1.0  # HA's 00:00 hour, not the archive's 24 kWh total
    assert [by_start[h(n)] for n in range(1, 24)] == [1.0] * 23
    assert merged[-1]["sum"] == pytest.approx(24 * 1.0 + 3 * 2.0)  # day 1 counted once


async def test_reinstalled_archive_daily_history_does_not_double_count(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """A reinstalled collector has HA's older hourly days only as daily totals."""
    collector.price_all()
    await _setup(hass, config_entry)
    before = await _stats(hass)

    tz = dt_util.get_default_time_zone()
    last_day = dt_util.parse_datetime(collector.hourly[-1]["start"]).astimezone(tz).date()
    by_day: dict = {}
    for row in collector.hourly:
        d = dt_util.parse_datetime(row["start"]).astimezone(tz).date()
        if d < last_day:
            kwh, cost = by_day.get(d, (0.0, 0.0))
            by_day[d] = (kwh + row["kwh"], cost + row["cost"])
    collector.hourly = [
        r for r in collector.hourly if dt_util.parse_datetime(r["start"]).astimezone(tz).date() == last_day
    ]
    collector.daily = [{"date": d.isoformat(), "kwh": k, "cost": c} for d, (k, c) in sorted(by_day.items())]
    await _poll(hass, config_entry)  # smaller archive: rebuild
    after = await _stats(hass)
    for statistic_id in (CONSUMPTION_ID, COST_ID):
        assert after[statistic_id][-1]["sum"] == pytest.approx(before[statistic_id][-1]["sum"])


async def test_archive_shrink_resyncs_and_imports_missed_hours(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """Add-on reinstalled while HA had missed some hours; the new archive is smaller."""
    full = [dict(h) for h in collector.hourly]
    collector.hourly = full[:50] + full[55:]  # HA was down: 5 hours never imported
    await _setup(hass, config_entry)
    assert len((await _stats(hass))[CONSUMPTION_ID]) == DAILY_ONLY + HOURS - 5

    collector.hourly, collector.daily = [dict(h) for h in full[40:]], []  # new, smaller archive
    await _poll(hass, config_entry)
    cons = (await _stats(hass))[CONSUMPTION_ID]
    assert len(cons) == DAILY_ONLY + HOURS  # the missed hours are in now
    assert cons[-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + HOURS * KWH)


async def test_archive_shrink_during_restart_resyncs_on_first_poll(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    """The add-on is reinstalled while HA restarts: the saved count is stale on load."""
    full = [dict(h) for h in collector.hourly]
    collector.hourly = full[:50] + full[55:]  # HA was down: 5 hours never imported
    await _setup(hass, config_entry)
    assert len((await _stats(hass))[CONSUMPTION_ID]) == DAILY_ONLY + HOURS - 5

    collector.hourly, collector.daily = [dict(h) for h in full[40:]], []  # new, smaller archive
    assert await hass.config_entries.async_reload(config_entry.entry_id)  # the restart's one poll
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    cons = (await _stats(hass))[CONSUMPTION_ID]
    assert len(cons) == DAILY_ONLY + HOURS  # caught on the first poll, not an hour later
    assert cons[-1]["sum"] == pytest.approx(DAILY_ONLY * 30.0 + HOURS * KWH)


async def test_rebuild_keeps_newer_cost_the_archive_hasnt_priced(
    hass: HomeAssistant, collector: FakeCollector, config_entry
) -> None:
    collector.price_all()
    await _setup(hass, config_entry)
    before = (await _stats(hass))[COST_ID]
    assert len(before) == DAILY_ONLY + HOURS

    # Reinstall: the new archive has the last 30 hours, its newest 4 not priced yet.
    new = [dict(h) for h in collector.hourly[-30:]]
    for h in new[-4:]:
        h["cost"] = None
    collector.hourly, collector.daily = new, []
    # The rebuild runs with the imported count unknown (as after a restart without it).
    coordinator = config_entry.runtime_data
    coordinator._known_hours = None  # noqa: SLF001
    await coordinator._store.async_remove()  # noqa: SLF001
    await _poll(hass, config_entry)
    cost = (await _stats(hass))[COST_ID]
    assert [r["state"] for r in cost[-4:]] == pytest.approx([KWH * RATE] * 4)  # not zeroed
    assert cost[-1]["sum"] == pytest.approx(before[-1]["sum"])

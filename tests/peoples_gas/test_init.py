"""Setup, statistics import, sensors and the bill PDF option, through Home Assistant."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.peoples_gas.const import CONF_SAVE_BILL_PDFS, DOMAIN
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from conftest import ACCOUNT, FakePortal

CONSUMPTION_ID = f"{DOMAIN}:{ACCOUNT}_gas_consumption"
COST_ID = f"{DOMAIN}:{ACCOUNT}_gas_cost"
TZ = ZoneInfo("America/New_York")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run the next scheduled poll now (the schedule is jittered, so firing time is flaky)."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _stats(hass: HomeAssistant) -> dict:
    return await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=TZ),
        None,
        {CONSUMPTION_ID, COST_ID},
        "hour",
        None,
        {"state", "sum"},
    )


def _rows(stats: dict, statistic_id: str) -> list[tuple[str, float, float]]:
    return [
        (datetime.fromtimestamp(r["start"], TZ).date().isoformat(), round(r["state"], 2), round(r["sum"], 2))
        for r in stats.get(statistic_id, [])
    ]


async def test_backfill_then_incremental(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    stats = await _stats(hass)
    # Mcf -> CCF (x10), one row per billing cycle at local midnight of its read date.
    assert _rows(stats, CONSUMPTION_ID) == [
        ("2025-11-01", 40.0, 40.0),
        ("2025-12-01", 70.0, 110.0),
        ("2026-01-01", 90.0, 200.0),
        ("2026-02-01", 80.0, 280.0),
        ("2026-03-01", 60.0, 340.0),
    ]
    assert _rows(stats, COST_ID)[-1] == ("2026-03-01", 70.25, 390.9)

    meta = await hass.async_add_executor_job(get_metadata, hass)
    assert meta[CONSUMPTION_ID][1]["unit_of_measurement"] == "CCF"
    assert meta[CONSUMPTION_ID][1]["source"] == DOMAIN

    # A new cycle appears; the next poll adds only that row and continues the sums.
    portal.add_cycle(4, 2026, 3.5, "2026-04-20", 45.0, "800000000004")
    portal.current[0] = (1, 2026, 50.0)  # an already-imported cycle changing is ignored
    await _poll(hass, config_entry)

    stats = await _stats(hass)
    rows = _rows(stats, CONSUMPTION_ID)
    assert len(rows) == 6
    assert rows[-1] == ("2026-04-01", 35.0, 375.0)
    assert rows[2] == ("2026-01-01", 90.0, 200.0)
    assert _rows(stats, COST_ID)[-1] == ("2026-04-01", 45.0, 435.9)


async def test_sensors_and_device(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    await _setup(hass, config_entry)

    device = device_registry.async_get_device(identifiers={(DOMAIN, ACCOUNT)})
    assert device is not None
    assert device.manufacturer == "Peoples Natural Gas"
    entities = er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
    assert {e.device_id for e in entities} == {device.id}
    ids = {e.unique_id.removeprefix(f"{ACCOUNT}_"): e.entity_id for e in entities}
    assert set(ids) == {
        "price_to_compare",
        "commodity_rate",
        "last_bill_amount",
        "last_bill_date",
        "current_balance",
    }

    ptc = hass.states.get(ids["price_to_compare"])
    assert float(ptc.state) == pytest.approx(0.357)
    assert ptc.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD/CCF"
    assert ptc.attributes["per_mcf"] == 3.57
    assert ptc.attributes["invoice"] == "800000000003"

    commodity = hass.states.get(ids["commodity_rate"])
    assert float(commodity.state) == pytest.approx(0.36389)
    assert commodity.attributes["per_mcf"] == 3.6389

    amount = hass.states.get(ids["last_bill_amount"])
    assert float(amount.state) == 70.25
    assert amount.attributes[ATTR_DEVICE_CLASS] == "monetary"
    assert amount.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD"
    assert amount.attributes["mcf_used"] == 1.8

    bill_date = hass.states.get(ids["last_bill_date"])
    assert bill_date.state == "2026-03-18"
    assert bill_date.attributes[ATTR_DEVICE_CLASS] == "date"

    balance = hass.states.get(ids["current_balance"])
    assert float(balance.state) == 193.76
    assert balance.attributes["due_date"] == "2026-04-12"

    # Only the newest bill's PDF is read (for the rates); nothing is saved by default.
    assert portal.pdf_downloads == ["800000000003"]


async def test_newest_bill_pdf_not_refetched(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry)
    await _poll(hass, config_entry)
    assert portal.logins == 2
    assert portal.pdf_downloads == ["800000000003"]


async def test_save_bill_pdfs_option(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    tmp_path: Path,
) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        data={"username": "user@example.com", "password": "secret"},
        options={CONF_SAVE_BILL_PDFS: True},
    )
    await _setup(hass, entry)

    folder = tmp_path / "peoples_gas" / ACCOUNT
    saved = sorted(p.name for p in folder.iterdir())
    assert saved == [f"{d}_{inv}.pdf" for d, _, inv in sorted(portal.bills)]
    assert (folder / "2026-03-18_800000000003.pdf").read_bytes().startswith(b"%PDF")
    # Each bill downloaded once; the newest bill's rates are read from the saved copy.
    assert sorted(portal.pdf_downloads) == sorted(inv for _, _, inv in portal.bills)


async def test_auth_failure_starts_reauth(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    config_entry: MockConfigEntry,
) -> None:
    portal.password = "changed-on-the-portal"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_connection_failure_retries(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    config_entry: MockConfigEntry,
) -> None:
    import requests

    portal.login_error = requests.ConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED

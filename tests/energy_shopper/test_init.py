"""Setup, ranking against the usage statistic, repair issues and the change event."""

from __future__ import annotations

import pytest

from custom_components.energy_shopper.const import (
    CONF_NO_CANCELLATION_FEE,
    CONF_PRIOR_SUPPLIERS,
    CONF_RATE_ENTITY,
    CONF_RATE_UNIT,
    CONF_RATE_VALUE,
    CONF_THRESHOLD,
    CONF_USAGE_STATISTIC,
    DOMAIN,
    EVENT_RECOMMENDATION_CHANGED,
)
from custom_components.energy_shopper.coordinator import monthly_usage_cycles
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from conftest import ANNUAL_MCF, CONNECTION_ERROR, PTC, USAGE_ID, FakeMarket, add_usage

P = "sensor.gas_offers_15222_"


def state(hass: HomeAssistant, key: str):
    return hass.states.get(P + key)


def issues(hass: HomeAssistant, prefix: str) -> list[str]:
    return sorted(i for d, i in ir.async_get(hass).issues if d == DOMAIN and i.startswith(prefix))


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _set_options(hass: HomeAssistant, entry: MockConfigEntry, **changes) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, **changes})
    await hass.async_block_till_done()  # the update listener reloads the entry


async def test_switch_recommendation(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    rec = state(hass, "gas_recommendation")
    assert rec.state == "switch"
    assert rec.attributes["recommended"]["supplier"] == "Welcome Gas"
    assert rec.attributes["usage_months"] == 24  # the current partial month is skipped
    assert rec.attributes["baseline_source"] == "Price to Compare"

    assert state(hass, "best_gas_offer").state == "Welcome Gas"
    price = state(hass, "best_gas_offer_price")
    assert float(price.state) == pytest.approx(2.80)
    assert price.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD/mcf"
    assert float(state(hass, "gas_price_to_compare").state) == pytest.approx(PTC)
    assert float(state(hass, "current_gas_supply_rate").state) == pytest.approx(PTC)
    assert float(state(hass, "projected_annual_gas_use").state) == pytest.approx(ANNUAL_MCF, rel=1e-3)
    assert float(state(hass, "gas_annual_savings").state) == pytest.approx(ANNUAL_MCF * (PTC - 2.80), rel=1e-3)
    assert float(state(hass, "gas_savings").state) == pytest.approx(100 * (PTC - 2.80) / PTC, rel=1e-3)
    offers = state(hass, "gas_offers_ranked")
    assert offers.state == "4"
    assert [r["supplier"] for r in offers.attributes["leaderboard"]][:2] == ["Welcome Gas", "Fee Gas"]
    assert offers.attributes["best_by_category"]["Best open to existing customers"]["supplier"] == "Fee Gas"

    issue_id = f"cheaper_gas_supplier_{config_entry.entry_id}_welcome_gas"
    assert issues(hass, "cheaper_gas_supplier") == [issue_id]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.translation_placeholders["phone"] == "800-555-0199"
    assert issue.translation_placeholders["price"] == "2.8000"

    assert len(events) == 1
    assert events[0].data["recommendation"] == "switch"
    assert events[0].data["supplier"] == "Welcome Gas"
    assert events[0].data["previous"] is None

    # A second poll with nothing changed fires nothing new.
    await _poll(hass, config_entry)
    assert len(events) == 1


async def test_prior_supplier_and_filter_change_the_pick(
    hass: HomeAssistant, market: FakeMarket, config_entry
) -> None:
    await add_usage(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    await _setup(hass, config_entry)

    # You've used Welcome Gas, so its new-customer offer is out; Fee Gas is next.
    await _set_options(hass, config_entry, **{CONF_PRIOR_SUPPLIERS: "Welcome Gas"})
    assert state(hass, "best_gas_offer").state == "Fee Gas"
    assert issues(hass, "cheaper_gas_supplier") == [f"cheaper_gas_supplier_{config_entry.entry_id}_fee_gas"]
    assert [e.data["supplier"] for e in events] == ["Welcome Gas", "Fee Gas"]

    # No cancellation fees: Steady Gas.
    await _set_options(hass, config_entry, **{CONF_NO_CANCELLATION_FEE: True})
    assert state(hass, "best_gas_offer").state == "Steady Gas"
    notes = state(hass, "gas_recommendation").attributes["notes"]
    assert any("Skipped 1 Welcome Gas offer(s)" in n for n in notes)


async def test_stay_clears_issue_and_fires(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    await _setup(hass, config_entry)
    assert issues(hass, "cheaper_gas_supplier")

    await _set_options(hass, config_entry, **{CONF_THRESHOLD: 50.0})
    assert state(hass, "gas_recommendation").state == "stay"
    assert issues(hass, "cheaper_gas_supplier") == []
    assert [e.data["recommendation"] for e in events] == ["switch", "stay"]
    assert events[-1].data["previous"] == "switch"


async def test_first_run_stay_fires_nothing(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    hass_entry = MockConfigEntry(
        domain=DOMAIN,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=config_entry.data,
        options={**config_entry.options, CONF_THRESHOLD: 50.0},
    )
    await _setup(hass, hass_entry)
    assert state(hass, "gas_recommendation").state == "stay"
    assert events == []


async def test_rate_sensor_in_ccf_and_rate_jump(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    hass.states.async_set("sensor.peoples_ptc", "0.300", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=config_entry.data,
        options={**config_entry.options, CONF_RATE_ENTITY: "sensor.peoples_ptc"},
    )
    await _setup(hass, entry)
    current = state(hass, "current_gas_supply_rate")
    assert float(current.state) == pytest.approx(3.00)  # $/ccf × 10
    assert current.attributes["source"] == "sensor"
    # Welcome Gas at $2.80 vs $3.00 is 6.7% — still a switch at 5%.
    assert state(hass, "gas_recommendation").state == "switch"
    assert issues(hass, "gas_rate_jump") == []

    hass.states.async_set("sensor.peoples_ptc", "0.360", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    await _poll(hass, entry)
    jumps = issues(hass, "gas_rate_jump")
    assert len(jumps) == 1
    placeholders = ir.async_get(hass).async_get_issue(DOMAIN, jumps[0]).translation_placeholders
    assert placeholders["pct"] == "20"
    assert placeholders["old"] == "3.0000" and placeholders["new"] == "3.6000"

    # A small move doesn't add another.
    hass.states.async_set("sensor.peoples_ptc", "0.365", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    await _poll(hass, entry)
    assert issues(hass, "gas_rate_jump") == jumps


async def test_typed_rate_in_therms(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    await _setup(hass, config_entry)
    await _set_options(hass, config_entry, **{CONF_RATE_VALUE: 0.30, CONF_RATE_UNIT: "therm"})
    current = state(hass, "current_gas_supply_rate")
    assert float(current.state) == pytest.approx(0.30 * 10.37)
    assert current.attributes["source"] == "configured value"


async def test_unavailable_rate_sensor_falls_back(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    hass.states.async_set("sensor.peoples_ptc", "unavailable")
    await _setup(hass, config_entry)
    await _set_options(hass, config_entry, **{CONF_RATE_ENTITY: "sensor.peoples_ptc"})
    rec = state(hass, "gas_recommendation")
    assert rec.attributes["baseline_source"] == "Price to Compare"
    assert any("sensor.peoples_ptc has no value" in n for n in rec.attributes["notes"])


async def test_no_usage_ranks_by_price(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await _setup(hass, config_entry)  # the statistic doesn't exist
    rec = state(hass, "gas_recommendation")
    assert any("not found" in n for n in rec.attributes["notes"])
    assert any("Ranked by listed $/mcf" in n for n in rec.attributes["notes"])
    assert state(hass, "best_gas_offer").state == "Welcome Gas"
    assert state(hass, "gas_annual_savings").state == "unknown"
    assert issues(hass, "cheaper_gas_supplier") == []


async def test_market_down_marks_unavailable(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    await _setup(hass, config_entry)
    market.error = CONNECTION_ERROR
    await _poll(hass, config_entry)
    assert state(hass, "gas_recommendation").state == "unavailable"


async def test_setup_retries_when_market_down(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    market.error = CONNECTION_ERROR
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_remove_entry_clears_issues(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    await _setup(hass, config_entry)
    assert issues(hass, "cheaper_gas_supplier")
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issues(hass, "cheaper_gas_supplier") == []


def test_monthly_cycles_energy_and_partial_month() -> None:
    now = dt_util.now()
    this_month = dt_util.as_utc(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    rows = [
        {"start": dt_util.as_utc(this_month.replace(year=this_month.year - 1)).timestamp(), "change": 1033.0 * 29.3071 / 10},
        {"start": this_month.timestamp(), "change": 500.0},
        {"start": 0.0, "change": None},
    ]
    cycles = monthly_usage_cycles(rows, "energy", 10.33, now)
    assert len(cycles) == 1
    assert cycles[0].usage_mcf == pytest.approx(10.0)

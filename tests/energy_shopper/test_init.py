"""Setup, ranking against the usage statistic, repair issues and the change event."""

from __future__ import annotations

from datetime import timedelta

from unittest.mock import patch

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
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
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
    assert rec.attributes["usage_months"] == 25  # a billing-cycle source keeps this month's cycle
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
    assert rec.state == "unknown"  # a configured input is missing: hold, don't compare against a fallback
    assert rec.attributes["baseline_source"] == "Price to Compare"
    assert any("sensor.peoples_ptc has no value" in n for n in rec.attributes["notes"])


async def test_no_usage_ranks_by_price(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await _setup(hass, config_entry)  # the statistic doesn't exist
    rec = state(hass, "gas_recommendation")
    assert rec.state == "unknown"  # nothing to compare: no usage, so no stay row
    assert rec.attributes["lock_in_note"] is None
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


def _day_rows(starts_and_changes):
    return [{"start": dt_util.as_utc(t).timestamp(), "change": c} for t, c in starts_and_changes]


def test_monthly_cycles_daily_source_drops_partial_month() -> None:
    now = dt_util.now()
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = (first - timedelta(days=1)).replace(day=1)
    rows = _day_rows(
        [(last_month + timedelta(days=d), 1033.0 * 29.3071 / 10 / 28) for d in range(28)]  # kWh
        + [(first, 500.0), (first + timedelta(hours=30), 500.0)]
    )
    cycles = monthly_usage_cycles(rows, "energy", 10.33, now)
    assert [(c.month, c.year) for c in cycles] == [(last_month.month, last_month.year)]
    assert cycles[0].usage_mcf == pytest.approx(10.0)


def test_monthly_cycles_billing_source_keeps_this_month() -> None:
    now = dt_util.now()
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    starts = [first]
    for _ in range(5):
        starts.insert(0, (starts[0] - timedelta(days=1)).replace(day=1))
    cycles = monthly_usage_cycles(_day_rows([(t, 4000.0) for t in starts] + [(first, None)]), "volume", 10.37, now)
    assert len(cycles) == 6
    assert (cycles[-1].month, cycles[-1].year) == (now.month, now.year)
    assert cycles[-1].usage_mcf == pytest.approx(4.0)


async def test_no_baseline_is_unknown(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    market.price_to_compare = None
    entry = MockConfigEntry(
        domain=DOMAIN, title=config_entry.title, unique_id=config_entry.unique_id,
        data={**config_entry.data, "price_unit": "mcf"}, options=config_entry.options,
    )
    await _setup(hass, entry)
    rec = state(hass, "gas_recommendation")
    assert rec.state == "unknown"
    assert any("No current rate or Price to Compare" in n for n in rec.attributes["notes"])
    assert issues(hass, "cheaper_gas_supplier") == []


async def test_unknown_refresh_does_not_repeat_switch_event(
    hass: HomeAssistant, market: FakeMarket, config_entry
) -> None:
    await add_usage(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    await _setup(hass, config_entry)
    assert [e.data["supplier"] for e in events] == ["Welcome Gas"]
    offers = market.offers
    market.offers = []  # one refresh with nothing listed
    await _poll(hass, config_entry)
    assert state(hass, "gas_recommendation").state == "unknown"
    market.offers = offers
    await _poll(hass, config_entry)
    assert state(hass, "gas_recommendation").state == "switch"
    assert [e.data["supplier"] for e in events] == ["Welcome Gas"]  # no repeat


async def test_empty_listing_keeps_dismissed_issue(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    await _setup(hass, config_entry)
    (issue_id,) = issues(hass, "cheaper_gas_supplier")
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    offers = market.offers
    market.offers = []
    await _poll(hass, config_entry)
    assert issues(hass, "cheaper_gas_supplier") == [issue_id]  # not deleted by a glitch
    market.offers = offers
    await _poll(hass, config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).dismissed_version is not None


async def test_unavailable_rate_sensor_holds_recommendation(
    hass: HomeAssistant, market: FakeMarket, config_entry
) -> None:
    await add_usage(hass)
    hass.states.async_set("sensor.peoples_ptc", "0.300", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    entry = MockConfigEntry(
        domain=DOMAIN, title=config_entry.title, unique_id=config_entry.unique_id,
        data=config_entry.data, options={**config_entry.options, CONF_RATE_ENTITY: "sensor.peoples_ptc"},
    )
    await _setup(hass, entry)
    assert state(hass, "gas_recommendation").state == "switch"
    hass.states.async_set("sensor.peoples_ptc", "unavailable")
    await _poll(hass, entry)
    assert state(hass, "gas_recommendation").state == "unknown"
    assert issues(hass, "cheaper_gas_supplier")  # kept
    hass.states.async_set("sensor.peoples_ptc", "0.300", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    await _poll(hass, entry)
    assert state(hass, "gas_recommendation").state == "switch"
    assert len(events) == 1  # no flip-flop events


def test_monthly_cycles_daily_source_drops_partial_first_month() -> None:
    now = dt_util.now()
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = [first]
    for _ in range(3):
        months.insert(0, (months[0] - timedelta(days=1)).replace(day=1))
    rows = []
    for i, m in enumerate(months[:-1]):
        start_day = 15 if i == 0 else 0  # history begins mid-month
        rows += [(m + timedelta(days=d), 100.0) for d in range(start_day, 28)]
    cycles = monthly_usage_cycles(_day_rows(rows), "volume", 10.37, now)
    assert [(c.year, c.month) for c in cycles] == [(m.year, m.month) for m in months[1:-1]]


def _with_rate_entity(config_entry: MockConfigEntry, entity_id: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title=config_entry.title, unique_id=config_entry.unique_id,
        data=config_entry.data, options={**config_entry.options, CONF_RATE_ENTITY: entity_id},
    )


async def test_refreshes_when_held_input_gets_a_value(
    hass: HomeAssistant, market: FakeMarket, config_entry
) -> None:
    """At startup the rate sensor's integration may load after this one: don't wait 12 h."""
    await add_usage(hass)
    entry = _with_rate_entity(config_entry, "sensor.peoples_ptc")
    await _setup(hass, entry)  # the sensor doesn't exist yet
    assert state(hass, "gas_recommendation").state == "unknown"

    hass.states.async_set("sensor.peoples_ptc", "unavailable")  # still nothing to read
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "gas_recommendation").state == "unknown"

    hass.states.async_set("sensor.peoples_ptc", "0.300", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "gas_recommendation").state == "switch"
    assert state(hass, "current_gas_supply_rate").attributes["source"] == "sensor"


async def test_input_changes_do_not_refresh_when_nothing_was_held(
    hass: HomeAssistant, market: FakeMarket, config_entry
) -> None:
    """Only a held refresh is redone early; a rate that just changes waits for the next poll."""
    await add_usage(hass)
    hass.states.async_set("sensor.peoples_ptc", "0.300", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    entry = _with_rate_entity(config_entry, "sensor.peoples_ptc")
    await _setup(hass, entry)
    checked = state(hass, "gas_recommendation").attributes["last_checked"]
    hass.states.async_set("sensor.peoples_ptc", "0.310", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "gas_recommendation").attributes["last_checked"] == checked


async def test_deleted_input_sensor_raises_issue(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    entry = _with_rate_entity(config_entry, "sensor.renamed_away")
    issue_id = f"input_sensor_missing_{entry.entry_id}"

    hass.set_state(CoreState.starting)  # during startup a missing sensor may just not be loaded yet
    await _setup(hass, entry)
    assert issues(hass, "input_sensor_missing") == []

    # Once startup finishes the sensor is judged gone, without waiting for the next poll.
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert issues(hass, "input_sensor_missing") == [issue_id]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.translation_placeholders["entities"] == "sensor.renamed_away"
    assert state(hass, "gas_recommendation").state == "unknown"

    # An unavailable (but existing) sensor is not "gone".
    hass.states.async_set("sensor.renamed_away", "unavailable")
    await _poll(hass, entry)
    assert issues(hass, "input_sensor_missing") == []

    # Clearing the field in the options also clears the issue.
    hass.states.async_remove("sensor.renamed_away")
    await _poll(hass, entry)
    assert issues(hass, "input_sensor_missing") == [issue_id]
    hass.config_entries.async_update_entry(
        entry, options={k: v for k, v in entry.options.items() if k != CONF_RATE_ENTITY}
    )
    await hass.async_block_till_done()
    assert issues(hass, "input_sensor_missing") == []


async def test_input_value_during_platform_setup_is_not_missed(
    hass: HomeAssistant, market: FakeMarket, config_entry
) -> None:
    """The sensor gets its value after the first refresh but before the watcher subscribes."""
    await add_usage(hass)
    entry = _with_rate_entity(config_entry, "sensor.peoples_ptc")
    forward = hass.config_entries.async_forward_entry_setups

    async def forward_while_sensor_loads(*args, **kwargs):
        hass.states.async_set("sensor.peoples_ptc", "0.300", {ATTR_UNIT_OF_MEASUREMENT: "USD/CCF"})
        return await forward(*args, **kwargs)

    with patch.object(hass.config_entries, "async_forward_entry_setups", forward_while_sensor_loads):
        await _setup(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "gas_recommendation").state == "switch"

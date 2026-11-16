"""Electric: config flow, ranking against hourly usage and bill sensors, Time-of-Use."""

from __future__ import annotations

import pytest

from custom_components.energy_shopper.const import (
    CONF_FUEL,
    CONF_RATE_TYPE,
    CONF_RENEWABLE_ONLY,
    CONF_THRESHOLD,
    CONF_TOU_OFF_PEAK,
    CONF_TOU_PEAK,
    CONF_TOU_SUPER_OFF_PEAK,
    CONF_USAGE_STATISTIC,
    DOMAIN,
    EVENT_RECOMMENDATION_CHANGED,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from conftest import (
    CURRENT_RATE,
    ELECTRIC_PTC,
    ELECTRIC_USAGE_ID,
    HOURLY_DAYS,
    SHOP_URL,
    FakePowerSwitch,
    add_hourly_usage,
    set_bill_sensors,
)

P = "sensor.electric_offers_15222_"
TOU = {CONF_TOU_PEAK: 20.0, CONF_TOU_OFF_PEAK: 12.0, CONF_TOU_SUPER_OFF_PEAK: 6.0}


def state(hass: HomeAssistant, key: str):
    return hass.states.get(P + key)


def issues(hass: HomeAssistant) -> list:
    reg = ir.async_get(hass)
    return sorted((i, reg.async_get_issue(d, i).translation_key) for d, i in reg.issues if d == DOMAIN)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _set_options(hass: HomeAssistant, entry: MockConfigEntry, **changes) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, **changes})
    await hass.async_block_till_done()


async def test_flow(hass: HomeAssistant, power_switch: FakePowerSwitch) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "electric"})
    assert result["step_id"] == "electric"
    flow = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow, {"shop_url": "https://www.papowerswitch.com/"})
    assert result["errors"] == {"shop_url": "invalid_shop_url"}
    result = await hass.config_entries.flow.async_configure(flow, {"shop_url": SHOP_URL})
    assert result["step_id"] == "electric_settings"
    with pytest.MonkeyPatch.context() as mp:  # don't set the entry up: nothing to rank yet
        mp.setattr("custom_components.energy_shopper.async_setup_entry", lambda *a: _true())
        result = await hass.config_entries.flow.async_configure(
            flow, {CONF_USAGE_STATISTIC: ELECTRIC_USAGE_ID, CONF_THRESHOLD: 5.0, "no_cancellation_fee": False, "renewable_only": False}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Electric offers 15222"
    assert result["data"] == {
        CONF_FUEL: "electric",
        "distributor": "1180",
        CONF_RATE_TYPE: "RS - Regular Residential Service",
        "zipcode": "15222",
        "service_type": "residential",
    }
    assert result["result"].unique_id == "electric_1180_rs_regular_residential_service_15222"


async def _true() -> bool:
    return True


async def test_flow_no_offers(hass: HomeAssistant, power_switch: FakePowerSwitch) -> None:
    power_switch.offers = []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "electric"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"shop_url": SHOP_URL})
    assert result["errors"] == {"base": "no_offers"}


async def test_switch_from_your_supplier(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    await _setup(hass, electric_entry)

    data = electric_entry.runtime_data.data
    d = data.decision
    assert d.stay.supplier == "Our Supplier LLC" and d.stay.unit_price == pytest.approx(CURRENT_RATE)
    # Your own supplier's new-customer deal is out; Duquesne default is listed as an option.
    assert "Our Supplier LLC" not in [o.supplier for o in d.ranked if not o.is_current_egs]
    assert any(o.is_utility_default and o.unit_price == pytest.approx(ELECTRIC_PTC) for o in d.ranked)
    assert d.recommended.supplier == "Bright Power"

    assert state(hass, "electric_recommendation").state == "switch"
    assert state(hass, "best_electric_offer").state == "Bright Power"
    price = state(hass, "best_electric_offer_price")
    assert float(price.state) == pytest.approx(0.10)
    assert price.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD/kWh"
    current = state(hass, "current_electric_supply_rate")
    assert float(current.state) == pytest.approx(CURRENT_RATE)
    assert current.attributes["source"] == "sensor" and current.attributes["supplier"] == "Our Supplier LLC"
    assert float(state(hass, "electric_price_to_compare").state) == pytest.approx(ELECTRIC_PTC)
    usage = state(hass, "projected_annual_electricity_use")
    assert usage.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kWh"
    annual = float(usage.state)
    assert float(state(hass, "electric_annual_savings").state) == pytest.approx(annual * (CURRENT_RATE - 0.10), rel=1e-3)
    assert state(hass, "electric_offers_ranked").state == "3"
    assert state(hass, "time_of_use_annual_savings").state == "unknown"  # no TOU rates set

    assert issues(hass) == [(f"cheaper_electric_supplier_{electric_entry.entry_id}_bright_power", "cheaper_electric_supplier")]
    assert [(e.data["fuel"], e.data["supplier"], e.data["unit"]) for e in events] == [("electric", "Bright Power", "kWh")]


async def test_default_service(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass, supplier=None, rate=None)
    await _setup(hass, electric_entry)
    d = electric_entry.runtime_data.data.decision
    assert d.stay.is_utility_default and d.stay.unit_price == pytest.approx(ELECTRIC_PTC)
    current = state(hass, "current_electric_supply_rate")
    assert float(current.state) == pytest.approx(ELECTRIC_PTC)
    assert current.attributes["source"] == "Price to Compare"
    # Never used Our Supplier LLC, so its new-customer offer counts.
    assert state(hass, "best_electric_offer").state == "Our Supplier LLC"


async def test_renewable_only(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    await _setup(hass, electric_entry)
    await _set_options(hass, electric_entry, **{CONF_RENEWABLE_ONLY: True})
    assert state(hass, "best_electric_offer").state == "Green Volts"


async def test_time_of_use_wins(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    power_switch.offers = power_switch.offers[3:]  # only the expensive variable plan
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    entry = MockConfigEntry(
        domain=DOMAIN, title=electric_entry.title, unique_id=electric_entry.unique_id,
        data=electric_entry.data, options={**electric_entry.options, **TOU},
    )
    await _setup(hass, entry)
    data = entry.runtime_data.data
    # Midnight-only backfill days are left out of the Time-of-Use mix.
    assert data.tou.days_analyzed in (HOURLY_DAYS, HOURLY_DAYS + 1)
    assert data.tou.super_off_peak_pct > 50
    assert data.decision.recommended.is_tou and data.decision.tou_recommended is True

    assert state(hass, "electric_recommendation").state == "switch"
    assert state(hass, "electric_recommendation").attributes["time_of_use_recommended"] is True
    tou = state(hass, "time_of_use_annual_savings")
    assert float(tou.state) > 0
    assert tou.attributes["rates"]["peak"] == pytest.approx(0.20)
    assert set(tou.attributes["load_shifted"]) == {"TOU, shift 25% of peak", "TOU, shift 50% of peak"}
    assert [k for _, k in issues(hass)] == ["tou_available"]
    assert events[-1].data["time_of_use"] is True


async def test_time_of_use_not_worth_it(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title=electric_entry.title, unique_id=electric_entry.unique_id,
        data=electric_entry.data,
        options={**electric_entry.options, CONF_TOU_PEAK: 40.0, CONF_TOU_OFF_PEAK: 25.0, CONF_TOU_SUPER_OFF_PEAK: 20.0},
    )
    await _setup(hass, entry)
    assert entry.runtime_data.data.decision.tou_recommended is False
    assert float(state(hass, "time_of_use_annual_savings").state) < 0
    assert state(hass, "best_electric_offer").state == "Bright Power"


async def test_wrong_statistic_unit(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass, unit="ft³")
    set_bill_sensors(hass)
    await _setup(hass, electric_entry)
    notes = state(hass, "electric_recommendation").attributes["notes"]
    assert any("not an energy unit" in n for n in notes)
    assert any("Ranked by listed $/kWh" in n for n in notes)


async def test_options_flow_electric(hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    await _setup(hass, electric_entry)
    result = await hass.config_entries.options.async_init(electric_entry.entry_id)
    assert result["step_id"] == "electric"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**electric_entry.options, CONF_THRESHOLD: 60.0, "rate_jump_pct": 10.0, "renewable_only": False, "no_cancellation_fee": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert state(hass, "electric_recommendation").state == "stay"


async def test_unavailable_supplier_sensor_is_not_default_service(
    hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry
) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    events = async_capture_events(hass, EVENT_RECOMMENDATION_CHANGED)
    await _setup(hass, electric_entry)
    assert state(hass, "electric_recommendation").state == "switch"
    hass.states.async_set("sensor.dl_supplier", "unavailable")  # the collector entry is down
    await electric_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    rec = state(hass, "electric_recommendation")
    assert rec.state == "unknown"
    assert any("sensor.dl_supplier" in n and "no recommendation" in n for n in rec.attributes["notes"])
    assert len(issues(hass)) == 1  # the cheaper-supplier issue is kept
    set_bill_sensors(hass)
    await electric_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert state(hass, "electric_recommendation").state == "switch"
    assert len(events) == 1


async def test_empty_listing_raises_no_tou_issue(
    hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry
) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass)
    power_switch.offers = []
    entry = MockConfigEntry(
        domain=DOMAIN, title=electric_entry.title, unique_id=electric_entry.unique_id,
        data=electric_entry.data, options={**electric_entry.options, **TOU},
    )
    await _setup(hass, entry)
    assert state(hass, "electric_recommendation").state == "unknown"
    assert issues(hass) == []


async def test_switching_supplier_is_not_a_rate_jump(
    hass: HomeAssistant, power_switch: FakePowerSwitch, electric_entry
) -> None:
    await add_hourly_usage(hass)
    set_bill_sensors(hass, supplier="Old Supplier", rate=0.10)
    await _setup(hass, electric_entry)
    set_bill_sensors(hass, supplier="Pricier Green LLC", rate=0.13)  # your own switch: +30%
    await electric_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert not [k for _, k in issues(hass) if k == "electric_rate_jump"]

    set_bill_sensors(hass, supplier="Pricier Green LLC", rate=0.16)  # same supplier, +23%
    await electric_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert [k for _, k in issues(hass) if k == "electric_rate_jump"] == ["electric_rate_jump"]

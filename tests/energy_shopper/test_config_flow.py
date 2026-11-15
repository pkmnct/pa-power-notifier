"""Config and options flow."""

from __future__ import annotations

from custom_components.energy_shopper.const import (
    CONF_DISTRIBUTOR,
    CONF_NO_CANCELLATION_FEE,
    CONF_PRICE_UNIT,
    CONF_RATE_JUMP_PCT,
    CONF_SERVICE_TYPE,
    CONF_THERMS_PER_MCF,
    CONF_THRESHOLD,
    CONF_USAGE_STATISTIC,
    CONF_ZIPCODE,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from conftest import CONNECTION_ERROR, DISTRIBUTOR, USAGE_ID, ZIP, FakeMarket, add_usage

MARKET = {CONF_ZIPCODE: f" {ZIP} ", CONF_DISTRIBUTOR: DISTRIBUTOR, CONF_SERVICE_TYPE: "residential"}
SETTINGS = {CONF_USAGE_STATISTIC: USAGE_ID, CONF_THRESHOLD: 5.0, CONF_NO_CANCELLATION_FEE: False}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_full_flow(hass: HomeAssistant, market: FakeMarket) -> None:
    await add_usage(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MARKET)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "settings"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETTINGS, "prior_suppliers": ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Gas offers {ZIP}"
    assert result["data"] == {CONF_ZIPCODE: ZIP, CONF_DISTRIBUTOR: DISTRIBUTOR, CONF_SERVICE_TYPE: "residential"}
    assert result["options"] == SETTINGS  # the blank prior_suppliers is dropped
    assert result["result"].unique_id == f"gas_{DISTRIBUTOR}_{ZIP}_residential"


async def test_cannot_connect(hass: HomeAssistant, market: FakeMarket) -> None:
    market.error = CONNECTION_ERROR
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], MARKET)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_no_offers(hass: HomeAssistant, market: FakeMarket) -> None:
    market.offers = []
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], MARKET)
    assert result["errors"] == {"base": "no_offers"}


async def test_unknown_unit_then_fixed(hass: HomeAssistant, market: FakeMarket) -> None:
    market.price_to_compare = None
    flow = (await _start(hass))["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow, MARKET)
    assert result["errors"] == {CONF_PRICE_UNIT: "unknown_unit"}
    result = await hass.config_entries.flow.async_configure(flow, {**MARKET, CONF_PRICE_UNIT: "mcf"})
    assert result["step_id"] == "settings"


async def test_already_configured(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], MARKET)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, market: FakeMarket, config_entry) -> None:
    await add_usage(hass)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    new = {**SETTINGS, CONF_THRESHOLD: 30.0, CONF_RATE_JUMP_PCT: 15.0, CONF_THERMS_PER_MCF: 10.4}
    result = await hass.config_entries.options.async_configure(result["flow_id"], new)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options == new
    assert hass.states.get("sensor.gas_offers_15222_gas_recommendation").state == "stay"

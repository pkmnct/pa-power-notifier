"""
Config flow for Energy Shopper.

The first step picks the fuel. Gas: ZIP and PA Gas Switch distributor id, checked by
fetching its offers. Electric: the PA Power Switch results URL (it carries distributor,
rate class and ZIP), checked the same way. The settings step then picks your usage
statistic, where your current rate comes from, and how picky to be; the options flow
edits the same settings later.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_DISTRIBUTOR,
    CONF_FUEL,
    CONF_NO_CANCELLATION_FEE,
    CONF_PRICE_UNIT,
    CONF_PRIOR_SUPPLIERS,
    CONF_PTC_ENTITY,
    CONF_PTC_VALUE,
    CONF_RATE_ENTITY,
    CONF_RATE_JUMP_PCT,
    CONF_RATE_TYPE,
    CONF_RATE_UNIT,
    CONF_RATE_VALUE,
    CONF_RENEWABLE_ONLY,
    CONF_SERVICE_TYPE,
    CONF_SHOP_URL,
    CONF_SUPPLIER_ENTITY,
    CONF_SUPPLIER_NAME,
    CONF_THERMS_PER_MCF,
    CONF_THRESHOLD,
    CONF_TOU_OFF_PEAK,
    CONF_TOU_PEAK,
    CONF_TOU_SUPER_OFF_PEAK,
    CONF_USAGE_STATISTIC,
    CONF_ZIPCODE,
    DEFAULT_RATE_JUMP_PCT,
    DEFAULT_SERVICE_TYPE,
    DEFAULT_THRESHOLD,
    DOMAIN,
    FUEL_ELECTRIC,
    FUEL_GAS,
    PRICE_UNITS,
)
from .coordinator import fetch_electric_market, fetch_market
from .vendor.energy_shopper_core.gas import DEFAULT_THERMS_PER_MCF
from .vendor.energy_shopper_core.markets.pa import parse_electric_shop_results_url

_LOGGER = logging.getLogger(__name__)

UNIT_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=PRICE_UNITS, mode=selector.SelectSelectorMode.DROPDOWN, translation_key="price_unit"
    )
)
SERVICE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(options=["residential", "commercial"], translation_key="service_type")
)


def _number(unit: str | None = None, minimum: float = 0, maximum: float | None = None, step: Any = "any"):
    config = selector.NumberSelectorConfig(min=minimum, step=step, mode=selector.NumberSelectorMode.BOX)
    if maximum is not None:
        config["max"] = maximum
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _sensor() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


GAS_MARKET_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZIPCODE): selector.TextSelector(),
        vol.Required(CONF_DISTRIBUTOR): selector.TextSelector(),
        vol.Required(CONF_SERVICE_TYPE, default=DEFAULT_SERVICE_TYPE): SERVICE_SELECTOR,
        vol.Optional(CONF_PRICE_UNIT): UNIT_SELECTOR,
    }
)

ELECTRIC_MARKET_SCHEMA = vol.Schema(
    {vol.Required(CONF_SHOP_URL): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL))}
)


def _common_fields(full: bool) -> dict[Any, Any]:
    fields: dict[Any, Any] = {
        vol.Required(CONF_THRESHOLD, default=DEFAULT_THRESHOLD): _number("%", 0, 100, 0.5),
        vol.Required(CONF_NO_CANCELLATION_FEE, default=False): selector.BooleanSelector(),
        vol.Optional(CONF_PRIOR_SUPPLIERS): selector.TextSelector(),
    }
    if full:
        fields[vol.Required(CONF_RATE_JUMP_PCT, default=DEFAULT_RATE_JUMP_PCT)] = _number("%", 1, 200, 1)
    return fields


def gas_settings_schema(full: bool) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(CONF_USAGE_STATISTIC): selector.StatisticSelector(),
        vol.Optional(CONF_RATE_ENTITY): _sensor(),
        vol.Optional(CONF_RATE_VALUE): _number(),
        vol.Optional(CONF_RATE_UNIT): UNIT_SELECTOR,
        **_common_fields(full),
    }
    if full:
        fields[vol.Required(CONF_THERMS_PER_MCF, default=DEFAULT_THERMS_PER_MCF)] = _number(None, 5, 15, 0.01)
    return vol.Schema(fields)


def electric_settings_schema(full: bool) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_USAGE_STATISTIC): selector.StatisticSelector(),
            vol.Optional(CONF_SUPPLIER_ENTITY): _sensor(),
            vol.Optional(CONF_SUPPLIER_NAME): selector.TextSelector(),
            vol.Optional(CONF_RATE_ENTITY): _sensor(),
            vol.Optional(CONF_RATE_VALUE): _number("USD/kWh"),
            vol.Optional(CONF_PTC_ENTITY): _sensor(),
            vol.Optional(CONF_PTC_VALUE): _number("USD/kWh"),
            vol.Required(CONF_RENEWABLE_ONLY, default=False): selector.BooleanSelector(),
            vol.Optional(CONF_TOU_PEAK): _number("¢/kWh"),
            vol.Optional(CONF_TOU_OFF_PEAK): _number("¢/kWh"),
            vol.Optional(CONF_TOU_SUPER_OFF_PEAK): _number("¢/kWh"),
            **_common_fields(full),
        }
    )


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop blank optional values so 'cleared' really means unset."""
    return {k: v for k, v in user_input.items() if v not in (None, "")}


class EnergyShopperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Energy Shopper for one gas or electric market."""

    VERSION = 1

    def __init__(self) -> None:
        self._market: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[FUEL_GAS, FUEL_ELECTRIC])

    async def _check(self, fetch, *args) -> tuple[Any, dict[str, str]]:
        try:
            found = await self.hass.async_add_executor_job(fetch, *args)
        except requests.RequestException as err:
            _LOGGER.debug("Market request failed: %s", err)
            return None, {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error fetching offers")
            return None, {"base": "unknown"}
        if not found.offers:
            return None, {"base": "no_offers"}
        return found, {}

    async def async_step_gas(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            market = _clean(user_input)
            market[CONF_ZIPCODE] = market[CONF_ZIPCODE].strip()
            market[CONF_DISTRIBUTOR] = market[CONF_DISTRIBUTOR].strip()
            await self.async_set_unique_id(
                f"gas_{market[CONF_DISTRIBUTOR]}_{market[CONF_ZIPCODE]}_{market[CONF_SERVICE_TYPE]}"
            )
            self._abort_if_unique_id_configured()
            found, errors = await self._check(
                fetch_market, market[CONF_DISTRIBUTOR], market[CONF_ZIPCODE], market[CONF_SERVICE_TYPE]
            )
            if found is not None and not found.price_to_compare and not market.get(CONF_PRICE_UNIT):
                errors = {CONF_PRICE_UNIT: "unknown_unit"}
            if not errors:
                self._market = market
                return await self.async_step_settings()
        return self.async_show_form(
            step_id="gas",
            data_schema=self.add_suggested_values_to_schema(GAS_MARKET_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_electric(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            params = parse_electric_shop_results_url(user_input[CONF_SHOP_URL])
            if not params.get("id") or not params.get("ratetype"):
                errors[CONF_SHOP_URL] = "invalid_shop_url"
            else:
                market = {
                    CONF_FUEL: FUEL_ELECTRIC,
                    CONF_DISTRIBUTOR: params["id"],
                    CONF_RATE_TYPE: params["ratetype"],
                    CONF_ZIPCODE: params.get("zipcode", ""),
                    CONF_SERVICE_TYPE: params.get("servicetype", DEFAULT_SERVICE_TYPE).lower(),
                }
                await self.async_set_unique_id(
                    f"electric_{market[CONF_DISTRIBUTOR]}_{slugify(market[CONF_RATE_TYPE])}_{market[CONF_ZIPCODE]}"
                )
                self._abort_if_unique_id_configured()
                _, errors = await self._check(
                    fetch_electric_market,
                    market[CONF_DISTRIBUTOR],
                    market[CONF_RATE_TYPE],
                    market[CONF_ZIPCODE],
                    market[CONF_SERVICE_TYPE],
                )
                if not errors:
                    self._market = market
                    return await self.async_step_electric_settings()
        return self.async_show_form(
            step_id="electric",
            data_schema=self.add_suggested_values_to_schema(ELECTRIC_MARKET_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"Gas offers {self._market[CONF_ZIPCODE]}",
                data=self._market,
                options=_clean(user_input),
            )
        return self.async_show_form(step_id="settings", data_schema=gas_settings_schema(full=False))

    async def async_step_electric_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            zipcode = self._market[CONF_ZIPCODE]
            return self.async_create_entry(
                title=f"Electric offers {zipcode}".strip(),
                data=self._market,
                options=_clean(user_input),
            )
        return self.async_show_form(step_id="electric_settings", data_schema=electric_settings_schema(full=False))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergyShopperOptionsFlow()


class EnergyShopperOptionsFlow(OptionsFlow):
    """Change usage source, rate sources, threshold and filters."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        electric = self.config_entry.data.get(CONF_FUEL, FUEL_GAS) == FUEL_ELECTRIC
        schema = electric_settings_schema(full=True) if electric else gas_settings_schema(full=True)
        return self.async_show_form(
            step_id="electric" if electric else "init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )

    async def async_step_electric(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_init(user_input)

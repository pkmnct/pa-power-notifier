"""
Config flow for Energy Shopper (gas).

Step 1 picks the market (ZIP and PA Gas Switch distributor id), checked by fetching its
offers. Step 2 picks your usage statistic, where your current rate comes from, and how
picky to be; the options flow edits the same settings later.
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

from .const import (
    CONF_DISTRIBUTOR,
    CONF_NO_CANCELLATION_FEE,
    CONF_PRICE_UNIT,
    CONF_PRIOR_SUPPLIERS,
    CONF_RATE_ENTITY,
    CONF_RATE_JUMP_PCT,
    CONF_RATE_UNIT,
    CONF_RATE_VALUE,
    CONF_SERVICE_TYPE,
    CONF_THERMS_PER_MCF,
    CONF_THRESHOLD,
    CONF_USAGE_STATISTIC,
    CONF_ZIPCODE,
    DEFAULT_RATE_JUMP_PCT,
    DEFAULT_SERVICE_TYPE,
    DEFAULT_THRESHOLD,
    DOMAIN,
    PRICE_UNITS,
)
from .coordinator import fetch_market
from .vendor.energy_shopper_core.gas import DEFAULT_THERMS_PER_MCF

_LOGGER = logging.getLogger(__name__)

UNIT_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=PRICE_UNITS, mode=selector.SelectSelectorMode.DROPDOWN, translation_key="price_unit"
    )
)

MARKET_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZIPCODE): selector.TextSelector(),
        vol.Required(CONF_DISTRIBUTOR): selector.TextSelector(),
        vol.Required(CONF_SERVICE_TYPE, default=DEFAULT_SERVICE_TYPE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["residential", "commercial"], translation_key="service_type"
            )
        ),
        vol.Optional(CONF_PRICE_UNIT): UNIT_SELECTOR,
    }
)


def _settings_schema(full: bool) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(CONF_USAGE_STATISTIC): selector.StatisticSelector(),
        vol.Optional(CONF_RATE_ENTITY): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_RATE_VALUE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, step="any", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_RATE_UNIT): UNIT_SELECTOR,
        vol.Required(CONF_THRESHOLD, default=DEFAULT_THRESHOLD): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, step=0.5, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_NO_CANCELLATION_FEE, default=False): selector.BooleanSelector(),
        vol.Optional(CONF_PRIOR_SUPPLIERS): selector.TextSelector(),
    }
    if full:
        fields[vol.Required(CONF_RATE_JUMP_PCT, default=DEFAULT_RATE_JUMP_PCT)] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=200, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX
            )
        )
        fields[vol.Required(CONF_THERMS_PER_MCF, default=DEFAULT_THERMS_PER_MCF)] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=15, step=0.01, mode=selector.NumberSelectorMode.BOX)
        )
    return vol.Schema(fields)


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop blank optional values so 'cleared' really means unset."""
    return {k: v for k, v in user_input.items() if v not in (None, "")}


class EnergyShopperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Energy Shopper for one gas market."""

    VERSION = 1

    def __init__(self) -> None:
        self._market: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            market = _clean(user_input)
            market[CONF_ZIPCODE] = market[CONF_ZIPCODE].strip()
            market[CONF_DISTRIBUTOR] = market[CONF_DISTRIBUTOR].strip()
            await self.async_set_unique_id(
                f"gas_{market[CONF_DISTRIBUTOR]}_{market[CONF_ZIPCODE]}_{market[CONF_SERVICE_TYPE]}"
            )
            self._abort_if_unique_id_configured()
            try:
                found = await self.hass.async_add_executor_job(
                    fetch_market,
                    market[CONF_DISTRIBUTOR],
                    market[CONF_ZIPCODE],
                    market[CONF_SERVICE_TYPE],
                )
            except requests.RequestException as err:
                _LOGGER.debug("PA Gas Switch request failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error fetching PA Gas Switch offers")
                errors["base"] = "unknown"
            else:
                if not found.offers:
                    errors["base"] = "no_offers"
                elif not found.price_to_compare and not market.get(CONF_PRICE_UNIT):
                    errors[CONF_PRICE_UNIT] = "unknown_unit"
                else:
                    self._market = market
                    return await self.async_step_settings()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(MARKET_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"Gas offers {self._market[CONF_ZIPCODE]}",
                data=self._market,
                options=_clean(user_input),
            )
        return self.async_show_form(step_id="settings", data_schema=_settings_schema(full=False))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergyShopperOptionsFlow()


class EnergyShopperOptionsFlow(OptionsFlow):
    """Change usage source, rate source, threshold and filters."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _settings_schema(full=True), self.config_entry.options
            ),
        )

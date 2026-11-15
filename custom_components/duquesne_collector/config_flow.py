"""Config flow for Duquesne Light (collector): the collector's URL and optional API token."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_TOKEN, CONF_URL
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify

from .api import CollectorAuthError, CollectorClient, CollectorError, NotACollectorError, normalize_url
from .const import CONF_STATISTIC_KEY, DEFAULT_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
        vol.Optional(CONF_API_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


class CollectorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        data = {CONF_URL: normalize_url(user_input[CONF_URL])}
        if user_input.get(CONF_API_TOKEN):
            data[CONF_API_TOKEN] = user_input[CONF_API_TOKEN]
        client = CollectorClient(async_get_clientsession(self.hass), data[CONF_URL], data.get(CONF_API_TOKEN))
        try:
            await client.status()
        except CollectorAuthError:
            return data, {CONF_API_TOKEN: "invalid_auth"}
        except NotACollectorError:
            return data, {"base": "not_collector"}
        except CollectorError as err:
            _LOGGER.debug("Collector check failed: %s", err)
            return data, {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error checking the collector")
            return data, {"base": "unknown"}
        return data, {}

    def _statistic_key(self) -> str:
        taken = {e.data.get(CONF_STATISTIC_KEY) for e in self._async_current_entries()}
        key, n = "duquesne_light", 1
        while key in taken:
            n += 1
            key = f"duquesne_light_{n}"
        return key

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._validate(user_input)
            if not errors:
                await self.async_set_unique_id(slugify(data[CONF_URL]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Duquesne Light",
                    data={**data, CONF_STATISTIC_KEY: self._statistic_key()},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the URL or token; the statistic ids stay, so history carries on."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._validate(user_input)
            if not errors:
                unique_id = slugify(data[CONF_URL])
                if any(
                    e.unique_id == unique_id and e.entry_id != entry.entry_id
                    for e in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    data={**data, CONF_STATISTIC_KEY: entry.data[CONF_STATISTIC_KEY]},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or {CONF_URL: entry.data[CONF_URL]}),
            errors=errors,
        )

"""Config flow for Peoples Gas: username and password, checked with a portal login."""

from __future__ import annotations

from collections.abc import Mapping
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
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .const import CONF_SAVE_BILL_PDFS, DEFAULT_SAVE_BILL_PDFS, DOMAIN
from .vendor.energy_shopper_core.utilities import peoples_gas as pg

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _fallback_account(username: str) -> str:
    return username.strip().lower()


def _login(username: str, password: str) -> str:
    """Log in and return the account number (blocking)."""
    client = pg.PeoplesGasClient(username, password)
    client.login()
    # The account number lives in a hidden input on the bill dashboard; fall back to the
    # username so a portal change can't block setup.
    return client._account_number() or _fallback_account(username)  # noqa: SLF001


async def validate_login(
    hass: HomeAssistant, username: str, password: str
) -> tuple[str | None, dict[str, str]]:
    """Return (account number, errors) for a login attempt. The account is the
    lowercased username when the portal didn't show the number."""
    try:
        account = await hass.async_add_executor_job(_login, username, password)
    except pg.PeoplesGasAuthError:
        return None, {"base": "invalid_auth"}
    except (requests.RequestException, ValueError, RuntimeError) as err:
        # RuntimeError: login page changed (no CSRF token); ValueError: bad JSON.
        _LOGGER.debug("Peoples Gas login failed: %s", err)
        return None, {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error logging in to Peoples Gas")
        return None, {"base": "unknown"}
    return account, {}


class PeoplesGasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Peoples Gas."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account, errors = await validate_login(
                self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                await self.async_set_unique_id(account)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Peoples Gas {account}", data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            account, errors = await validate_login(
                self.hass, data[CONF_USERNAME], data[CONF_PASSWORD]
            )
            if not errors:
                # Only a real account number can prove it's a different account: when
                # the portal hid it this time (the lookup fell back to the username), or
                # hid it at setup, don't block re-entering the password.
                fallback = _fallback_account(data[CONF_USERNAME])
                if account != fallback and entry.unique_id != _fallback_account(entry.data[CONF_USERNAME]):
                    await self.async_set_unique_id(account)
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data[CONF_USERNAME]
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PeoplesGasOptionsFlow()


class PeoplesGasOptionsFlow(OptionsFlow):
    """Options: whether to keep copies of bill PDFs."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SAVE_BILL_PDFS,
                        default=self.config_entry.options.get(
                            CONF_SAVE_BILL_PDFS, DEFAULT_SAVE_BILL_PDFS
                        ),
                    ): bool,
                }
            ),
        )

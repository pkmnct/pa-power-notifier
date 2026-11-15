"""Config, reauth and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
import requests

from custom_components.peoples_gas.const import CONF_SAVE_BILL_PDFS, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from conftest import ACCOUNT, PASSWORD, USERNAME

CREDS = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep flows from setting up the entry they create (that's tested in test_init)."""
    with patch("custom_components.peoples_gas.async_setup_entry", return_value=True) as mock:
        yield mock


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}
    return result


async def test_user_flow_creates_entry(recorder_mock, hass: HomeAssistant, mock_setup_entry, portal) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Peoples Gas {ACCOUNT}"
    assert result["data"] == CREDS
    assert result["result"].unique_id == ACCOUNT


async def test_user_flow_errors_then_recovers(recorder_mock, hass: HomeAssistant, mock_setup_entry, portal) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDS, CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    portal.login_error = requests.ConnectionError("down")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["errors"] == {"base": "cannot_connect"}

    portal.login_error = KeyError("surprise")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["errors"] == {"base": "unknown"}

    portal.login_error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(recorder_mock, hass: HomeAssistant, portal, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(recorder_mock, hass: HomeAssistant, mock_setup_entry, portal, config_entry) -> None:
    config_entry.add_to_hass(hass)
    portal.password = "new-secret"
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"


async def test_reauth_rejects_other_account(recorder_mock, hass: HomeAssistant, portal, config_entry) -> None:
    config_entry.add_to_hass(hass)
    portal.account = "9999999999"
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_options_flow(recorder_mock, hass: HomeAssistant, portal, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SAVE_BILL_PDFS: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SAVE_BILL_PDFS: True}

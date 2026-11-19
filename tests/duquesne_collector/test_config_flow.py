"""Config flow and reconfigure."""

from __future__ import annotations

from custom_components.duquesne_collector.const import CONF_STATISTIC_KEY, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from collections.abc import Generator
from unittest.mock import patch

import pytest

from conftest import URL, FakeCollector


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """Flow tests don't set the entry up, so no import races the recorder's teardown."""
    with patch("custom_components.duquesne_collector.async_setup_entry", return_value=True):
        yield


async def _submit(hass: HomeAssistant, user_input: dict):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_create(hass: HomeAssistant, collector: FakeCollector, aioclient_mock: AiohttpClientMocker) -> None:
    result = await _submit(hass, {CONF_URL: "collector.test:8765/", CONF_API_TOKEN: "s3cret"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Duquesne Light"
    assert result["data"] == {CONF_URL: URL, CONF_API_TOKEN: "s3cret", CONF_STATISTIC_KEY: "duquesne_light"}
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer s3cret"


async def test_second_collector_gets_its_own_statistics(hass: HomeAssistant, collector: FakeCollector, aioclient_mock) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="other", data={CONF_URL: "http://x", CONF_STATISTIC_KEY: "duquesne_light"}).add_to_hass(hass)
    result = await _submit(hass, {CONF_URL: URL})
    assert result["data"][CONF_STATISTIC_KEY] == "duquesne_light_2"


async def test_errors(hass: HomeAssistant, collector: FakeCollector, aioclient_mock: AiohttpClientMocker) -> None:
    collector.reject_auth = True
    # No token for a collector that wants one (it generates one by default): say where to find it.
    assert (await _submit(hass, {CONF_URL: URL}))["errors"] == {CONF_API_TOKEN: "token_required"}
    assert (await _submit(hass, {CONF_URL: URL, CONF_API_TOKEN: "wrong"}))["errors"] == {CONF_API_TOKEN: "invalid_auth"}
    collector.reject_auth = False
    collector.down = True
    assert (await _submit(hass, {CONF_URL: URL}))["errors"] == {"base": "cannot_connect"}
    aioclient_mock.get("http://not.collector/status", json={"hello": "world"})
    assert (await _submit(hass, {CONF_URL: "http://not.collector"}))["errors"] == {"base": "not_collector"}


async def test_already_configured(hass: HomeAssistant, collector: FakeCollector, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await _submit(hass, {CONF_URL: URL})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reconfigure_keeps_statistics(
    hass: HomeAssistant, collector: FakeCollector, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    for path in ("/status", "/bills", "/usage"):
        aioclient_mock.get(f"http://moved.test:8765{path}", side_effect=collector.respond)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://moved.test:8765", CONF_API_TOKEN: "new"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert config_entry.data == {
        CONF_URL: "http://moved.test:8765",
        CONF_API_TOKEN: "new",
        CONF_STATISTIC_KEY: "duquesne_light",
    }
    assert config_entry.unique_id == "http_moved_test_8765"

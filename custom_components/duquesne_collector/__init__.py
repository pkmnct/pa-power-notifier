"""Duquesne Light (collector): usage statistics, supplier and bills from the local collector."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import CollectorConfigEntry, CollectorCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: CollectorConfigEntry) -> bool:
    coordinator = CollectorCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CollectorConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: CollectorConfigEntry) -> None:
    """Drop this entry's repair issues and saved import count. Statistics stay, as with core Opower."""
    await CollectorCoordinator.async_remove_state(hass, entry.entry_id)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.endswith(entry.entry_id):
            ir.async_delete_issue(hass, DOMAIN, issue_id)

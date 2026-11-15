"""Energy Shopper: ranks PA Gas Switch offers against your own usage and rate."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import EnergyShopperConfigEntry, EnergyShopperCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EnergyShopperConfigEntry) -> bool:
    """Set up Energy Shopper from a config entry."""
    coordinator = EnergyShopperCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EnergyShopperConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnergyShopperConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EnergyShopperConfigEntry) -> None:
    """Drop this entry's stored state and any issues it raised."""
    await EnergyShopperCoordinator.async_remove_state(hass, entry.entry_id)
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and entry.entry_id in issue_id:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

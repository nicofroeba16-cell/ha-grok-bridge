from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AutoControlCenterClient
from .const import CONF_URL, DOMAIN, PLATFORMS
from .coordinator import AutoControlCenterCoordinator


@dataclass(slots=True)
class AutoControlCenterRuntimeData:
    coordinator: AutoControlCenterCoordinator


AutoControlCenterConfigEntry = ConfigEntry[AutoControlCenterRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: AutoControlCenterConfigEntry) -> bool:
    client = AutoControlCenterClient(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = AutoControlCenterCoordinator(hass, client, config_entry=entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = AutoControlCenterRuntimeData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AutoControlCenterConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.coordinator.async_stop()
    return True


async def async_reload_entry(hass: HomeAssistant, entry: AutoControlCenterConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

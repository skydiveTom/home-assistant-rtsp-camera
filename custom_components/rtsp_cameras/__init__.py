"""The RTSP Camera Manager integration."""

from __future__ import annotations

from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import RtspCamerasCoordinator

PLATFORMS: list[Platform] = [Platform.CAMERA]

RtspCamerasConfigEntry: TypeAlias = ConfigEntry[RtspCamerasCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RtspCamerasConfigEntry) -> bool:
    """Set up RTSP Camera Manager from a config entry."""
    coordinator = RtspCamerasCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RtspCamerasConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: RtspCamerasConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
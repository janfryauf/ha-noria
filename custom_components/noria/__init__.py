"""The Noria Online Monitoring integration (read-only)."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import NoriaConfigEntry, NoriaCoordinator, async_remove_store

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: NoriaConfigEntry) -> bool:
    """Set up Noria from a config entry."""
    coordinator = NoriaCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entities are fully added now: deliver the first refresh's alarm events (event.py).
    coordinator.async_update_listeners()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NoriaConfigEntry) -> bool:
    """Unload a config entry, persisting alarm tracker state first."""
    await entry.runtime_data.async_save()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: NoriaConfigEntry) -> None:
    """Remove stored alarm tracker state with the entry."""
    await async_remove_store(hass, entry.entry_id)

"""Alarm and status events produced by alarm reconciliation (docs/design.md §3)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NoriaConfigEntry, NoriaCoordinator
from .entity import NoriaEntity
from .models import ALARM_EVENT_TYPES, STATUS_EVENT_TYPES, AlarmCategory

PARALLEL_UPDATES = 0

EVENTS: dict[AlarmCategory, EventEntityDescription] = {
    AlarmCategory.ALARM: EventEntityDescription(key="alarm", translation_key="alarm", event_types=ALARM_EVENT_TYPES),
    AlarmCategory.STATUS: EventEntityDescription(
        key="status", translation_key="status", event_types=STATUS_EVENT_TYPES
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NoriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One alarm and one status event entity per device."""
    coordinator = entry.runtime_data
    async_add_entities(
        NoriaEventEntity(coordinator, device_id, category)
        for device_id in coordinator.data.devices
        for category in EVENTS
    )


class NoriaEventEntity(NoriaEntity, EventEntity):
    """Fires each reconciled alarm transition or status exactly once."""

    def __init__(self, coordinator: NoriaCoordinator, device_id: int, category: AlarmCategory) -> None:
        """Bind to one device and category."""
        super().__init__(coordinator, device_id, EVENTS[category])
        self._category = category
        self._handled_update: int | None = None

    # Events of the first refresh (e.g. delayed replays after a restart) are delivered by the
    # async_update_listeners() call in async_setup_entry, once every entity is fully added.
    # Firing from async_added_to_hass would not work: HA drops state writes while an entity
    # is still being added, so all but the last event would be lost.

    @callback
    def _handle_coordinator_update(self) -> None:
        self._fire_new_events()
        super()._handle_coordinator_update()

    @callback
    def _fire_new_events(self) -> None:
        data = self.coordinator.data
        if data.update_id == self._handled_update:
            return
        self._handled_update = data.update_id
        for event in data.events:
            if event.device_id == self.device_id and event.category is self._category:
                self._trigger_event(event.kind, event.attributes)
                self.async_write_ha_state()

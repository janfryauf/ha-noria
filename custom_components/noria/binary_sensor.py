"""Problem binary sensors driven by NOM's active-alarm list (docs/design.md §3)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NoriaConfigEntry
from .entity import NoriaEntity
from .models import AlarmCategory

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NoriaAlarmBinarySensorDescription(BinarySensorEntityDescription):
    """On while a matching alarm is active. kind=None matches any alarm."""

    kind: str | None
    per_pump: bool = False


BINARY_SENSORS: tuple[NoriaAlarmBinarySensorDescription, ...] = (
    # No translation key: named after the device class ("Problem").
    NoriaAlarmBinarySensorDescription(key="problem", kind=None),
    NoriaAlarmBinarySensorDescription(
        key="pump_failure", translation_key="pump_failure", kind="pump_failure", per_pump=True
    ),
    NoriaAlarmBinarySensorDescription(
        key="pump_damage_risk", translation_key="pump_damage_risk", kind="pump_damage_risk", per_pump=True
    ),
    NoriaAlarmBinarySensorDescription(key="emergency_level", translation_key="emergency_level", kind="emergency_level"),
    NoriaAlarmBinarySensorDescription(
        key="emergency_level_float_error",
        translation_key="emergency_level_float_error",
        kind="emergency_level_float_error",
    ),
    NoriaAlarmBinarySensorDescription(
        key="emergency_level_probe_error",
        translation_key="emergency_level_probe_error",
        kind="emergency_level_probe_error",
    ),
    NoriaAlarmBinarySensorDescription(
        key="probe_contamination", translation_key="probe_contamination", kind="probe_contamination"
    ),
    NoriaAlarmBinarySensorDescription(key="za_switch", translation_key="za_switch", kind="za_switch"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NoriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create alarm binary sensors; pump-specific ones once per pump."""
    coordinator = entry.runtime_data
    entities: list[NoriaAlarmBinarySensor] = []
    for device_id, data in coordinator.data.devices.items():
        for description in BINARY_SENSORS:
            if description.per_pump:
                entities.extend(
                    NoriaAlarmBinarySensor(coordinator, device_id, description, pump)
                    for pump in data.device.pump_numbers
                )
            else:
                entities.append(NoriaAlarmBinarySensor(coordinator, device_id, description))
    async_add_entities(entities)


class NoriaAlarmBinarySensor(NoriaEntity, BinarySensorEntity):
    """Reflects the active-alarm list only; short alarms show up as events instead."""

    entity_description: NoriaAlarmBinarySensorDescription
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def available(self) -> bool:
        """Unknown (not "no problem") when the active-alarm fetch failed."""
        return super().available and self.device_data.active_alarms is not None

    @property
    def is_on(self) -> bool:
        """Whether a matching alarm is active."""
        kind, pump = self.entity_description.kind, self.pump
        return any(
            alarm.category is AlarmCategory.ALARM
            and (kind is None or alarm.kind == kind)
            and (pump is None or alarm.pump_number == pump)
            for alarm in self.device_data.active_alarms or ()
        )

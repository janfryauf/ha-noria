"""Sensors for Noria TLAKAN pump stations (state classes: docs/design.md §5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import DeviceData, NoriaConfigEntry
from .entity import NoriaEntity
from .models import AlarmCategory, PumpTime, Reading

PARALLEL_UPDATES = 0

type SensorValue = StateType | date | datetime


@dataclass(frozen=True, kw_only=True)
class NoriaReadingSensorDescription(SensorEntityDescription):
    """Per-pump value from the latest reading."""

    value_fn: Callable[[Reading], SensorValue]


@dataclass(frozen=True, kw_only=True)
class NoriaPumpTimeSensorDescription(SensorEntityDescription):
    """Per-pump value from the newest pump-time window of one type."""

    time_type: str


@dataclass(frozen=True, kw_only=True)
class NoriaDeviceSensorDescription(SensorEntityDescription):
    """Device-level value."""

    value_fn: Callable[[DeviceData], SensorValue]
    available_fn: Callable[[DeviceData], bool] = lambda _: True
    attributes_fn: Callable[[DeviceData], dict[str, Any] | None] = lambda _: None


READING_SENSORS: tuple[NoriaReadingSensorDescription, ...] = (
    NoriaReadingSensorDescription(
        key="pumping_time",
        translation_key="pumping_time",
        device_class=SensorDeviceClass.DURATION,
        # TOTAL, not TOTAL_INCREASING: the counter dips by seconds after device restarts,
        # and HA reads a drop of 10% or more as a reset (inflating statistics).
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda r: r.total_time,
    ),
    NoriaReadingSensorDescription(
        key="pump_cycles",
        translation_key="pump_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda r: r.switch_count,
    ),
    # Alarm counters can be reset on the device and have dropped without a reset, so no
    # state class: they must not feed long-term statistics.
    NoriaReadingSensorDescription(
        key="pump_failure_count",
        translation_key="pump_failure_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.phase_alarm_count,
    ),
    NoriaReadingSensorDescription(
        key="emergency_level_count",
        translation_key="emergency_level_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.alarm_count,
    ),
    NoriaReadingSensorDescription(
        key="probe_sensitivity",
        translation_key="probe_sensitivity",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.prob_sens,
    ),
    NoriaReadingSensorDescription(
        key="pumping_time_last_interval",
        translation_key="pumping_time_last_interval",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_registry_enabled_default=False,
        # Negative after a device restart; negative pumping makes no sense.
        value_fn=lambda r: max(r.time_diff, 0) if r.time_diff is not None else None,
    ),
    NoriaReadingSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda r: r.timestamp,
    ),
)

PUMP_TIME_SENSORS: tuple[NoriaPumpTimeSensorDescription, ...] = (
    NoriaPumpTimeSensorDescription(
        key="average_pump_time",
        translation_key="average_pump_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        time_type="average_time",
    ),
    NoriaPumpTimeSensorDescription(
        key="reference_pump_time",
        translation_key="reference_pump_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        time_type="ref_time",
    ),
)


def _window_end(pump_time: PumpTime) -> float:
    moment = pump_time.measured_to or pump_time.measured_from
    return moment.timestamp() if moment else 0.0


def _active_alarms(data: DeviceData) -> list[str]:
    return sorted(a.kind for a in data.active_alarms or () if a.category is AlarmCategory.ALARM)


DEVICE_SENSORS: tuple[NoriaDeviceSensorDescription, ...] = (
    NoriaDeviceSensorDescription(
        key="active_alarms",
        translation_key="active_alarms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(_active_alarms(d)),
        available_fn=lambda d: d.active_alarms is not None,
        attributes_fn=lambda d: {"alarms": _active_alarms(d)},
    ),
    NoriaDeviceSensorDescription(
        key="last_message",
        translation_key="last_message",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.device.last_message_at,
    ),
    NoriaDeviceSensorDescription(
        key="subscription_expiration",
        translation_key="subscription_expiration",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.device.subscription_expires,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NoriaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every supported device and pump."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for device_id, data in coordinator.data.devices.items():
        entities.extend(NoriaDeviceSensor(coordinator, device_id, d) for d in DEVICE_SENSORS)
        for pump in data.device.pump_numbers:
            entities.extend(NoriaReadingSensor(coordinator, device_id, d, pump) for d in READING_SENSORS)
            entities.extend(NoriaPumpTimeSensor(coordinator, device_id, d, pump) for d in PUMP_TIME_SENSORS)
    async_add_entities(entities)


class NoriaReadingSensor(NoriaEntity, SensorEntity):
    """Value from the pump's latest reading."""

    entity_description: NoriaReadingSensorDescription

    @property
    def native_value(self) -> SensorValue:
        """Value, or None until the pump has a reading."""
        reading = self.device_data.readings.get(self.pump or 1)
        return self.entity_description.value_fn(reading) if reading else None


class NoriaPumpTimeSensor(NoriaEntity, SensorEntity):
    """Newest pump-time window of one type for the pump."""

    entity_description: NoriaPumpTimeSensorDescription

    def _newest(self) -> PumpTime | None:
        candidates = [
            t
            for t in self.device_data.pump_times
            if t.time_type == self.entity_description.time_type and t.pump_number == (self.pump or 1)
        ]
        return max(candidates, key=_window_end, default=None)

    @property
    def native_value(self) -> float | None:
        """Seconds."""
        newest = self._newest()
        return newest.seconds if newest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The window the value was measured over."""
        newest = self._newest()
        if newest is None:
            return None
        return {
            "measured_from": newest.measured_from.isoformat() if newest.measured_from else None,
            "measured_to": newest.measured_to.isoformat() if newest.measured_to else None,
            "caused_alarm": newest.caused_alarm,
        }


class NoriaDeviceSensor(NoriaEntity, SensorEntity):
    """Device-level value."""

    entity_description: NoriaDeviceSensorDescription

    @property
    def available(self) -> bool:
        """E.g. active alarms are unknown when their fetch failed."""
        return super().available and self.entity_description.available_fn(self.device_data)

    @property
    def native_value(self) -> SensorValue:
        """Value."""
        return self.entity_description.value_fn(self.device_data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra details, e.g. which alarms are active."""
        return self.entity_description.attributes_fn(self.device_data)

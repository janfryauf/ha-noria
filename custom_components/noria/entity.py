"""Base entity for Noria devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import DeviceData, NoriaCoordinator


class NoriaEntity(CoordinatorEntity[NoriaCoordinator]):
    """Entity bound to one NOM device and, optionally, one pump."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NoriaCoordinator,
        device_id: int,
        description: EntityDescription,
        pump: int | None = None,
    ) -> None:
        """Set identity; per-pump entities get a pump-aware unique_id and name."""
        super().__init__(coordinator)
        self.entity_description = description
        self.device_id = device_id
        self.pump = pump
        device = coordinator.data.devices[device_id].device
        self._attr_unique_id = (
            f"{device_id}_pump{pump}_{description.key}" if pump is not None else f"{device_id}_{description.key}"
        )
        if pump is not None and device.device_type.pump_count > 1 and description.translation_key:
            self._attr_translation_key = f"{description.translation_key}_pump"
            self._attr_translation_placeholders = {"pump": str(pump)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(device_id))},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.device_type.name,
            model_id=device.device_type.order_name,
            hw_version=str(device.firmware) if device.firmware is not None else None,
            sw_version=str(device.software) if device.software is not None else None,
            serial_number=device.serial_number,
        )

    @property
    def device_data(self) -> DeviceData:
        """Current data for this entity's device."""
        return self.coordinator.data.devices[self.device_id]

    @property
    def available(self) -> bool:
        """Unavailable when the device disappears from the account."""
        return super().available and self.device_id in self.coordinator.data.devices

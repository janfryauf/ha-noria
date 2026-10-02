"""Diagnostics for Noria Online Monitoring, with personal data redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import NoriaConfigEntry

# Keys as observed live AND as spelled in the spec: they differ (live `street` vs spec
# `address`, live `device_user_contact` vs spec `devise_user_contact`), and either may appear.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "unique_id",
    "name",
    "serial_number",
    "wmbus_serial",
    "street",
    "address",
    "city",
    "zip",
    "latitude",
    "longitude",
    "location_string",
    "alarms_email",
    "reports_email",
    "device_user_contact",
    "devise_user_contact",
    "device_uid",
    "device_imsi",
    "note",
    "noria_id",
    "pac",
    "token_listing_id",
    "listing_id",
    "raw_data",
    "user_id",
    "old_device_number",
    "location_number",
    "meter_number",
    "water_meter_number",
    "power_meter_number",
    "protocol_url",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: NoriaConfigEntry) -> dict[str, Any]:
    """Return redacted coordinator state."""
    coordinator = entry.runtime_data
    data = coordinator.data
    devices: dict[str, Any] = {}
    for device_id, device_data in data.devices.items():
        devices[str(device_id)] = {
            "device": async_redact_data(device_data.device.raw, TO_REDACT),
            "readings": {str(pump): asdict(reading) for pump, reading in device_data.readings.items()},
            "active_alarms": None
            if device_data.active_alarms is None
            else [asdict(alarm) for alarm in device_data.active_alarms],
            "pump_times": [asdict(pump_time) for pump_time in device_data.pump_times],
            # Delivery state is queued or sent only; "sent" does not mean the device acted on it.
            "downlinks": [
                {
                    "id": downlink.id,
                    "label": downlink.label,
                    "data": downlink.safe_data,
                    "state": downlink.state,
                    "created_at": downlink.created_at,
                    "sent_at": downlink.sent_at,
                }
                for downlink in device_data.downlinks
            ],
        }
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "devices": devices,
        "unsupported_devices": data.unsupported,
        "alarm_tracker": coordinator.tracker.as_dict(),
        "failing_calls": sorted(f"{device_id}:{part}" for device_id, part in coordinator.failing),
    }

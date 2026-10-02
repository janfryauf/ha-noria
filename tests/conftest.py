"""Shared fixtures. API data comes from sanitized live fixtures in tests/fixtures/."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, load_json_array_fixture

from custom_components.noria.const import DOMAIN
from custom_components.noria.models import Alarm, Device, DownlinkEvent, PumpTime, Reading

USERNAME = "user@example.com"
PASSWORD = "secret"
DEVICE_ID = 3040
DEVICE_NAME = "Chata"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom_components in every test."""


def device_rows() -> list[dict[str, Any]]:
    """The fixture device with a readable name (the fixture name is redacted)."""
    return [{**row, "name": DEVICE_NAME} for row in load_json_array_fixture("devices.json")]


def latest_readings() -> dict[int, Reading]:
    """Newest fixture reading per pump."""
    readings = [r for row in load_json_array_fixture("readings_tlakan.json") if (r := Reading.from_api(row))]
    return {1: max(readings, key=lambda r: r.timestamp)}


def history_alarms() -> list[Alarm]:
    """Fixture alarm history (all ended)."""
    return [Alarm.from_api(row) for row in load_json_array_fixture("alarms_tlakan.json")]


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A configured NOM account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch NoriaClient everywhere it is imported; serve fixture data."""
    with (
        patch("custom_components.noria.coordinator.NoriaClient", autospec=True) as client_cls,
        patch("custom_components.noria.config_flow.NoriaClient", new=client_cls),
    ):
        client = client_cls.return_value
        client.async_get_device_count.return_value = 1
        client.async_get_devices.return_value = [Device.from_api(row) for row in device_rows()]
        client.async_get_active_alarms.return_value = []
        client.async_get_alarm_history.return_value = history_alarms()
        client.async_get_alarm.side_effect = AssertionError("no lookup expected")
        client.async_get_latest_readings.return_value = latest_readings()
        client.async_get_pump_times.return_value = [
            p for row in load_json_array_fixture("pump_times_tlakan.json") if (p := PumpTime.from_api(row))
        ]
        client.async_get_downlink_events.return_value = [
            DownlinkEvent.from_api(row) for row in load_json_array_fixture("downlink_events_tlakan.json")
        ]
        yield client


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry and wait for setup."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

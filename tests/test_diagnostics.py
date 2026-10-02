"""Diagnostics redact personal data and keep command states honest."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.noria.models import Device

from .conftest import DEVICE_NAME, PASSWORD, USERNAME, device_rows, setup_integration


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_integration(hass, mock_config_entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
    text = json.dumps(diagnostics)
    for secret in (USERNAME, PASSWORD, DEVICE_NAME):
        assert secret not in text

    device = diagnostics["devices"]["3040"]
    assert device["device"]["serial_number"] == "**REDACTED**"
    assert device["device"]["device_address"]["street"] == "**REDACTED**"
    assert device["active_alarms"] == []
    downlinks = device["downlinks"]
    assert {d["state"] for d in downlinks} == {"sent"}  # never "confirmed"
    assert {d["data"] for d in downlinks} >= {"9100", "9101"}
    assert all(len(d["data"]) <= 8 for d in downlinks)
    assert diagnostics["failing_calls"] == []


async def test_diagnostics_redact_spec_spellings_of_private_fields(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The spec spells some fields differently from the live API (address, devise_user_contact)."""
    row = device_rows()[0]
    row["device_address"] = {**row["device_address"], "address": "Hlavní 12, Lipno"}
    row["user_info"] = {**row["user_info"], "devise_user_contact": "Jan Novák +420 777 123 456"}
    row["device_info"] = {**row["device_info"], "device_uid": "UID-123456", "device_imsi": "230011234567890"}
    mock_client.async_get_devices.return_value = [Device.from_api(row)]
    await setup_integration(hass, mock_config_entry)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
    text = json.dumps(diagnostics, ensure_ascii=False)
    for value in ("Hlavní 12", "Jan Novák", "777 123 456", "UID-123456", "230011234567890"):
        assert value not in text
    device = diagnostics["devices"]["3040"]["device"]
    assert device["device_address"]["address"] == "**REDACTED**"
    assert device["user_info"]["devise_user_contact"] == "**REDACTED**"

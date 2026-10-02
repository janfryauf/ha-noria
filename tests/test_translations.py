"""Every key the code uses exists in translations/en.json and icons.json."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.noria import binary_sensor, sensor
from custom_components.noria.models import ALARM_EVENT_TYPES, STATUS_EVENT_TYPES

COMPONENT = Path(__file__).parents[1] / "custom_components" / "noria"
STRINGS = json.loads((COMPONENT / "translations" / "en.json").read_text())
ICONS = json.loads((COMPONENT / "icons.json").read_text())

PER_PUMP_SENSORS = [*sensor.READING_SENSORS, *sensor.PUMP_TIME_SENSORS]
PER_PUMP_BINARY = [d for d in binary_sensor.BINARY_SENSORS if d.per_pump]


def test_sensor_names_and_icons() -> None:
    names, icons = STRINGS["entity"]["sensor"], ICONS["entity"]["sensor"]
    for description in [*PER_PUMP_SENSORS, *sensor.DEVICE_SENSORS]:
        assert description.translation_key in names
        assert description.translation_key in icons
    for description in PER_PUMP_SENSORS:
        assert "{pump}" in names[f"{description.translation_key}_pump"]["name"]
        assert f"{description.translation_key}_pump" in icons


def test_binary_sensor_names_and_icons() -> None:
    names, icons = STRINGS["entity"]["binary_sensor"], ICONS["entity"]["binary_sensor"]
    for description in binary_sensor.BINARY_SENSORS:
        if description.translation_key is None:
            continue  # named after the device class
        assert description.translation_key in names
        assert description.translation_key in icons
    for description in PER_PUMP_BINARY:
        assert "{pump}" in names[f"{description.translation_key}_pump"]["name"]


def test_event_types_are_translated() -> None:
    events = STRINGS["entity"]["event"]
    assert set(events["alarm"]["state_attributes"]["event_type"]["state"]) == set(ALARM_EVENT_TYPES)
    assert set(events["status"]["state_attributes"]["event_type"]["state"]) == set(STATUS_EVENT_TYPES)
    assert set(ICONS["entity"]["event"]) == {"alarm", "status"}


def test_flow_and_exception_strings() -> None:
    config = STRINGS["config"]
    assert set(config["step"]) == {"user", "reauth_confirm", "reconfigure"}
    assert {"cannot_connect", "invalid_auth", "unknown"} <= set(config["error"])
    assert {"already_configured", "reauth_successful", "reconfigure_successful", "wrong_account"} <= set(
        config["abort"]
    )
    for step in config["step"].values():
        assert set(step["data"]) == set(step["data_description"])
    assert {"cannot_connect", "invalid_auth", "rate_limited"} <= set(STRINGS["exceptions"])

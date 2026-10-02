"""Setup, entities, failure handling and the alarm lifecycle through real entities."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.noria.api import (
    NoriaAuthError,
    NoriaConnectionError,
    NoriaForbiddenError,
    NoriaRateLimitError,
    NoriaResponseError,
)
from custom_components.noria.const import DOMAIN
from custom_components.noria.models import Alarm

from .conftest import DEVICE_ID, history_alarms, setup_integration


def alarm(
    alarm_id: int, *, created: datetime, number: int = 3, status: bool = False, end: datetime | None = None
) -> Alarm:
    start = created - timedelta(seconds=15)
    if status:
        end = start
    return Alarm.from_api(
        {
            "id": alarm_id,
            "device_id": DEVICE_ID,
            "alarm_type": "tlakan_status" if status else "tlakan_alarm",
            "alarm_number": number,
            "human_number": f"{9 if status else 1}{number:02d}",
            "pump_number": 1,
            "active": end is None,
            "status_start": start.isoformat(),
            "status_end": end.isoformat() if end else None,
            "created_at": created.isoformat(),
            "updated_at": (end or created).isoformat(),
            "system_notice": False,
            "human_description": "test",
        }
    )


async def refresh(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def state(hass: HomeAssistant, entity_id: str) -> Any:
    current = hass.states.get(entity_id)
    assert current is not None, entity_id
    return current


async def test_setup_entities_and_unload_persists_tracker(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    pumping = state(hass, "sensor.chata_pumping_time")
    assert float(pumping.state) == pytest.approx(234780 / 3600, rel=1e-6)
    assert pumping.attributes["unit_of_measurement"] == "h"
    assert pumping.attributes["state_class"] == "total"
    cycles = state(hass, "sensor.chata_pump_cycles")
    assert (cycles.state, cycles.attributes["state_class"]) == ("5524", "total_increasing")
    failures = state(hass, "sensor.chata_pump_failure_count")
    assert failures.state == "11"
    assert "state_class" not in failures.attributes
    assert state(hass, "sensor.chata_average_pump_time").state == "409.6"
    assert state(hass, "sensor.chata_subscription_expiration").state == "2027-09-30"
    assert state(hass, "sensor.chata_active_alarms").state == "0"
    assert state(hass, "binary_sensor.chata_problem").state == "off"
    assert state(hass, "binary_sensor.chata_pump_failure").state == "off"
    assert state(hass, "event.chata_alarm").state == "unknown"  # seeding fires nothing
    assert state(hass, "event.chata_status").state == "unknown"
    assert hass.states.get("sensor.chata_probe_sensitivity") is None  # disabled by default

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert f"{DOMAIN}.{mock_config_entry.entry_id}.alarms" in hass_storage


async def test_remove_entry_deletes_tracker_store(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    await setup_integration(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{mock_config_entry.entry_id}.alarms" not in hass_storage


@pytest.mark.parametrize(
    ("side_effect", "entry_state"),
    [(NoriaConnectionError, ConfigEntryState.SETUP_RETRY), (NoriaAuthError, ConfigEntryState.SETUP_ERROR)],
)
async def test_setup_failures(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    side_effect: type[Exception],
    entry_state: ConfigEntryState,
) -> None:
    mock_client.async_get_devices.side_effect = side_effect
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is entry_state
    if side_effect is NoriaAuthError:
        assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_failed_active_alarms_make_alarm_entities_unavailable_only(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_client.async_get_active_alarms.side_effect = NoriaConnectionError
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, "binary_sensor.chata_problem").state == STATE_UNAVAILABLE
    assert state(hass, "binary_sensor.chata_pump_failure").state == STATE_UNAVAILABLE
    assert state(hass, "sensor.chata_active_alarms").state == STATE_UNAVAILABLE
    assert state(hass, "sensor.chata_pump_cycles").state == "5524"


async def test_failed_readings_keep_previous_values(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    device = mock_client.async_get_devices.return_value[0]
    mock_client.async_get_devices.return_value = [replace(device, last_event_id=(device.last_event_id or 0) + 1)]
    mock_client.async_get_latest_readings.side_effect = NoriaConnectionError
    await refresh(hass, freezer)
    assert mock_client.async_get_latest_readings.await_count == 2
    assert state(hass, "sensor.chata_pump_cycles").state == "5524"


async def test_readings_are_only_fetched_after_a_new_uplink(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    await refresh(hass, freezer)
    assert mock_client.async_get_latest_readings.await_count == 1
    assert mock_client.async_get_active_alarms.await_count == 2  # alarms are polled every update


async def test_alarm_lifecycle_through_entities(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    # A pump failure opens.
    created = dt_util.utcnow() + timedelta(minutes=2)
    open_alarm = alarm(900, created=created)
    mock_client.async_get_active_alarms.return_value = [open_alarm]
    mock_client.async_get_alarm_history.return_value = [*history_alarms(), open_alarm]
    await refresh(hass, freezer)
    event = state(hass, "event.chata_alarm")
    assert event.attributes["event_type"] == "pump_failure"
    assert (event.attributes["phase"], event.attributes["delayed"]) == ("started", False)
    assert state(hass, "binary_sensor.chata_pump_failure").state == "on"
    assert state(hass, "binary_sensor.chata_problem").state == "on"
    assert state(hass, "sensor.chata_active_alarms").state == "1"

    # Hours later it ends; history windows no longer contain it, so the end comes from a lookup.
    ended = alarm(900, created=created, end=created + timedelta(hours=3))
    mock_client.async_get_active_alarms.return_value = []
    mock_client.async_get_alarm_history.return_value = []
    mock_client.async_get_alarm.side_effect = None
    mock_client.async_get_alarm.return_value = ended
    await refresh(hass, freezer)
    mock_client.async_get_alarm.assert_awaited_with(DEVICE_ID, 900)
    event = state(hass, "event.chata_alarm")
    assert (event.attributes["event_type"], event.attributes["phase"]) == ("pump_failure", "ended")
    assert event.attributes["duration_s"] == 3 * 3600 + 15
    assert state(hass, "binary_sensor.chata_pump_failure").state == "off"

    # A 60 s emergency level between two polls, never in the active list, plus a status.
    short_created = dt_util.utcnow() + timedelta(minutes=1)
    short = alarm(901, number=6, created=short_created, end=short_created + timedelta(seconds=45))
    cleaning = alarm(902, number=9, status=True, created=short_created)
    mock_client.async_get_alarm_history.return_value = [short, cleaning]
    await refresh(hass, freezer)
    alarm_changes = [c.data["new_state"].attributes for c in changes if c.data["entity_id"] == "event.chata_alarm"]
    assert [(a["event_type"], a["phase"]) for a in alarm_changes] == [
        ("pump_failure", "started"),
        ("pump_failure", "ended"),
        ("emergency_level", "started"),
        ("emergency_level", "ended"),
    ]
    assert state(hass, "binary_sensor.chata_emergency_level").state == "off"  # never active
    status = state(hass, "event.chata_status")
    assert (status.attributes["event_type"], status.attributes["code"]) == ("cleaning", "909")

    # The same records in the next (overlapping) window fire nothing new.
    before = len(changes)
    await refresh(hass, freezer)
    assert not [c for c in changes[before:] if c.data["entity_id"].startswith("event.")]


async def test_two_pump_controller_gets_entities_per_pump(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Synthetic 2-pump device (no real one exists on the account)."""
    device = mock_client.async_get_devices.return_value[0]
    two_pumps = replace(device, device_type=replace(device.device_type, pump_count=2))
    mock_client.async_get_devices.return_value = [two_pumps]
    reading = mock_client.async_get_latest_readings.return_value[1]
    mock_client.async_get_latest_readings.return_value = {
        1: reading,
        2: replace(reading, pump_number=2, switch_count=7),
    }
    pump_2_failure = replace(alarm(910, number=2, created=dt_util.utcnow()), pump_number=2)
    mock_client.async_get_active_alarms.return_value = [pump_2_failure]
    await setup_integration(hass, mock_config_entry)

    assert state(hass, "sensor.chata_pump_1_cycles").state == "5524"
    assert state(hass, "sensor.chata_pump_2_cycles").state == "7"
    assert state(hass, "binary_sensor.chata_pump_1_failure").state == "off"
    assert state(hass, "binary_sensor.chata_pump_2_failure").state == "on"
    assert state(hass, "binary_sensor.chata_problem").state == "on"
    mock_client.async_get_latest_readings.assert_awaited_with(DEVICE_ID, 2)


async def test_unsupported_device_families_are_skipped(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    device = mock_client.async_get_devices.return_value[0]
    meter = replace(device, id=4000, device_type=replace(device.device_type, parse_method="water_meter"))
    mock_client.async_get_devices.return_value = [device, meter]
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.data.unsupported == {4000: "water_meter"}
    assert set(mock_config_entry.runtime_data.data.devices) == {DEVICE_ID}


async def test_rate_limit_sets_retry_after(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    mock_client.async_get_devices.side_effect = NoriaRateLimitError(120)
    await refresh(hass, freezer)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.last_update_success is False
    assert state(hass, "sensor.chata_pump_cycles").state == STATE_UNAVAILABLE


async def test_auth_error_in_a_device_call_starts_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    mock_client.async_get_active_alarms.side_effect = NoriaAuthError
    await refresh(hass, freezer)
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_optional_failure_logs_once_and_recovery_is_logged(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_integration(hass, mock_config_entry)
    mock_client.async_get_alarm_history.side_effect = NoriaConnectionError("down")
    await refresh(hass, freezer)
    await refresh(hass, freezer)
    assert caplog.text.count("Fetching alarm_history for Noria device 3040 failed") == 1
    assert mock_config_entry.runtime_data.failing == {(DEVICE_ID, "alarm_history")}
    mock_client.async_get_alarm_history.side_effect = None
    await refresh(hass, freezer)
    assert "Fetching alarm_history for Noria device 3040 works again" in caplog.text
    assert mock_config_entry.runtime_data.failing == set()


# --- regressions from review ---------------------------------------------------


def alarm_changes(changes: list[Any], entity_id: str) -> list[dict[str, Any]]:
    """Attributes of every state change of an event entity that carried an event."""
    return [
        c.data["new_state"].attributes
        for c in changes
        if c.data["entity_id"] == entity_id and c.data["new_state"].attributes.get("event_type")
    ]


async def test_restart_replay_delivers_every_event_through_entities(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Events of the first refresh must all reach the state machine, not just the last one."""
    now = dt_util.utcnow()
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.alarms"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"devices": {str(DEVICE_ID): {"synced_until": (now - timedelta(hours=1)).isoformat(), "records": []}}},
    }
    created = now - timedelta(minutes=30)
    short = alarm(920, number=6, created=created, end=created + timedelta(minutes=1))
    cleaning = alarm(921, number=9, status=True, created=now - timedelta(minutes=20))
    mock_client.async_get_alarm_history.return_value = [*history_alarms(), short, cleaning]
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    await setup_integration(hass, mock_config_entry)

    replayed = alarm_changes(changes, "event.chata_alarm")
    assert [(a["event_type"], a["phase"], a["delayed"]) for a in replayed] == [
        ("emergency_level", "started", True),
        ("emergency_level", "ended", True),
    ]
    assert [a["event_type"] for a in alarm_changes(changes, "event.chata_status")] == ["cleaning"]


async def test_events_survive_a_failing_later_call_in_the_same_update(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    device = mock_client.async_get_devices.return_value[0]
    mock_client.async_get_devices.return_value = [device, replace(device, id=4000, name="Sklep")]
    state_of = {"show": False, "fail_second_device": False}
    open_alarm = alarm(930, created=dt_util.utcnow() + timedelta(minutes=1))

    def active_alarms(device_id: int) -> list[Alarm]:
        if device_id == 4000 and state_of["fail_second_device"]:
            raise NoriaRateLimitError(None)
        return [open_alarm] if device_id == DEVICE_ID and state_of["show"] else []

    mock_client.async_get_active_alarms.side_effect = active_alarms
    mock_client.async_get_alarm_history.side_effect = lambda device_id, since: (
        [open_alarm] if device_id == DEVICE_ID and state_of["show"] else []
    )
    await setup_integration(hass, mock_config_entry)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    state_of.update(show=True, fail_second_device=True)
    await refresh(hass, freezer)  # device 3040 reconciled, then device 4000 is rate limited
    assert mock_config_entry.runtime_data.last_update_success is False
    assert alarm_changes(changes, "event.chata_alarm") == []

    state_of["fail_second_device"] = False
    await refresh(hass, freezer)
    assert [(a["event_type"], a["phase"]) for a in alarm_changes(changes, "event.chata_alarm")] == [
        ("pump_failure", "started")
    ]


async def test_readings_are_retried_after_a_failure_on_the_same_uplink(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    device = mock_client.async_get_devices.return_value[0]
    mock_client.async_get_devices.return_value = [replace(device, last_event_id=(device.last_event_id or 0) + 1)]
    reading = mock_client.async_get_latest_readings.return_value[1]
    mock_client.async_get_latest_readings.side_effect = NoriaConnectionError
    await refresh(hass, freezer)
    assert state(hass, "sensor.chata_pump_cycles").state == "5524"

    mock_client.async_get_latest_readings.side_effect = None
    mock_client.async_get_latest_readings.return_value = {1: replace(reading, switch_count=6000)}
    await refresh(hass, freezer)  # same uplink id: retried because the last fetch failed
    assert mock_client.async_get_latest_readings.await_count == 3
    assert state(hass, "sensor.chata_pump_cycles").state == "6000"
    await refresh(hass, freezer)
    assert mock_client.async_get_latest_readings.await_count == 3


async def test_forbidden_optional_endpoint_does_not_start_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_client.async_get_downlink_events.side_effect = NoriaForbiddenError
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress()
    assert (DEVICE_ID, "downlinks") in mock_config_entry.runtime_data.failing


async def test_forbidden_device_list_starts_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_client.async_get_devices.side_effect = NoriaForbiddenError
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_store_round_trip_reports_an_end_learned_after_reload(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    created = dt_util.utcnow() + timedelta(minutes=1)
    open_alarm = alarm(940, created=created)
    mock_client.async_get_active_alarms.return_value = [open_alarm]
    mock_client.async_get_alarm_history.return_value = [open_alarm]
    await refresh(hass, freezer)
    assert state(hass, "binary_sensor.chata_pump_failure").state == "on"

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    mock_client.async_get_active_alarms.return_value = []
    mock_client.async_get_alarm_history.return_value = []
    mock_client.async_get_alarm.side_effect = None
    mock_client.async_get_alarm.return_value = alarm(940, created=created, end=created + timedelta(minutes=2))
    freezer.tick(timedelta(minutes=3))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mock_client.async_get_alarm.assert_awaited_with(DEVICE_ID, 940)
    event = state(hass, "event.chata_alarm")
    assert (event.attributes["event_type"], event.attributes["phase"]) == ("pump_failure", "ended")
    assert state(hass, "binary_sensor.chata_pump_failure").state == "off"


async def test_event_entity_shows_the_newest_of_several_statuses(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await setup_integration(hass, mock_config_entry)
    now = dt_util.utcnow()
    power = alarm(950, number=1, status=True, created=now + timedelta(minutes=1))
    reregistration = alarm(951, number=13, status=True, created=now + timedelta(minutes=2))
    mock_client.async_get_alarm_history.return_value = [reregistration, power]  # API order: newest first
    await refresh(hass, freezer)
    status = state(hass, "event.chata_status")
    assert (status.attributes["event_type"], status.attributes["alarm_id"]) == ("nb_iot_reregistration", 951)


async def test_incomplete_lists_keep_previous_state_and_watermark(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """More pages than MAX_PAGES: the client raises instead of returning a partial list."""
    await setup_integration(hass, mock_config_entry)
    tracker = mock_config_entry.runtime_data.tracker
    synced_before = tracker.as_dict()["devices"][str(DEVICE_ID)]["synced_until"]

    incomplete = NoriaResponseError("/devices/3040/alarms has more than 1000 items; refusing to use an incomplete list")
    mock_client.async_get_active_alarms.side_effect = incomplete
    mock_client.async_get_alarm_history.side_effect = incomplete
    await refresh(hass, freezer)

    assert state(hass, "binary_sensor.chata_problem").state == STATE_UNAVAILABLE  # unknown, not "no problem"
    assert tracker.as_dict()["devices"][str(DEVICE_ID)]["synced_until"] == synced_before
    assert state(hass, "sensor.chata_pump_cycles").state == "5524"

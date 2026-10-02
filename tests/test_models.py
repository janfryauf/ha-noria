"""Parsing of live NOM fixtures into models."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pytest_homeassistant_custom_component.common import load_json_array_fixture

from custom_components.noria.models import (
    NOM_TIMEZONE,
    Alarm,
    AlarmCategory,
    CommandState,
    Device,
    DownlinkEvent,
    PumpTime,
    Reading,
    parse_datetime,
)


def test_device_from_fixture() -> None:
    device = Device.from_api(load_json_array_fixture("devices.json")[0])
    assert device.id == 3040
    assert device.is_tlakan
    assert device.device_type.name == "TLK P4-NBr"
    assert device.device_type.pump_count == 1
    assert list(device.pump_numbers) == [1]
    assert device.device_type.has_downlink is True
    assert device.firmware == 12106
    assert device.software == 63784
    assert device.subscription_expires == date(2027, 9, 30)
    assert device.last_message_at == datetime(2026, 10, 1, 17, 26, tzinfo=UTC)
    assert device.last_event_id is not None


def test_readings_parse_and_dedupe_key() -> None:
    rows = load_json_array_fixture("readings_duplicates_tlakan.json")
    readings = [Reading.from_api(row) for row in rows]
    assert all(r is not None for r in readings)
    keys = {r.key for r in readings if r}
    assert len(rows) == 6
    assert len(keys) == 3

    newest = Reading.from_api(load_json_array_fixture("readings_tlakan.json")[0])
    assert newest is not None
    assert (newest.total_time, newest.switch_count, newest.phase_alarm_count) == (234780, 5524, 11)
    assert newest.timestamp.utcoffset() == timedelta(hours=2)


def test_restart_reading_has_negative_time_diff() -> None:
    readings = [Reading.from_api(row) for row in load_json_array_fixture("readings_tlakan.json")]
    assert any(r and r.time_diff is not None and r.time_diff < 0 for r in readings)


def test_alarm_kinds_from_fixture_codes() -> None:
    alarms = {a.code: a for a in map(Alarm.from_api, load_json_array_fixture("alarm_examples_tlakan.json"))}
    expected = {
        "103": (AlarmCategory.ALARM, "pump_failure"),
        "104": (AlarmCategory.ALARM, "emergency_level_float_error"),
        "105": (AlarmCategory.ALARM, "emergency_level_probe_error"),
        "106": (AlarmCategory.ALARM, "emergency_level"),
        "107": (AlarmCategory.ALARM, "probe_contamination"),
        "108": (AlarmCategory.ALARM, "za_switch"),
        "111": (AlarmCategory.ALARM, "pump_damage_risk"),
        "901": (AlarmCategory.STATUS, "power_connected"),
        "902": (AlarmCategory.STATUS, "manual_pumping"),
        "903": (AlarmCategory.STATUS, "alarm_counters_reset"),
        "909": (AlarmCategory.STATUS, "cleaning"),
        "913": (AlarmCategory.STATUS, "nb_iot_reregistration"),
    }
    assert {code: (a.category, a.kind) for code, a in alarms.items()} == expected
    # alarm_number 3 is both an alarm (103) and a status (903)
    assert alarms["103"].alarm_number == alarms["903"].alarm_number == 3


def test_pump_times_parse() -> None:
    times = [PumpTime.from_api(row) for row in load_json_array_fixture("pump_times_tlakan.json")]
    assert all(t is not None for t in times)
    assert {t.time_type for t in times if t} == {"average_time", "ref_time", "ref_time_active"}


def test_downlink_states_are_queued_or_sent_never_confirmed() -> None:
    rows = load_json_array_fixture("downlink_events_tlakan.json")
    downlinks = [DownlinkEvent.from_api(row) for row in rows]
    assert {d.state for d in downlinks} == {CommandState.SENT}  # all fixture rows were sent
    horn_on = next(d for d in downlinks if d.data == "9101")
    assert horn_on.sent_at
    assert horn_on.created_at
    assert horn_on.sent_at > horn_on.created_at

    queued = DownlinkEvent.from_api({**rows[0], "sent": False, "sent_at": None})
    assert queued.state is CommandState.QUEUED
    # Nothing in downlink history can make a command physically confirmed.
    assert all(
        DownlinkEvent.from_api({**row, "sent": sent}).state is not CommandState.CONFIRMED
        for row in rows
        for sent in (True, False)
    )


def test_downlink_payload_exposure() -> None:
    row = load_json_array_fixture("downlink_events_tlakan.json")[0]
    assert DownlinkEvent.from_api({**row, "data": "9101"}).safe_data == "9101"
    long_payload = "0A0B0C0D" + "687474703A2F2F31302E302E302E31".ljust(100, "0")
    assert DownlinkEvent.from_api({**row, "data": long_payload}).safe_data == f"<{len(long_payload) // 2} bytes>"


def test_parse_datetime_reads_naive_times_as_prague() -> None:
    assert parse_datetime("2026-10-01T09:29:05") == datetime(2026, 10, 1, 9, 29, 5, tzinfo=NOM_TIMEZONE)
    assert parse_datetime("2026-10-01T07:29:05Z") == datetime(2026, 10, 1, 7, 29, 5, tzinfo=UTC)
    assert parse_datetime(None) is None
    assert parse_datetime("not a date") is None

"""Typed models for Noria Online Monitoring (NOM) API responses.

Deliberately free of Home Assistant imports so the client can become a library.
Field meanings and quirks are documented in docs/design.md and the live findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

NOM_TIMEZONE = ZoneInfo("Europe/Prague")
TLAKAN_PARSE_METHODS = frozenset({"tlakan", "multi_tlakan"})

ALARM_TYPE = "tlakan_alarm"
STATUS_TYPE = "tlakan_status"


class AlarmCategory(StrEnum):
    """Whether a record is a lasting alarm or an instantaneous status."""

    ALARM = "alarm"
    STATUS = "status"


# alarm_number -> kind. Numbers repeat across alarm_type (3 is pump failure as an alarm
# and alarm counters reset as a status), so always look up by (alarm_type, alarm_number).
# Observed live: alarms 3-8 and 11, statuses 1-3, 9 and 13. The rest come from the
# spec's al*/st* bit descriptions and have not been seen yet.
ALARM_KINDS: dict[int, str] = {
    1: "pump_failure",  # spec al1: pump 1 failure
    2: "pump_failure",  # spec al2: pump 2 failure
    3: "pump_failure",
    4: "emergency_level_float_error",
    5: "emergency_level_probe_error",
    6: "emergency_level",
    7: "probe_contamination",
    8: "za_switch",
    11: "pump_damage_risk",
}
STATUS_KINDS: dict[int, str] = {
    1: "power_connected",
    2: "manual_pumping",
    3: "alarm_counters_reset",
    4: "daily_message_limit",
    5: "drained_by_float",
    6: "drained_after_timeout",
    7: "remote_horn_off",
    8: "remote_horn_on",
    9: "cleaning",
    10: "probe_sensitivity_changed",
    11: "remote_pump_refused",
    12: "downlink_ok",
    13: "nb_iot_reregistration",
}
UNKNOWN_ALARM = "unknown_alarm"
UNKNOWN_STATUS = "unknown_status"
PUMP_SPECIFIC_KINDS = frozenset({"pump_failure", "pump_damage_risk"})

ALARM_EVENT_TYPES = [*sorted(set(ALARM_KINDS.values())), UNKNOWN_ALARM]
STATUS_EVENT_TYPES = [*sorted(set(STATUS_KINDS.values())), UNKNOWN_STATUS]


def alarm_category(alarm_type: str) -> AlarmCategory:
    """Category of an alarm_type; anything that is not a status is treated as an alarm."""
    return AlarmCategory.STATUS if alarm_type == STATUS_TYPE else AlarmCategory.ALARM


def alarm_kind(alarm_type: str, alarm_number: int) -> str:
    """Kind for an (alarm_type, alarm_number) pair, or unknown_alarm/unknown_status."""
    if alarm_category(alarm_type) is AlarmCategory.STATUS:
        return STATUS_KINDS.get(alarm_number, UNKNOWN_STATUS)
    if alarm_type != ALARM_TYPE:
        return UNKNOWN_ALARM
    return ALARM_KINDS.get(alarm_number, UNKNOWN_ALARM)


def parse_datetime(value: Any) -> datetime | None:
    """Parse a NOM timestamp. NOM reads zone-less times as Prague local time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=NOM_TIMEZONE)


def parse_date(value: Any) -> date | None:
    """Parse a YYYY-MM-DD date (or the date part of a timestamp)."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class DeviceType:
    """Product type of a device (embedded in every device record)."""

    id: int | None
    name: str
    parse_method: str | None
    protocol: str | None
    pump_count: int
    has_downlink: bool
    order_name: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DeviceType:
        """Build from a device_type object."""
        return cls(
            id=_int(data.get("id")),
            name=str(data.get("name") or "Unknown"),
            parse_method=data.get("parse_method"),
            protocol=data.get("protocol"),
            pump_count=max(1, _int(data.get("pump_count")) or 1),
            has_downlink=bool(data.get("has_downlink")),
            order_name=data.get("order_servis_name"),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """A device on the NOM account."""

    id: int
    name: str
    serial_number: str | None
    device_type: DeviceType
    last_message_at: datetime | None
    last_event_id: int | None
    firmware: int | None
    software: int | None
    subscription_expires: date | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        """Build from a /devices item."""
        info = data.get("device_info") or {}
        token = data.get("token") or {}
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"Noria {data['id']}"),
            serial_number=data.get("serial_number") or None,
            device_type=DeviceType.from_api(data.get("device_type") or {}),
            last_message_at=parse_datetime(info.get("last_message_at")),
            last_event_id=_int(info.get("last_event_id")),
            firmware=_int(token.get("fw")),
            software=_int(token.get("sw")),
            subscription_expires=parse_date(token.get("token_expiration")),
            raw=data,
        )

    @property
    def is_tlakan(self) -> bool:
        """Whether this is a TLAKAN pump station controller (the family v0.1 supports)."""
        return self.device_type.parse_method in TLAKAN_PARSE_METHODS

    @property
    def pump_numbers(self) -> range:
        """Pump numbers on this controller (1-based)."""
        return range(1, self.device_type.pump_count + 1)


@dataclass(frozen=True, slots=True)
class Reading:
    """One TLAKAN reading (pump_reading) for one pump."""

    device_id: int
    pump_number: int
    timestamp: datetime
    total_time: int | None
    switch_count: int | None
    time_diff: int | None
    switch_diff: int | None
    phase_alarm_count: int | None
    alarm_count: int | None
    prob_sens: int | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Reading | None:
        """Build from a /readings item; None when it has no usable timestamp."""
        timestamp = parse_datetime(data.get("timestamp"))
        if timestamp is None or _int(data.get("device_id")) is None:
            return None
        return cls(
            device_id=int(data["device_id"]),
            pump_number=_int(data.get("pump_number")) or 1,
            timestamp=timestamp,
            total_time=_int(data.get("total_time")),
            switch_count=_int(data.get("switch_count")),
            time_diff=_int(data.get("time_diff")),
            switch_diff=_int(data.get("switch_diff")),
            phase_alarm_count=_int(data.get("phase_alarm_count")),
            alarm_count=_int(data.get("alarm_count")),
            prob_sens=_int(data.get("prob_sens")),
        )

    @property
    def key(self) -> tuple[int, int, datetime]:
        """Deduplication key; NOM returns the same reading under several ids."""
        return (self.device_id, self.pump_number, self.timestamp)


@dataclass(frozen=True, slots=True)
class Alarm:
    """One alarm or status record from /alarms, /alarms/active or /alarms/{id}."""

    id: int
    device_id: int
    alarm_type: str
    alarm_number: int
    code: str | None
    pump_number: int
    active: bool
    start: datetime | None
    end: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    system_notice: bool
    description: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Alarm:
        """Build from an alarm item."""
        alarm_type = str(data.get("alarm_type") or "")
        alarm_number = _int(data.get("alarm_number")) or 0
        pump_number = _int(data.get("pump_number"))
        if pump_number is None:
            # The spec allows a null pump_number; al1/al2 are "pump 1/2 failure" by definition.
            pump_number = alarm_number if alarm_type == ALARM_TYPE and alarm_number in (1, 2) else 1
        return cls(
            id=int(data["id"]),
            device_id=int(data["device_id"]),
            alarm_type=alarm_type,
            alarm_number=alarm_number,
            code=str(data["human_number"]) if data.get("human_number") is not None else None,
            pump_number=pump_number,
            active=bool(data.get("active")),
            start=parse_datetime(data.get("status_start")),
            end=parse_datetime(data.get("status_end")),
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            system_notice=bool(data.get("system_notice")),
            description=data.get("human_description"),
        )

    @property
    def category(self) -> AlarmCategory:
        """Alarm or status."""
        return alarm_category(self.alarm_type)

    @property
    def kind(self) -> str:
        """Translation-key style kind, e.g. pump_failure."""
        return alarm_kind(self.alarm_type, self.alarm_number)


@dataclass(frozen=True, slots=True)
class PumpTime:
    """Average or reference pumping time over a measurement window."""

    pump_number: int
    time_type: str
    seconds: float
    measured_from: datetime | None
    measured_to: datetime | None
    caused_alarm: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PumpTime | None:
        """Build from a /pump_times item; None when the time is missing."""
        seconds = data.get("time")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return None
        return cls(
            pump_number=_int(data.get("pump_number")) or 1,
            time_type=str(data.get("time_type") or ""),
            seconds=float(seconds),
            measured_from=parse_datetime(data.get("measured_from")),
            measured_to=parse_datetime(data.get("measured_to")),
            caused_alarm=bool(data.get("caused_alarm")),
        )


class CommandState(StrEnum):
    """Lifecycle of a command sent to a device (docs/design.md §9).

    QUEUED and SENT come from NOM's downlink history. SENT means NOM handed the command
    to the device in its next uplink window; it does not prove the device executed it.
    CONFIRMED needs a device-originated signal that the action physically happened. No
    such signal is known, so nothing in this integration produces CONFIRMED.
    """

    QUEUED = "queued"
    SENT = "sent"
    CONFIRMED = "confirmed"


# Downlink payloads up to 4 bytes are command codes (9101 horn on, 9100 horn off, FFFF);
# longer ones can embed URLs or identifiers and are never exposed.
MAX_EXPOSED_PAYLOAD_HEX = 8


@dataclass(frozen=True, slots=True)
class DownlinkEvent:
    """One row of NOM's downlink history (a command queued for a device)."""

    id: int
    device_id: int
    label: str | None
    data: str | None
    sent: bool
    sent_at: datetime | None
    created_at: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DownlinkEvent:
        """Build from a /downlink_events item."""
        return cls(
            id=int(data["id"]),
            device_id=int(data["device_id"]),
            label=data.get("event_type"),
            data=data.get("data"),
            sent=bool(data.get("sent")),
            sent_at=parse_datetime(data.get("sent_at")),
            created_at=parse_datetime(data.get("created_at")),
        )

    @property
    def state(self) -> CommandState:
        """QUEUED or SENT. Never CONFIRMED: downlink history carries no device acknowledgement."""
        return CommandState.SENT if self.sent else CommandState.QUEUED

    @property
    def safe_data(self) -> str | None:
        """Payload if it is a short command code, otherwise only its size."""
        if self.data is None or len(self.data) <= MAX_EXPOSED_PAYLOAD_HEX:
            return self.data
        return f"<{len(self.data) // 2} bytes>"

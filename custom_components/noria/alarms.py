"""Reconcile NOM alarm records across polls (docs/design.md §3).

NOM facts this relies on:
- /alarms `from`/`to` filter on created_at, so a history window shows an alarm's end
  only if the alarm was also created inside that window;
- /alarms/active is authoritative for what is open right now;
- /alarms/{id} returns the current state of one record.

Every alarm yields exactly one `started` event and at most one `ended` event over its
lifetime; every status yields one `occurred` event. Free of Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import logging
from typing import Any

from .models import Alarm, AlarmCategory, alarm_category, alarm_kind, parse_datetime

_LOGGER = logging.getLogger(__name__)

# History windows overlap by this much, absorbing creation lag and server batch alarms.
OVERLAP = timedelta(hours=2)
# After a restart, records created up to this long before startup still fire (delayed).
REPLAY_WINDOW = timedelta(hours=6)
# Ended records are forgotten once they are this far behind the synced boundary.
PRUNE_AFTER = 2 * OVERLAP
# Cap on single-alarm lookups per device and update.
MAX_LOOKUPS = 10
# An event reported more than this long after it happened is marked `delayed`
# (normal latency is one poll interval plus NOM's ~2 min ingest lag).
DELAY_THRESHOLD = timedelta(minutes=15)


class Phase(StrEnum):
    """Which transition an event reports."""

    STARTED = "started"
    ENDED = "ended"
    OCCURRED = "occurred"


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    """Something that happened to an alarm or status, ready to fire as an HA event."""

    device_id: int
    category: AlarmCategory
    kind: str
    phase: Phase
    attributes: dict[str, Any]
    # (when it happened, started-before-ended, created_at, id): events fire oldest first.
    sort_key: tuple[datetime, int, datetime, int] = field(compare=False, repr=False)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True)
class TrackedAlarm:
    """What the tracker remembers about one alarm/status id."""

    id: int
    alarm_type: str
    alarm_number: int
    code: str | None
    pump_number: int
    created_at: datetime | None
    start: datetime | None
    end: datetime | None
    open: bool = False  # present in the last successful active list
    pending: bool = False  # missing from a successful active list, end not known yet
    ended_announced: bool = False
    lookups: int = 0  # /alarms/{id} attempts, used to rotate lookups fairly

    @classmethod
    def from_alarm(cls, alarm: Alarm) -> TrackedAlarm:
        """Start tracking an API record. An end on a record NOM calls active is not trusted."""
        return cls(
            id=alarm.id,
            alarm_type=alarm.alarm_type,
            alarm_number=alarm.alarm_number,
            code=alarm.code,
            pump_number=alarm.pump_number,
            created_at=alarm.created_at,
            start=alarm.start,
            end=None if alarm.active else alarm.end,
        )

    @property
    def category(self) -> AlarmCategory:
        """Alarm or status."""
        return alarm_category(self.alarm_type)

    @property
    def kind(self) -> str:
        """Kind such as pump_failure."""
        return alarm_kind(self.alarm_type, self.alarm_number)

    def refresh(self, alarm: Alarm) -> None:
        """Take start/end from a newer copy of the same record."""
        self.start = alarm.start or self.start
        self.created_at = alarm.created_at or self.created_at
        if alarm.end is not None and not alarm.active:
            self.end = alarm.end

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the Store."""
        return {
            "id": self.id,
            "alarm_type": self.alarm_type,
            "alarm_number": self.alarm_number,
            "code": self.code,
            "pump_number": self.pump_number,
            "created_at": _iso(self.created_at),
            "start": _iso(self.start),
            "end": _iso(self.end),
            "open": self.open,
            "pending": self.pending,
            "ended_announced": self.ended_announced,
            "lookups": self.lookups,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrackedAlarm:
        """Restore from the Store."""
        return cls(
            id=int(data["id"]),
            alarm_type=str(data["alarm_type"]),
            alarm_number=int(data["alarm_number"]),
            code=data.get("code"),
            pump_number=int(data.get("pump_number") or 1),
            created_at=parse_datetime(data.get("created_at")),
            start=parse_datetime(data.get("start")),
            end=parse_datetime(data.get("end")),
            open=bool(data.get("open")),
            pending=bool(data.get("pending")),
            ended_announced=bool(data.get("ended_announced")),
            lookups=int(data.get("lookups") or 0),
        )


@dataclass(slots=True)
class _DeviceState:
    synced_until: datetime | None
    records: dict[int, TrackedAlarm]


@dataclass(slots=True)
class _Poll:
    """Context of one update() call for one device."""

    device_id: int
    state: _DeviceState
    seeding: bool
    now: datetime
    events: list[AlarmEvent]


class AlarmTracker:
    """Per-account alarm reconciliation state."""

    def __init__(self, *, started_at: datetime, devices: dict[int, _DeviceState] | None = None) -> None:
        """`started_at` is when this tracker began watching (HA start), used for restart replay."""
        self._started_at = started_at
        self._devices: dict[int, _DeviceState] = devices or {}
        self._unknown_logged: set[tuple[str, int]] = set()

    # --- persistence --------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the Store."""
        return {
            "devices": {
                str(device_id): {
                    "synced_until": _iso(state.synced_until),
                    "records": [record.as_dict() for record in state.records.values()],
                }
                for device_id, state in self._devices.items()
            }
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *, started_at: datetime) -> AlarmTracker:
        """Restore from the Store; tolerates missing or partial data."""
        devices: dict[int, _DeviceState] = {}
        for device_id, state in ((data or {}).get("devices") or {}).items():
            records = {}
            for item in state.get("records") or []:
                record = TrackedAlarm.from_dict(item)
                records[record.id] = record
            devices[int(device_id)] = _DeviceState(parse_datetime(state.get("synced_until")), records)
        return cls(started_at=started_at, devices=devices)

    def forget_devices(self, keep: Iterable[int]) -> None:
        """Drop state of devices that are no longer on the account."""
        keep = set(keep)
        for device_id in [d for d in self._devices if d not in keep]:
            del self._devices[device_id]

    # --- queries for the coordinator ------------------------------------------

    def window_start(self, device_id: int, now: datetime) -> datetime:
        """`from` for the next history fetch: overlap the last synced point, bounded after downtime."""
        state = self._devices.get(device_id)
        if state is None or state.synced_until is None:
            return now - OVERLAP
        return max(state.synced_until - OVERLAP, now - REPLAY_WINDOW - OVERLAP)

    def ids_to_look_up(
        self, device_id: int, active: Iterable[Alarm] | None, history: Iterable[Alarm] | None
    ) -> list[int]:
        """Alarms whose end must be fetched with /alarms/{id}, least-tried first.

        With a successful active fetch: every alarm without a known end that is missing
        from the active list, whether or not it was ever seen open there. With a failed
        one (None): only alarms already pending, since absence from a failed fetch means
        nothing. Alarms whose end the history window already shows are skipped.
        """
        state = self._devices.get(device_id)
        if state is None:
            return []
        active_ids = None if active is None else {alarm.id for alarm in active}
        ended_in_history = {alarm.id for alarm in history or () if alarm.end is not None and not alarm.active}
        candidates = [
            record
            for record in state.records.values()
            if record.category is AlarmCategory.ALARM
            and record.end is None
            and record.id not in ended_in_history
            and (record.pending if active_ids is None else record.id not in active_ids)
        ]
        candidates.sort(key=lambda record: (record.lookups, record.id))
        return [record.id for record in candidates[:MAX_LOOKUPS]]

    def open_alarms(self, device_id: int) -> list[TrackedAlarm]:
        """Return the records the tracker currently considers open."""
        state = self._devices.get(device_id)
        return [r for r in state.records.values() if r.open] if state else []

    # --- reconciliation -------------------------------------------------------

    def update(
        self,
        device_id: int,
        *,
        now: datetime,
        active: Iterable[Alarm] | None,
        history: Iterable[Alarm] | None,
        lookups: Mapping[int, Alarm] | None = None,
        attempted: Iterable[int] = (),
        missing: Iterable[int] = (),
    ) -> list[AlarmEvent]:
        """Merge one poll's data and return the events it produces, oldest first.

        `active` / `history` are None when that fetch failed. `attempted` lists the ids
        from ids_to_look_up() that were fetched, `lookups` their successful results and
        `missing` those NOM answered 404 for (the record no longer exists).
        """
        state = self._devices.get(device_id)
        if state is None:
            state = self._devices[device_id] = _DeviceState(None, {})
        # Until one history fetch has succeeded, everything seen is existing state, not news.
        poll = _Poll(device_id, state, seeding=state.synced_until is None, now=now, events=[])
        self._merge_history(poll, history or ())
        if active is not None:
            self._merge_active(poll, active)
        self._apply_lookups(state, lookups or {}, attempted, missing)
        self._announce_ends(poll)
        if history is not None:
            state.synced_until = now
            self._prune(state)
        # NOM lists history newest first; an event entity must end up showing the newest event.
        poll.events.sort(key=lambda event: event.sort_key)
        return poll.events

    def _track(self, poll: _Poll, alarm: Alarm) -> TrackedAlarm:
        """Return the record for an API alarm, creating (and announcing) it when new."""
        record = poll.state.records.get(alarm.id)
        if record is None:
            record = poll.state.records[alarm.id] = TrackedAlarm.from_alarm(alarm)
            self._log_if_unknown(record)
            self._announce_new(poll, record)
        return record

    def _merge_history(self, poll: _Poll, history: Iterable[Alarm]) -> None:
        """Track new records (possibly already ended) and ends of recently created ones."""
        for alarm in history:
            known = alarm.id in poll.state.records
            record = self._track(poll, alarm)
            if known:
                record.refresh(alarm)

    def _merge_active(self, poll: _Poll, active: Iterable[Alarm]) -> None:
        """Apply the active list: listed ids are open; any other alarm without an end is pending."""
        active_ids = set()
        for alarm in active:
            active_ids.add(alarm.id)
            record = self._track(poll, alarm)
            if record.category is AlarmCategory.ALARM and record.ended_announced:
                # Listed as active after its end was taken as known (e.g. it ended between the
                # active and the history fetch of the seeding poll): its real end is still to come.
                record.ended_announced = False
            record.open, record.pending, record.end = True, False, None
        for record in poll.state.records.values():
            if record.id in active_ids:
                continue
            record.open = False
            if record.category is AlarmCategory.ALARM and record.end is None:
                record.pending = True

    @staticmethod
    def _apply_lookups(
        state: _DeviceState, lookups: Mapping[int, Alarm], attempted: Iterable[int], missing: Iterable[int]
    ) -> None:
        """Apply lookups: ends of alarms that left the active list after their creation window."""
        for alarm_id in attempted:
            if (record := state.records.get(alarm_id)) is not None:
                record.lookups += 1
        for alarm_id, alarm in lookups.items():
            record = state.records.get(alarm_id)
            if record is not None and not record.open:
                record.refresh(alarm)
        for alarm_id in missing:
            record = state.records.get(alarm_id)
            if record is not None and not record.open:
                # Gone from NOM: its end can't be learned. The binary sensors follow the active
                # list, so nothing is left on; stop looking it up.
                _LOGGER.debug("NOM alarm %s no longer exists; dropping it", alarm_id)
                del state.records[alarm_id]

    def _announce_ends(self, poll: _Poll) -> None:
        """Clear pending for every known end and announce each end exactly once."""
        for record in poll.state.records.values():
            if record.category is not AlarmCategory.ALARM or record.end is None or record.open:
                continue
            record.pending = False
            if not record.ended_announced:
                record.ended_announced = True
                poll.events.append(self._event(poll, record, Phase.ENDED, record.end))

    def _announce_new(self, poll: _Poll, record: TrackedAlarm) -> None:
        """Fire for a newly seen record, unless seeding or too old to replay."""
        too_old = record.created_at is not None and record.created_at < self._started_at - REPLAY_WINDOW
        if poll.seeding or too_old:
            # Track silently. An open alarm still gets its `ended` event later.
            record.ended_announced = record.end is not None or record.category is AlarmCategory.STATUS
            return
        if record.category is AlarmCategory.STATUS:
            record.ended_announced = True
            poll.events.append(self._event(poll, record, Phase.OCCURRED, record.start or record.created_at))
        else:
            poll.events.append(self._event(poll, record, Phase.STARTED, record.created_at or record.start))

    def _log_if_unknown(self, record: TrackedAlarm) -> None:
        key = (record.alarm_type, record.alarm_number)
        if record.kind.startswith("unknown_") and key not in self._unknown_logged:
            self._unknown_logged.add(key)
            _LOGGER.warning(
                "Unknown NOM alarm code %s (alarm_type %s, alarm_number %s); reported as %s",
                record.code,
                record.alarm_type,
                record.alarm_number,
                record.kind,
            )

    @staticmethod
    def _event(poll: _Poll, record: TrackedAlarm, phase: Phase, happened: datetime | None) -> AlarmEvent:
        attributes: dict[str, Any] = {
            "code": record.code,
            "alarm_id": record.id,
            "pump_number": record.pump_number,
            "delayed": happened is not None and poll.now - happened > DELAY_THRESHOLD,
        }
        if phase is Phase.OCCURRED:
            attributes["occurred"] = _iso(record.start or record.created_at)
        else:
            attributes["phase"] = phase.value
            attributes["started"] = _iso(record.start)
            attributes["ended"] = _iso(record.end)
            attributes["duration_s"] = (
                int((record.end - record.start).total_seconds()) if record.start and record.end else None
            )
        # Order by device time. An end never sorts before its own start, even when NOM created
        # the record (created_at) after the alarm had already ended.
        at = record.start or record.created_at or poll.now
        if phase is Phase.ENDED and record.end is not None:
            at = max(at, record.end)
        sort_key = (at, 1 if phase is Phase.ENDED else 0, record.created_at or at, record.id)
        return AlarmEvent(poll.device_id, record.category, record.kind, phase, attributes, sort_key)

    @staticmethod
    def _prune(state: _DeviceState) -> None:
        if state.synced_until is None:
            return
        horizon = state.synced_until - PRUNE_AFTER
        for alarm_id in [
            record.id
            for record in state.records.values()
            if record.ended_announced
            and not record.open
            and not record.pending
            and record.created_at is not None
            and record.created_at < horizon
        ]:
            del state.records[alarm_id]

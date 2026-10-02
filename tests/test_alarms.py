"""Alarm reconciliation across polls (custom_components/noria/alarms.py, design §3).

A fake NOM serves alarms the way the live API does: /alarms/active lists open alarms,
/alarms filters on created_at (inclusive), /alarms/{id} returns the current record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from custom_components.noria.alarms import MAX_LOOKUPS, OVERLAP, REPLAY_WINDOW, AlarmEvent, AlarmTracker, Phase
from custom_components.noria.models import Alarm, AlarmCategory

DEVICE = 3040
T0 = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
POLL = timedelta(minutes=5)


def make_alarm(
    alarm_id: int,
    *,
    created: datetime,
    number: int = 3,
    alarm_type: str = "tlakan_alarm",
    start: datetime | None = None,
    end: datetime | None = None,
    pump: int = 1,
    active: bool | None = None,
) -> Alarm:
    """An API record; active while end is None unless given. Statuses are instantaneous."""
    start = start or created - timedelta(seconds=15)
    if alarm_type == "tlakan_status":
        end = end or start
    prefix = "9" if alarm_type == "tlakan_status" else "1"
    return Alarm.from_api(
        {
            "id": alarm_id,
            "device_id": DEVICE,
            "alarm_type": alarm_type,
            "alarm_number": number,
            "human_number": f"{prefix}{number:02d}",
            "pump_number": pump,
            "active": end is None if active is None else active,
            "status_start": start.isoformat(),
            "status_end": end.isoformat() if end else None,
            "created_at": created.isoformat(),
            "updated_at": (end or created).isoformat(),
            "system_notice": False,
            "human_description": "test",
        }
    )


class FakeNom:
    """NOM alarm endpoints over an in-memory set of records."""

    def __init__(self) -> None:
        self.alarms: dict[int, Alarm] = {}
        self.deleted: set[int] = set()  # ids /alarms/{id} answers 404 for

    def put(self, alarm: Alarm) -> None:
        self.alarms[alarm.id] = alarm

    def active(self) -> list[Alarm]:
        return [a for a in self.alarms.values() if a.active]

    def history(self, since: datetime) -> list[Alarm]:
        """Created at or after `since`, newest first, like the real endpoint."""
        window = [a for a in self.alarms.values() if a.created_at and a.created_at >= since]
        return sorted(window, key=lambda a: (a.created_at, a.id), reverse=True)


def poll(
    tracker: AlarmTracker,
    nom: FakeNom,
    now: datetime,
    *,
    active_ok: bool = True,
    history_ok: bool = True,
    lookups_ok: bool = True,
    active_override: list[Alarm] | None = None,
) -> tuple[list[AlarmEvent], list[int]]:
    """One coordinator update: fetch, look up ends, reconcile. Returns (events, looked-up ids)."""
    active = (active_override if active_override is not None else nom.active()) if active_ok else None
    history = nom.history(tracker.window_start(DEVICE, now)) if history_ok else None
    ids = tracker.ids_to_look_up(DEVICE, active, history)
    missing = {i for i in ids if i in nom.deleted}
    lookups = {i: nom.alarms[i] for i in ids if i not in missing} if lookups_ok else {}
    events = tracker.update(
        DEVICE, now=now, active=active, history=history, lookups=lookups, attempted=ids, missing=missing
    )
    return events, ids


def summary(events: list[AlarmEvent]) -> list[tuple[str, Phase]]:
    return [(event.kind, event.phase) for event in events]


def running_tracker(nom: FakeNom, now: datetime = T0) -> AlarmTracker:
    """A tracker that started an hour ago and has finished its silent seeding poll."""
    tracker = AlarmTracker(started_at=now - timedelta(hours=1))
    events, _ = poll(tracker, nom, now)
    assert events == []
    return tracker


def record_ids(tracker: AlarmTracker) -> set[int]:
    return {r["id"] for r in tracker.as_dict()["devices"][str(DEVICE)]["records"]}


def test_first_poll_seeds_silently_but_still_tracks_open_alarms() -> None:
    nom = FakeNom()
    nom.put(make_alarm(1, created=T0 - timedelta(minutes=30), end=T0 - timedelta(minutes=20)))
    nom.put(make_alarm(2, created=T0 - timedelta(minutes=10)))
    nom.put(make_alarm(3, number=9, alarm_type="tlakan_status", created=T0 - timedelta(minutes=5)))
    tracker = AlarmTracker(started_at=T0)

    events, _ = poll(tracker, nom, T0)
    assert events == []

    nom.put(make_alarm(2, created=T0 - timedelta(minutes=10), end=T0 + timedelta(minutes=2)))
    events, _ = poll(tracker, nom, T0 + POLL)
    assert summary(events) == [("pump_failure", Phase.ENDED)]
    assert events[0].attributes["delayed"] is False


def test_seeding_continues_until_history_succeeds() -> None:
    nom = FakeNom()
    nom.put(make_alarm(1, created=T0 - timedelta(minutes=30), end=T0 - timedelta(minutes=29)))
    tracker = AlarmTracker(started_at=T0)
    assert poll(tracker, nom, T0, history_ok=False)[0] == []
    # The first successful history fetch is still existing state, not news.
    assert poll(tracker, nom, T0 + POLL)[0] == []


def test_alarm_seen_active_then_ended_inside_its_creation_window() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=3)
    nom.put(make_alarm(10, created=created))

    events, _ = poll(tracker, nom, T0 + POLL)
    assert summary(events) == [("pump_failure", Phase.STARTED)]
    assert events[0].attributes["ended"] is None
    assert events[0].attributes["delayed"] is False

    nom.put(make_alarm(10, created=created, end=T0 + timedelta(minutes=8)))
    events, looked_up = poll(tracker, nom, T0 + 2 * POLL)
    assert looked_up == []  # the history window still shows the end
    assert summary(events) == [("pump_failure", Phase.ENDED)]
    assert events[0].attributes["duration_s"] == 315


def test_alarm_never_observed_active() -> None:
    """Starts and ends between two polls: the active list never shows it, history shows it ended."""
    nom = FakeNom()
    tracker = running_tracker(nom)
    start, end = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    nom.put(make_alarm(20, number=6, created=start, start=start, end=end))

    events, looked_up = poll(tracker, nom, T0 + POLL)
    assert looked_up == []
    assert summary(events) == [("emergency_level", Phase.STARTED), ("emergency_level", Phase.ENDED)]
    assert all(event.attributes["ended"] == end.isoformat() for event in events)
    assert events[1].attributes["duration_s"] == 60
    assert tracker.open_alarms(DEVICE) == []

    # The next window overlaps and returns it again: nothing new.
    assert poll(tracker, nom, T0 + 2 * POLL)[0] == []


def test_end_set_after_the_creation_window_is_learned_by_lookup() -> None:
    """A long alarm ends hours later, when no history window (created_at filter) includes it."""
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=2)
    nom.put(make_alarm(30, created=created))
    now = T0 + POLL
    assert summary(poll(tracker, nom, now)[0]) == [("pump_failure", Phase.STARTED)]

    while now < created + 3 * OVERLAP:
        now += POLL
        events, looked_up = poll(tracker, nom, now)
        assert events == []
        assert looked_up == []
    assert tracker.window_start(DEVICE, now) > created
    assert 30 in {r.id for r in tracker.open_alarms(DEVICE)}

    end = now + timedelta(minutes=1)
    nom.put(make_alarm(30, created=created, end=end))
    now += POLL
    events, looked_up = poll(tracker, nom, now)
    assert looked_up == [30]
    assert summary(events) == [("pump_failure", Phase.ENDED)]
    assert events[0].attributes["ended"] == end.isoformat()
    assert events[0].attributes["delayed"] is False

    # Announced once only.
    assert poll(tracker, nom, now + POLL) == ([], [])


def test_failed_lookup_is_retried_next_poll() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 - timedelta(hours=5)  # outside every history window: known only via the active list
    nom.put(make_alarm(31, created=created))
    events, _ = poll(tracker, nom, T0 + POLL)
    assert summary(events) == [("pump_failure", Phase.STARTED)]
    assert events[0].attributes["delayed"] is True  # created before the tracker started

    nom.put(make_alarm(31, created=created, end=T0 + timedelta(minutes=7)))
    events, looked_up = poll(tracker, nom, T0 + 2 * POLL, lookups_ok=False)
    assert looked_up == [31]
    assert events == []

    events, looked_up = poll(tracker, nom, T0 + 3 * POLL)
    assert looked_up == [31]
    assert summary(events) == [("pump_failure", Phase.ENDED)]


def test_lookup_reporting_still_active_keeps_alarm_open_through_list_lag() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=1)
    nom.put(make_alarm(32, created=created))
    poll(tracker, nom, T0 + POLL)

    # The active list briefly misses it while the record itself is still active.
    events, looked_up = poll(tracker, nom, T0 + 2 * POLL, active_override=[])
    assert looked_up == [32]
    assert events == []
    events, looked_up = poll(tracker, nom, T0 + 3 * POLL)
    assert looked_up == []
    assert events == []
    assert 32 in {r.id for r in tracker.open_alarms(DEVICE)}


def test_failed_active_fetch_marks_nothing_pending() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(33, created=T0 + timedelta(minutes=1)))
    poll(tracker, nom, T0 + POLL)

    events, looked_up = poll(tracker, nom, T0 + 2 * POLL, active_ok=False)
    assert events == []
    assert looked_up == []
    assert 33 in {r.id for r in tracker.open_alarms(DEVICE)}


def test_failed_history_still_processes_the_active_list() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=1)
    nom.put(make_alarm(34, number=11, created=created))
    events, _ = poll(tracker, nom, T0 + POLL, history_ok=False)
    assert summary(events) == [("pump_damage_risk", Phase.STARTED)]

    nom.put(make_alarm(34, number=11, created=created, end=T0 + timedelta(minutes=9)))
    events, looked_up = poll(tracker, nom, T0 + 2 * POLL, history_ok=False)
    assert looked_up == [34]
    assert summary(events) == [("pump_damage_risk", Phase.ENDED)]


def test_alarm_and_status_sharing_a_number_are_distinct() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(40, number=3, created=T0 + timedelta(minutes=1)))
    nom.put(make_alarm(41, number=3, alarm_type="tlakan_status", created=T0 + timedelta(minutes=2)))

    events, _ = poll(tracker, nom, T0 + POLL)
    by_id = {event.attributes["alarm_id"]: event for event in events}
    assert (by_id[40].category, by_id[40].kind, by_id[40].phase) == (
        AlarmCategory.ALARM,
        "pump_failure",
        Phase.STARTED,
    )
    assert (by_id[41].category, by_id[41].kind, by_id[41].phase) == (
        AlarmCategory.STATUS,
        "alarm_counters_reset",
        Phase.OCCURRED,
    )
    assert by_id[41].attributes["code"] == "903"


def test_status_fires_once_across_overlapping_windows() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(42, number=9, alarm_type="tlakan_status", created=T0 + timedelta(minutes=1)))
    assert summary(poll(tracker, nom, T0 + POLL)[0]) == [("cleaning", Phase.OCCURRED)]
    for step in range(2, 10):
        assert poll(tracker, nom, T0 + step * POLL)[0] == []


def test_unknown_codes_map_to_unknown_kinds() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(43, number=42, created=T0 + timedelta(minutes=1)))
    nom.put(make_alarm(44, number=77, alarm_type="tlakan_status", created=T0 + timedelta(minutes=1)))
    kinds = {event.kind for event in poll(tracker, nom, T0 + POLL)[0]}
    assert kinds == {"unknown_alarm", "unknown_status"}


def test_restart_replays_recent_records_as_delayed_and_reconciles_open_ones() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    long_alarm_created = T0 + timedelta(minutes=1)
    nom.put(make_alarm(50, created=long_alarm_created))
    poll(tracker, nom, T0 + POLL)
    stored = json.loads(json.dumps(tracker.as_dict()))  # what the Store would hold

    # HA is down from T0+5min until T0+10h. Meanwhile:
    restart = T0 + timedelta(hours=10)
    nom.put(make_alarm(50, created=long_alarm_created, end=T0 + timedelta(hours=2)))  # ended during outage
    old = restart - REPLAY_WINDOW - timedelta(hours=1)
    nom.put(make_alarm(51, created=old, end=old + timedelta(minutes=1)))  # too old to replay
    recent = restart - timedelta(hours=1)
    nom.put(make_alarm(52, number=6, created=recent, end=recent + timedelta(minutes=1)))
    nom.put(make_alarm(53, number=1, alarm_type="tlakan_status", created=restart - timedelta(minutes=30)))

    restored = AlarmTracker.from_dict(stored, started_at=restart)
    events, looked_up = poll(restored, nom, restart)
    assert looked_up == [50]
    emitted = [(e.attributes["alarm_id"], e.phase) for e in events]
    assert set(emitted) == {(50, Phase.ENDED), (52, Phase.STARTED), (52, Phase.ENDED), (53, Phase.OCCURRED)}
    assert len(emitted) == 4
    assert emitted.index((52, Phase.STARTED)) < emitted.index((52, Phase.ENDED))
    assert all(event.attributes["delayed"] is True for event in events)


def test_old_ended_records_are_pruned_but_open_ones_kept() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(60, created=T0 + timedelta(minutes=1), end=T0 + timedelta(minutes=2)))
    nom.put(make_alarm(61, created=T0 + timedelta(minutes=1)))
    poll(tracker, nom, T0 + POLL)
    assert record_ids(tracker) == {60, 61}

    poll(tracker, nom, T0 + timedelta(hours=5))
    assert record_ids(tracker) == {61}


def test_window_start_overlaps_and_is_bounded_after_downtime() -> None:
    nom = FakeNom()
    tracker = AlarmTracker(started_at=T0)
    assert tracker.window_start(DEVICE, T0) == T0 - OVERLAP  # seeding window
    poll(tracker, nom, T0)
    assert tracker.window_start(DEVICE, T0 + POLL) == T0 - OVERLAP
    later = T0 + timedelta(days=1)
    assert tracker.window_start(DEVICE, later) == later - REPLAY_WINDOW - OVERLAP


def test_per_pump_alarms_keep_their_pump_number() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(70, number=1, pump=1, created=T0 + timedelta(minutes=1)))
    nom.put(make_alarm(71, number=2, pump=2, created=T0 + timedelta(minutes=1)))
    events = poll(tracker, nom, T0 + POLL)[0]
    assert sorted((e.kind, e.attributes["pump_number"]) for e in events) == [
        ("pump_failure", 1),
        ("pump_failure", 2),
    ]


# --- regressions from review ---------------------------------------------------


def test_active_alarm_carrying_an_end_is_still_open() -> None:
    """Open question Q2: NOM might fill status_end on active alarms. Active wins."""
    nom = FakeNom()
    created = T0 - timedelta(minutes=20)
    nom.put(make_alarm(80, created=created, end=T0 - timedelta(minutes=19), active=True))
    tracker = AlarmTracker(started_at=T0)
    assert poll(tracker, nom, T0)[0] == []  # seeding
    assert 80 in {r.id for r in tracker.open_alarms(DEVICE)}

    now = T0
    while now < created + 3 * OVERLAP:  # still active, with its odd end, for hours
        now += POLL
        assert poll(tracker, nom, now) == ([], [])
    real_end = now - timedelta(minutes=2)
    nom.put(make_alarm(80, created=created, end=real_end, active=False))
    now += POLL
    events, looked_up = poll(tracker, nom, now)
    assert looked_up == [80]
    assert summary(events) == [("pump_failure", Phase.ENDED)]
    assert events[0].attributes["ended"] == real_end.isoformat()
    assert 80 not in record_ids(tracker)  # ended and old: pruned, not stuck pending forever


def test_new_history_row_marked_active_with_an_end_does_not_fire_ended() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(81, created=T0 + timedelta(minutes=1), end=T0 + timedelta(minutes=2), active=True))
    events, _ = poll(tracker, nom, T0 + POLL, active_ok=False)
    assert summary(events) == [("pump_failure", Phase.STARTED)]


def test_seeding_race_alarm_ends_between_active_and_history_fetch() -> None:
    """The seeding poll's active list shows it open; history (fetched after) already shows it ended."""
    nom = FakeNom()
    created = T0 - timedelta(minutes=10)
    open_copy = make_alarm(82, created=created)
    nom.put(make_alarm(82, created=created, end=T0 - timedelta(seconds=10)))
    tracker = AlarmTracker(started_at=T0)
    assert poll(tracker, nom, T0, active_override=[open_copy])[0] == []

    events, _ = poll(tracker, nom, T0 + POLL)
    assert summary(events) == [("pump_failure", Phase.ENDED)]


def test_alarm_known_only_from_history_while_active_fetch_fails_for_hours() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=1)
    nom.put(make_alarm(83, number=11, created=created))
    now = T0 + POLL
    assert summary(poll(tracker, nom, now, active_ok=False)[0]) == [("pump_damage_risk", Phase.STARTED)]
    while now < created + 3 * OVERLAP:  # active list unavailable the whole time
        now += POLL
        assert poll(tracker, nom, now, active_ok=False) == ([], [])

    nom.put(make_alarm(83, number=11, created=created, end=now - timedelta(minutes=30)))
    now += POLL
    events, looked_up = poll(tracker, nom, now)  # active list works again
    assert looked_up == [83]
    assert summary(events) == [("pump_damage_risk", Phase.ENDED)]


def test_lookup_404_drops_the_record() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=1)
    nom.put(make_alarm(84, created=created))
    poll(tracker, nom, T0 + POLL)
    now = T0 + 3 * OVERLAP
    poll(tracker, nom, now)  # outside its creation window by now
    del nom.alarms[84]
    nom.deleted.add(84)
    events, looked_up = poll(tracker, nom, now + POLL)
    assert looked_up == [84]
    assert events == []
    assert 84 not in record_ids(tracker)
    assert poll(tracker, nom, now + 2 * POLL) == ([], [])


def test_lookups_rotate_when_more_are_pending_than_the_cap() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    ids = list(range(100, 100 + MAX_LOOKUPS + 5))
    for alarm_id in ids:
        nom.put(make_alarm(alarm_id, created=T0 + timedelta(minutes=1)))
    poll(tracker, nom, T0 + POLL)
    for alarm_id in ids:  # all leave the active list without a known end
        nom.alarms[alarm_id] = make_alarm(alarm_id, created=T0 + timedelta(minutes=1), active=False)
    now = T0 + 3 * OVERLAP
    first = poll(tracker, nom, now, lookups_ok=False)[1]
    second = poll(tracker, nom, now + POLL, lookups_ok=False)[1]
    assert len(first) == MAX_LOOKUPS
    assert set(ids) - set(first) <= set(second)  # the ones skipped first come next


def test_alarms_reported_late_after_a_nom_outage_are_delayed() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    created = T0 + timedelta(minutes=10)
    nom.put(make_alarm(85, number=6, created=created, end=created + timedelta(minutes=1)))
    for step in range(1, 37):  # three hours without NOM, no HA restart
        assert poll(tracker, nom, T0 + step * POLL, active_ok=False, history_ok=False)[0] == []
    events = poll(tracker, nom, T0 + timedelta(hours=3, minutes=5))[0]
    assert summary(events) == [("emergency_level", Phase.STARTED), ("emergency_level", Phase.ENDED)]
    assert all(event.attributes["delayed"] for event in events)


def test_unknown_codes_are_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(86, number=42, created=T0 + timedelta(minutes=1)))
    nom.put(make_alarm(87, number=42, created=T0 + timedelta(minutes=2)))
    poll(tracker, nom, T0 + POLL)
    assert caplog.text.count("Unknown NOM alarm code 142") == 1


def test_devices_removed_from_the_account_are_forgotten() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    tracker.forget_devices([])
    assert tracker.as_dict() == {"devices": {}}


def test_events_come_out_oldest_first_although_history_is_newest_first() -> None:
    """Two statuses in one window: the event entity must end up showing the newer one."""
    nom = FakeNom()
    tracker = running_tracker(nom)
    nom.put(make_alarm(90, number=1, alarm_type="tlakan_status", created=T0 + timedelta(minutes=1)))
    nom.put(make_alarm(91, number=13, alarm_type="tlakan_status", created=T0 + timedelta(minutes=2)))
    assert [a.id for a in nom.history(T0)] == [91, 90]  # API order: newest first

    events = poll(tracker, nom, T0 + POLL)[0]
    assert [(e.kind, e.attributes["alarm_id"]) for e in events] == [
        ("power_connected", 90),
        ("nb_iot_reregistration", 91),
    ]


def test_mixed_alarms_and_statuses_are_ordered_by_device_time() -> None:
    nom = FakeNom()
    tracker = running_tracker(nom)
    t = T0 + timedelta(minutes=1)
    nom.put(make_alarm(92, number=6, created=t, start=t, end=t + timedelta(minutes=2)))  # 60 s+ alarm
    nom.put(make_alarm(93, number=9, alarm_type="tlakan_status", created=t + timedelta(minutes=1)))
    events = poll(tracker, nom, T0 + POLL)[0]
    assert [(e.attributes["alarm_id"], e.phase) for e in events] == [
        (92, Phase.STARTED),
        (93, Phase.OCCURRED),
        (92, Phase.ENDED),
    ]


def test_end_never_sorts_before_its_start_when_created_late() -> None:
    """NOM created the record 10 minutes after the alarm had already ended."""
    nom = FakeNom()
    tracker = running_tracker(nom)
    start = T0 + timedelta(minutes=1)
    end = start + timedelta(minutes=1)
    nom.put(make_alarm(94, number=7, created=end + timedelta(minutes=10), start=start, end=end))
    events = poll(tracker, nom, T0 + timedelta(minutes=20))[0]
    assert summary(events) == [("probe_contamination", Phase.STARTED), ("probe_contamination", Phase.ENDED)]

"""Data update coordinator for Noria Online Monitoring (docs/design.md §2, §7)."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .alarms import AlarmEvent, AlarmTracker
from .api import (
    NoriaAuthError,
    NoriaClient,
    NoriaError,
    NoriaForbiddenError,
    NoriaNotFoundError,
    NoriaRateLimitError,
)
from .const import DOMAIN, LOGGER, PUMP_TIMES_INTERVAL, STORAGE_VERSION, STORE_SAVE_DELAY, UPDATE_INTERVAL
from .models import Alarm, Device, DownlinkEvent, PumpTime, Reading

type NoriaConfigEntry = ConfigEntry[NoriaCoordinator]


@dataclass(slots=True)
class DeviceData:
    """Everything known about one device after an update."""

    device: Device
    readings: dict[int, Reading] = field(default_factory=dict)
    # Uplink (last_event_id) that readings/downlinks were last fetched successfully for.
    readings_event_id: int | None = None
    downlinks_event_id: int | None = None
    # None means the active-alarm fetch failed: alarm entities go unavailable.
    active_alarms: list[Alarm] | None = None
    pump_times: list[PumpTime] = field(default_factory=list)
    pump_times_fetched: datetime | None = None
    downlinks: list[DownlinkEvent] = field(default_factory=list)


@dataclass(slots=True)
class _AlarmFetch:
    """One device's alarm data from this update, reconciled only once the update succeeds."""

    active: list[Alarm] | None
    history: list[Alarm] | None
    lookups: dict[int, Alarm] = field(default_factory=dict)
    attempted: list[int] = field(default_factory=list)
    missing: set[int] = field(default_factory=set)


@dataclass(slots=True)
class NoriaData:
    """Coordinator payload."""

    devices: dict[int, DeviceData]
    events: list[AlarmEvent]
    update_id: int
    unsupported: dict[int, str | None] = field(default_factory=dict)


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.alarms")


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the alarm tracker state of a removed entry."""
    await _store(hass, entry_id).async_remove()


def _auth_failed() -> ConfigEntryAuthFailed:
    return ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth")


def _rate_limited(err: NoriaRateLimitError) -> UpdateFailed:
    return UpdateFailed(translation_domain=DOMAIN, translation_key="rate_limited", retry_after=err.retry_after)


class NoriaCoordinator(DataUpdateCoordinator[NoriaData]):
    """Polls one NOM account."""

    config_entry: NoriaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: NoriaConfigEntry) -> None:
        """Create the client on HA's shared session."""
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = NoriaClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], async_get_clientsession(hass))
        self._store = _store(hass, entry.entry_id)
        self.tracker = AlarmTracker(started_at=dt_util.utcnow())
        self.failing: set[tuple[int, str]] = set()
        self._unsupported_logged: set[int] = set()
        self._update_id = 0

    async def _async_setup(self) -> None:
        """Restore alarm tracker state before the first refresh."""
        self.tracker = AlarmTracker.from_dict(await self._store.async_load(), started_at=dt_util.utcnow())

    async def async_save(self) -> None:
        """Write tracker state now (on unload)."""
        await self._store.async_save(self.tracker.as_dict())

    async def _async_update_data(self) -> NoriaData:
        try:
            devices = await self.client.async_get_devices()
        except (NoriaAuthError, NoriaForbiddenError) as err:
            # Not even the device list is readable: the credentials are wrong or revoked.
            raise _auth_failed() from err
        except NoriaRateLimitError as err:
            raise _rate_limited(err) from err
        except NoriaError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="cannot_connect") from err

        now = dt_util.utcnow()
        previous = self.data.devices if self.data else {}
        result: dict[int, DeviceData] = {}
        alarm_fetches: dict[int, _AlarmFetch] = {}
        unsupported: dict[int, str | None] = {}
        for device in devices:
            if not device.is_tlakan:
                unsupported[device.id] = device.device_type.parse_method
                if device.id not in self._unsupported_logged:
                    self._unsupported_logged.add(device.id)
                    LOGGER.info(
                        "Noria device %s (%s) is not a TLAKAN controller and is not supported yet",
                        device.id,
                        device.device_type.parse_method,
                    )
                continue
            result[device.id], alarm_fetches[device.id] = await self._async_fetch_device(
                device, previous.get(device.id), now
            )

        # Every call of this update has completed: only now commit alarm state. An exception
        # above (reauth, rate limit) leaves the tracker untouched, so no event is lost.
        events: list[AlarmEvent] = []
        for device_id, fetch in alarm_fetches.items():
            events.extend(
                self.tracker.update(
                    device_id,
                    now=now,
                    active=fetch.active,
                    history=fetch.history,
                    lookups=fetch.lookups,
                    attempted=fetch.attempted,
                    missing=fetch.missing,
                )
            )
        self.tracker.forget_devices(result)
        self._store.async_delay_save(self.tracker.as_dict, STORE_SAVE_DELAY)
        self._update_id += 1
        return NoriaData(devices=result, events=events, update_id=self._update_id, unsupported=unsupported)

    async def _async_fetch_device(
        self, device: Device, previous: DeviceData | None, now: datetime
    ) -> tuple[DeviceData, _AlarmFetch]:
        did = device.id
        data = DeviceData(
            device=device,
            readings=dict(previous.readings) if previous else {},
            readings_event_id=previous.readings_event_id if previous else None,
            downlinks_event_id=previous.downlinks_event_id if previous else None,
            pump_times=previous.pump_times if previous else [],
            pump_times_fetched=previous.pump_times_fetched if previous else None,
            downlinks=previous.downlinks if previous else [],
        )

        # Alarms: active list (authoritative), history window, then lookups for ends.
        data.active_alarms = await self._optional(did, "active_alarms", self.client.async_get_active_alarms(did))
        fetch = _AlarmFetch(
            active=data.active_alarms,
            history=await self._optional(
                did, "alarm_history", self.client.async_get_alarm_history(did, self.tracker.window_start(did, now))
            ),
        )
        for alarm_id in self.tracker.ids_to_look_up(did, fetch.active, fetch.history):
            fetch.attempted.append(alarm_id)
            try:
                alarm = await self._optional(
                    did, f"alarm {alarm_id}", self.client.async_get_alarm(did, alarm_id), not_found=True
                )
            except NoriaNotFoundError:
                fetch.missing.add(alarm_id)
                continue
            if alarm is not None:
                fetch.lookups[alarm_id] = alarm

        # Readings and downlink history change only with a new uplink; a failed fetch is
        # retried on the next update because the marker only moves on success.
        if device.last_event_id is None or data.readings_event_id != device.last_event_id:
            readings = await self._optional(
                did, "readings", self.client.async_get_latest_readings(did, device.device_type.pump_count)
            )
            if readings is not None:
                for pump, reading in readings.items():
                    current = data.readings.get(pump)
                    if current is None or reading.timestamp >= current.timestamp:
                        data.readings[pump] = reading
                data.readings_event_id = device.last_event_id
        if device.last_event_id is None or data.downlinks_event_id != device.last_event_id:
            downlinks = await self._optional(did, "downlinks", self.client.async_get_downlink_events(did))
            if downlinks is not None:
                data.downlinks, data.downlinks_event_id = downlinks, device.last_event_id

        if data.pump_times_fetched is None or now - data.pump_times_fetched >= PUMP_TIMES_INTERVAL:
            pump_times = await self._optional(did, "pump_times", self.client.async_get_pump_times(did))
            if pump_times is not None:
                data.pump_times, data.pump_times_fetched = pump_times, now
        return data, fetch

    async def _optional[T](self, device_id: int, part: str, call: Awaitable[T], *, not_found: bool = False) -> T | None:
        """Await a per-device call; on failure keep going and return None (logged once).

        401 anywhere starts reauth and 429 anywhere backs off the whole update. A 403 only
        fails this part: the account is valid but not allowed to read this endpoint. With
        not_found=True a 404 is re-raised for the caller to handle.
        """
        key = (device_id, part)
        try:
            result = await call
        except NoriaAuthError as err:
            raise _auth_failed() from err
        except NoriaRateLimitError as err:
            raise _rate_limited(err) from err
        except NoriaNotFoundError:
            if not_found:
                self.failing.discard(key)
                raise
            self._log_failure(key, "not found")
            return None
        except NoriaError as err:
            self._log_failure(key, err)
            return None
        if key in self.failing:
            self.failing.discard(key)
            LOGGER.info("Fetching %s for Noria device %s works again", part, device_id)
        return result

    def _log_failure(self, key: tuple[int, str], err: object) -> None:
        if key not in self.failing:
            self.failing.add(key)
            LOGGER.warning("Fetching %s for Noria device %s failed: %s", key[1], key[0], err)

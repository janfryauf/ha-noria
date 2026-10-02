"""Async client for the Noria Online Monitoring (NOM) REST API.

Read-only by design. On Noria a GET is not automatically harmless: the noriaonline.cz
website runs device commands (pump, horn) on plain GETs, and the REST spec has a GET that
executes callbacks. This client therefore:
- calls only the documented read endpoints in _READ_ONLY_PATHS (anything else raises);
- never follows redirects and never retries; a failed read is retried by the next poll.
Free of Home Assistant imports so it can become a library.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, encode_basic_auth

from .models import Alarm, Device, DownlinkEvent, PumpTime, Reading

API_URL = "https://nom.noriatechnology.cz/api"
PAGE_SIZE = 50
MAX_PAGES = 20
MAX_READING_PAGES = 5
READINGS_LOOKBACK = timedelta(hours=48)
TIMEOUT = ClientTimeout(total=30)

_READ_ONLY_PATHS = tuple(
    re.compile(pattern)
    for pattern in (
        r"/devices",
        r"/devices/count",
        r"/devices/\d+/readings",
        r"/devices/\d+/alarms",
        r"/devices/\d+/alarms/active",
        r"/devices/\d+/alarms/\d+",
        r"/devices/\d+/pump_times",
        r"/devices/\d+/downlink_events",
    )
)
_COMMAND_PATH = re.compile(r"downlink_(?!events(/|$))|horn|swap_|sd_assembled", re.IGNORECASE)


class NoriaError(Exception):
    """Base error for the NOM client."""


class NoriaAuthError(NoriaError):
    """Credentials were rejected (401/403)."""


class NoriaConnectionError(NoriaError):
    """Network problem, timeout or server error; retry later."""


class NoriaRateLimitError(NoriaConnectionError):
    """HTTP 429."""

    def __init__(self, retry_after: float | None) -> None:
        """Keep the server's Retry-After hint (seconds), if any."""
        super().__init__(f"Rate limited (retry after {retry_after}s)")
        self.retry_after = retry_after


class NoriaForbiddenError(NoriaError):
    """HTTP 403: authenticated, but this endpoint is not allowed for the account.

    Not a credential problem: the spec documents 403 per endpoint ("Not authorized").
    """


class NoriaNotFoundError(NoriaError):
    """HTTP 404: record or capability not available."""


class NoriaResponseError(NoriaError):
    """Unexpected status (including any redirect) or malformed payload."""


class NoriaRefusedError(NoriaError):
    """The client refused to send a request to a non-allowlisted path."""


def _check_path(path: str) -> None:
    if _COMMAND_PATH.search(path) or not any(p.fullmatch(path) for p in _READ_ONLY_PATHS):
        raise NoriaRefusedError(f"Refusing to call {path}: not a known read-only endpoint")


def _parse[T](factory: Callable[[dict[str, Any]], T], items: list[dict[str, Any]], what: str) -> list[T]:
    """Build models; a malformed item is a response error, not a crash."""
    try:
        return [factory(item) for item in items]
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise NoriaResponseError(f"Malformed {what} in NOM response") from err


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _utc(value: datetime) -> str:
    """NOM reads zone-less times as Prague local time, so always send UTC with Z."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NoriaClient:
    """Read-only NOM client for one account."""

    def __init__(self, username: str, password: str, session: ClientSession, base_url: str = API_URL) -> None:
        """Use the caller's aiohttp session (Home Assistant's shared one)."""
        self._headers = {"Accept": "application/json", "Authorization": encode_basic_auth(username, password)}
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One GET, no redirects, no retries. Returns parsed JSON, or None for 204."""
        _check_path(path)
        try:
            async with self._session.get(
                f"{self._base_url}{path}",
                params=params,
                timeout=TIMEOUT,
                allow_redirects=False,
                headers=self._headers,
            ) as response:
                status = response.status
                if status == 401:
                    raise NoriaAuthError("Authentication failed (401)")
                if status == 403:
                    raise NoriaForbiddenError(f"{path} is not allowed for this account (403)")
                if status == 429:
                    raise NoriaRateLimitError(_retry_after(response.headers.get("Retry-After")))
                if status == 404:
                    raise NoriaNotFoundError(f"{path} not found")
                if status >= 500:
                    raise NoriaConnectionError(f"Server error {status} for {path}")
                if status == 204:
                    return None
                if status != 200:
                    raise NoriaResponseError(f"Unexpected status {status} for {path}")
                text = await response.text()
        except (TimeoutError, ClientError) as err:
            raise NoriaConnectionError(f"Error talking to NOM: {err}") from err
        try:
            return json.loads(text) if text else None
        except ValueError as err:
            raise NoriaResponseError(f"Invalid JSON from {path}") from err

    async def _get_list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data = await self._get(path, params)
        if data is None:
            return []
        if not isinstance(data, list):
            raise NoriaResponseError(f"Expected a list from {path}")
        return data

    async def _paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        page_size: int = PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """Collect every page, or raise: callers treat these lists as complete.

        The active list is authoritative for "open now" and a history fetch advances the
        alarm watermark, so a truncated list would hide problems or lose events. Hitting
        MAX_PAGES therefore raises NoriaResponseError and the caller keeps its previous state.
        """
        items: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            batch = await self._get_list(path, {**(params or {}), "limit": page_size, "offset": page * page_size})
            items.extend(batch)
            if len(batch) < page_size:
                return items
        raise NoriaResponseError(
            f"{path} has more than {MAX_PAGES * page_size} items; refusing to use an incomplete list"
        )

    # --- endpoints ------------------------------------------------------------

    async def async_get_device_count(self) -> int:
        """Return the number of devices; a cheap credential check."""
        data = await self._get("/devices/count")
        if not isinstance(data, int) or isinstance(data, bool):
            raise NoriaResponseError("Expected an integer device count")
        return data

    async def async_get_devices(self) -> list[Device]:
        """All devices on the account."""
        return _parse(Device.from_api, await self._paginate("/devices"), "device")

    async def async_get_latest_readings(self, device_id: int, pump_count: int) -> dict[int, Reading]:
        """Newest reading per pump, deduplicated on (device, pump, timestamp).

        Fetches a page sized for every pump and pages further (up to READINGS_LOOKBACK)
        until each pump has a reading: limit=1 is not enough on multi-pump controllers.
        """
        page_size = max(10, 4 * pump_count)
        wanted = set(range(1, pump_count + 1))
        latest: dict[int, Reading] = {}
        seen: set[tuple[int, int, datetime]] = set()
        newest: datetime | None = None
        for page in range(MAX_READING_PAGES):
            rows = await self._get_list(
                f"/devices/{device_id}/readings", {"limit": page_size, "offset": page * page_size}
            )
            for reading in _parse(Reading.from_api, rows, "reading"):
                if reading is None or reading.key in seen:
                    continue
                seen.add(reading.key)
                newest = newest or reading.timestamp
                current = latest.get(reading.pump_number)
                if current is None or reading.timestamp > current.timestamp:
                    latest[reading.pump_number] = reading
                if newest - reading.timestamp > READINGS_LOOKBACK:
                    return latest
            if wanted <= latest.keys() or len(rows) < page_size:
                return latest
        return latest

    async def async_get_active_alarms(self, device_id: int) -> list[Alarm]:
        """Alarms open right now (all pages)."""
        return _parse(Alarm.from_api, await self._paginate(f"/devices/{device_id}/alarms/active"), "alarm")

    async def async_get_alarm_history(self, device_id: int, since: datetime) -> list[Alarm]:
        """Alarms and statuses created at or after `since` (NOM filters on created_at)."""
        items = await self._paginate(f"/devices/{device_id}/alarms", {"from": _utc(since)})
        return _parse(Alarm.from_api, items, "alarm")

    async def async_get_alarm(self, device_id: int, alarm_id: int) -> Alarm:
        """Return the current state of one alarm record."""
        data = await self._get(f"/devices/{device_id}/alarms/{alarm_id}")
        if not isinstance(data, dict):
            raise NoriaResponseError(f"Expected an object for alarm {alarm_id}")
        return _parse(Alarm.from_api, [data], "alarm")[0]

    async def async_get_pump_times(self, device_id: int) -> list[PumpTime]:
        """Average/reference pumping times."""
        items = await self._get_list(f"/devices/{device_id}/pump_times")
        return [pump_time for pump_time in _parse(PumpTime.from_api, items, "pump time") if pump_time is not None]

    async def async_get_downlink_events(self, device_id: int, limit: int = 10) -> list[DownlinkEvent]:
        """Newest rows of the downlink (command) history. Read-only."""
        items = await self._get_list(f"/devices/{device_id}/downlink_events", {"limit": limit, "offset": 0})
        return _parse(DownlinkEvent.from_api, items, "downlink event")

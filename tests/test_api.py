"""NOM client: allowlist, pagination, error mapping, no redirects, per-pump readings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import load_json_array_fixture
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.noria.api import (
    API_URL,
    NoriaAuthError,
    NoriaClient,
    NoriaConnectionError,
    NoriaForbiddenError,
    NoriaNotFoundError,
    NoriaRateLimitError,
    NoriaRefusedError,
    NoriaResponseError,
)


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> NoriaClient:
    """Client on HA's session; depends on aioclient_mock so the session is the mocked one."""
    return NoriaClient("user", "pass", async_get_clientsession(hass))


def reading(pump: int, timestamp: str, total: int, row_id: int) -> dict[str, Any]:
    return {
        "id": row_id,
        "device_id": 7,
        "pump_number": pump,
        "timestamp": timestamp,
        "total_time": total,
        "switch_count": 1,
        "time_diff": 0,
        "switch_diff": 0,
        "phase_alarm_count": 0,
        "alarm_count": 0,
        "prob_sens": 50,
    }


async def test_device_count_and_devices(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_URL}/devices/count", text="1")
    aioclient_mock.get(f"{API_URL}/devices", json=load_json_array_fixture("devices.json"))
    assert await client.async_get_device_count() == 1
    devices = await client.async_get_devices()
    assert [d.id for d in devices] == [3040]
    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert url.query["limit"] == "50"
    assert url.query["offset"] == "0"
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"] == "Basic dXNlcjpwYXNz"  # user:pass


async def test_devices_are_paginated(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    template = load_json_array_fixture("devices.json")[0]
    first = [{**template, "id": i} for i in range(50)]
    aioclient_mock.get(f"{API_URL}/devices", params={"limit": 50, "offset": 0}, json=first)
    aioclient_mock.get(f"{API_URL}/devices", params={"limit": 50, "offset": 50}, json=[{**template, "id": 99}])
    devices = await client.async_get_devices()
    assert len(devices) == 51
    assert devices[-1].id == 99
    assert aioclient_mock.call_count == 2


async def test_active_alarms_are_paginated(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    template = load_json_array_fixture("alarm_examples_tlakan.json")[0]
    page = [{**template, "id": i, "active": True} for i in range(50)]
    aioclient_mock.get(f"{API_URL}/devices/7/alarms/active", params={"offset": 0}, json=page)
    aioclient_mock.get(f"{API_URL}/devices/7/alarms/active", params={"offset": 50}, json=[])
    assert len(await client.async_get_active_alarms(7)) == 50


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, NoriaAuthError),
        (403, NoriaForbiddenError),
        (404, NoriaNotFoundError),
        (500, NoriaConnectionError),
        (503, NoriaConnectionError),
        (302, NoriaResponseError),
        (418, NoriaResponseError),
    ],
)
async def test_status_mapping(
    client: NoriaClient, aioclient_mock: AiohttpClientMocker, status: int, error: type[Exception]
) -> None:
    aioclient_mock.get(f"{API_URL}/devices/count", status=status, headers={"Location": "https://example.invalid"})
    with pytest.raises(error):
        await client.async_get_device_count()
    assert aioclient_mock.call_count == 1  # no retry, no redirect follow-up


async def test_rate_limit_keeps_retry_after(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_URL}/devices/count", status=429, headers={"Retry-After": "30"})
    with pytest.raises(NoriaRateLimitError) as err:
        await client.async_get_device_count()
    assert err.value.retry_after == 30


async def test_connection_errors_and_bad_json(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_URL}/devices/count", exc=ClientError())
    with pytest.raises(NoriaConnectionError):
        await client.async_get_device_count()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_URL}/devices/count", exc=TimeoutError())
    with pytest.raises(NoriaConnectionError):
        await client.async_get_device_count()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_URL}/devices/count", text="<html>login</html>")
    with pytest.raises(NoriaResponseError):
        await client.async_get_device_count()


async def test_no_content_is_empty(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_URL}/devices/7/pump_times", status=204)
    assert await client.async_get_pump_times(7) == []


@pytest.mark.parametrize(
    "path",
    [
        "/devices/7/downlink_horn_on",
        "/devices/7/downlink_horn_off",
        "/devices/7/downlink_pump",
        "/devices/7/sd_assembled_device",
        "/devices/7/swap_unit",
        "/tokens",
        "/devices/7/params",
    ],
)
async def test_non_allowlisted_paths_are_refused(
    client: NoriaClient, aioclient_mock: AiohttpClientMocker, path: str
) -> None:
    with pytest.raises(NoriaRefusedError):
        await client._get(path)
    assert aioclient_mock.call_count == 0


async def test_alarm_history_filters_in_utc(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_URL}/devices/7/alarms", json=load_json_array_fixture("alarms_tlakan.json"))
    since = datetime(2026, 10, 1, 7, 29, 5, tzinfo=UTC)
    alarms = await client.async_get_alarm_history(7, since)
    assert len(alarms) == 10
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query["from"] == "2026-10-01T07:29:05Z"


async def test_single_alarm_lookup(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    row = load_json_array_fixture("alarm_examples_tlakan.json")[0]
    aioclient_mock.get(f"{API_URL}/devices/3040/alarms/{row['id']}", json=row)
    alarm = await client.async_get_alarm(3040, row["id"])
    assert alarm.id == row["id"]
    assert alarm.code == row["human_number"]


async def test_latest_reading_per_pump_with_duplicates(
    client: NoriaClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Synthetic 2-pump controller: one row per pump per timestamp, plus duplicate ids."""
    rows = [
        reading(1, "2026-10-01T18:55:00.000+02:00", 100, 1),
        reading(2, "2026-10-01T18:55:00.000+02:00", 200, 2),
        reading(1, "2026-10-01T18:55:00.000+02:00", 100, 3),  # duplicate under another id
        reading(1, "2026-10-01T17:55:00.000+02:00", 90, 4),
        reading(2, "2026-10-01T17:55:00.000+02:00", 190, 5),
    ]
    aioclient_mock.get(f"{API_URL}/devices/7/readings", json=rows)
    latest = await client.async_get_latest_readings(7, pump_count=2)
    assert {pump: r.total_time for pump, r in latest.items()} == {1: 100, 2: 200}
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query["limit"] == "10"


async def test_readings_page_further_until_every_pump_has_one(
    client: NoriaClient, aioclient_mock: AiohttpClientMocker
) -> None:
    page1 = [reading(1, f"2026-10-01T{18 - i:02d}:55:00.000+02:00", 100 - i, i) for i in range(10)]
    page2 = [reading(2, "2026-10-01T05:55:00.000+02:00", 50, 99)]
    aioclient_mock.get(f"{API_URL}/devices/7/readings", params={"offset": 0}, json=page1)
    aioclient_mock.get(f"{API_URL}/devices/7/readings", params={"offset": 10}, json=page2)
    latest = await client.async_get_latest_readings(7, pump_count=2)
    assert latest[1].total_time == 100
    assert latest[2].total_time == 50


async def test_downlink_events(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        f"{API_URL}/devices/3040/downlink_events", json=load_json_array_fixture("downlink_events_tlakan.json")
    )
    downlinks = await client.async_get_downlink_events(3040)
    assert {d.data for d in downlinks} >= {"9100", "9101"}


async def test_malformed_items_are_response_errors(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_URL}/devices/7/alarms/active", json=[{"id": 1}])  # no device_id
    with pytest.raises(NoriaResponseError):
        await client.async_get_active_alarms(7)


async def test_paging_past_max_pages_is_an_error(
    client: NoriaClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.noria import api

    monkeypatch.setattr(api, "MAX_PAGES", 2)
    template = load_json_array_fixture("alarm_examples_tlakan.json")[0]
    aioclient_mock.get(f"{API_URL}/devices/7/alarms/active", json=[{**template, "id": i} for i in range(50)])
    with pytest.raises(NoriaResponseError, match="incomplete"):
        await client.async_get_active_alarms(7)  # a truncated active list must never look complete
    assert aioclient_mock.call_count == 2


async def test_readings_stop_at_the_lookback(client: NoriaClient, aioclient_mock: AiohttpClientMocker) -> None:
    """Pump 2 never reports: paging stops once rows are older than 48 h, not after every page."""
    rows = [reading(1, f"2026-10-0{3 - day}T12:00:00.000+02:00", 100 - day, day) for day in range(3)]
    aioclient_mock.get(f"{API_URL}/devices/7/readings", json=rows)
    latest = await client.async_get_latest_readings(7, pump_count=2)
    assert set(latest) == {1}
    assert latest[1].total_time == 100
    assert aioclient_mock.call_count == 1

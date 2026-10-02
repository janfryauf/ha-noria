# ha-noria design (v0.1)

Decisions for the first version of the `noria` integration. The observed API behaviour these rest on is recorded in the workspace's `.claude/skills/noria-api/live-findings.md`. Fixtures are in `tests/fixtures/`.

## 1. Scope and API coverage

v0.1 is **read-only monitoring of TLAKAN pump stations**, the only device family with real data. Each NOM endpoint has one of these statuses:

- **verified**: called on the live account and the shape is known.
- **verified-empty**: called, but the account has no device of that kind (204), so the shape is still spec-only.
- **schema-only**: not called.
- **out of scope**: account management or administration; needs a separate justification before it's ever used.

| Area | Endpoints | Status | Use in v0.1 |
|---|---|---|---|
| Devices | `GET /devices`, `/devices/count`, `/devices/{id}` | verified | list = discovery and metadata each update; count = config-flow credential check; detail = unused (same as the list item) |
| Readings | `GET /devices/{id}/readings`, `/readings/count` | verified | latest reading per pump |
| Alarms | `GET /devices/{id}/alarms/active`, `/alarms` | verified (active list empty so far) | current problems + alarm/status events |
| Single alarm | `GET /devices/{id}/alarms/{alarm_id}` | verified (same shape as a list item) | learns the end of an alarm after it leaves the active list (§3) |
| Alarm counts | `/alarms/count`, `/alarms/active/count` | verified | not used; counts come from the lists |
| Pump times | `GET /devices/{id}/pump_times` | verified | average/reference pump time diagnostics |
| Params | `GET /devices/{id}/params` | verified | not used (only internal flags on this device) |
| Device types | `GET /device_types`, `/device_types/{id}` | verified | not needed; `device_type` is embedded in each device |
| Raw uplinks | `GET /devices/{id}/events*` | verified, **deprecated** | not used (raw payloads only) |
| QI meters | `/devices/qi_readings`, `/devices/{id}/qi_readings`, `/devices/{id}/qi_latest`, `/meters/last-metrics`, `/meters/alerts` | verified-empty (204) | none until a QI fixture exists |
| Water meters | `/devices/wm_readings`, `/water_meters/manufacturers*` | verified-empty / schema-only | none until a meter fixture exists |
| Sync (meter calibration) | `GET/POST /devices/{id}/sync`, `/sync_history` | schema-only, POST is a write | out of v0.1 |
| **Controls (REST)** | `PUT /devices/{id}/params/{param_id}` | schema-only, **downlink to the device** | deferred (§9) |
| **Controls (website)** | `www.noriaonline.cz` `/devices/{id}/downlink_pump`, `/downlink_horn_on`, `/downlink_horn_off` | observed manually, **GET with side effects** | future capability (§9); never probed or called |
| Downlink history | `GET /devices/{id}/downlink_events*` (REST) | verified | read-only; diagnostics only, with each row's delivery state (queued or sent, never "confirmed"; §9) |
| Device lifecycle | `POST/PUT/DELETE /devices`, `swap_unit`, `swap_faulty_unit`, `sd_assembled_device` (admin) | out of scope | never from HA |
| Account admin | `/device_groups*`, `/tokens*`, `/app_tokens` | out of scope | never from HA |
| Third-party export | `/anasoft` (`nom_key`) | out of scope | different auth, not for HA |

**Controls are future capabilities** (§9): not implemented and not executed. "Using the API fully" means useful monitoring first. Device replacement and account administration don't belong in HA.

**GET is not read-only on Noria.** The website triggers device commands with plain GETs, and the REST spec has a GET that executes callbacks (`sd_assembled_device`). The client calls only an explicit allowlist of documented read endpoints. It never follows redirects to other hosts, and it never retries a request that could be a command.

## 2. Update cycle

One `DataUpdateCoordinator` per config entry (account) with a fixed **5 minute** interval. Per update:

1. **Devices**: `GET /devices`, paginated (see §5). Gives metadata, `last_message_at`, `last_event_id`, firmware and subscription end. **Required**: if it fails, the whole update fails.
2. **Active alarms**, per device: `GET /devices/{id}/alarms/active`, paginated. This is the authoritative current problem state, so it's fetched every update.
3. **Alarm history**, per device: `GET /devices/{id}/alarms?from=<window start>`, paginated, with the time always sent in UTC (`Z`). Feeds events and catches alarms shorter than the poll interval (§3).
4. **Alarm lookups**, per device: `GET /devices/{id}/alarms/{alarm_id}` for alarms that just left the active list without a known end. At most 10 per update; the rare case.
5. **Readings**, per device, only on the first refresh or when `last_event_id` changed (§4).
6. **Downlink history**, per device: `GET /devices/{id}/downlink_events?limit=10`, alongside readings. Diagnostics only.
7. **Pump times**, per device, on the first refresh and then every 6 h.

Expected load for one device is about 40 GET calls per hour. No rate limit has been seen. A 429 maps to `UpdateFailed(retry_after=...)`.

## 3. Alarms and events

**Why history as well:** in the fixtures, alarms 105, 106 and 107 lasted **60 s**, and statuses (901–913) are instantaneous. Polling only active alarms every 5 minutes would never see them.

**Identity and matching.**
- Every alarm or status has a stable `id`. When it ends, `updated_at`, `status_end` and `active` change.
- An alarm's kind is the pair **`(alarm_type, alarm_number)`**, because `alarm_number` repeats across types (3 = pump failure as an alarm and counters reset as a status). The per-pump key is `(alarm_type, alarm_number, pump_number)`.

**What the API gives** (verified in the second pass, see live-findings):
- `from`/`to` on `/alarms` filter on **`created_at`**, inclusive.
- A history window therefore shows an alarm's end only if the alarm was also *created* inside the window. A long alarm that ends hours later is never returned by later windows.
- `GET /alarms/{alarm_id}` returns the current state of one record.

**Reconciliation.** This lives in `custom_components/noria/alarms.py` (`AlarmTracker`): pure, HA-free and unit-tested. Each device keeps a set of tracked records, each with id, kind, pump, `created_at`, start, end, whether it was in the last active list, and whether its end has been announced. Every update combines three sources:

1. **Active list.** This is authoritative for "open right now". Any id in it is open.
2. **History window.** `created_at ≥ max(watermark − 2 h, now − 8 h)`, with the time sent in UTC. It brings new records, including ones that already ended (alarms shorter than the poll interval). The 2 h overlap absorbs the 10–30 s creation lag and server-side batch alarms such as 111.
3. **Lookups.** `GET /alarms/{id}` for every alarm without a known end that is missing from a *successful* active list ("pending end"), whether or not it was ever seen open there. That covers alarms known only from history while the active list was unavailable. A failed lookup is retried at the next update, which is fine because it's a read. At most 10 per update, least-tried first, so permanently failing ids can't starve the rest. If the lookup still shows the alarm as active (list lag), the record stays pending. A **404** means the record is gone: it is dropped without an `ended` event, since its end can't be known. The binary sensors follow the active list, so nothing stays on.

Rules:
- **New id**, whether from history or the active list: track it. An alarm fires `started`; if it already has an end (it was *never observed active*), `ended` fires right after it. A status fires one `status` event.
- **Known open record gets an end** (from a history row in the window, or from a lookup): fire `ended`. This is how an end set **after the creation window** is learned. Never infer an end from an alarm's absence in history.
- **An end on a record NOM marks `active` is ignored** (design question 2 is still open). The active list always wins: a listed alarm is open even if it was taken as ended before. This covers an alarm that ends between the active and the history fetch of the seeding poll.
- Every alarm fires exactly one `started` and at most one `ended` over its lifetime, deduplicated by id across overlapping windows and restarts.
- Watermark (`synced_until`) = time of the last successful history fetch. Ended records created before `synced_until − 4 h` are pruned; open or pending records are kept however old they are. State of devices that leave the account is dropped.
- If the history call fails, the active list and lookups are still processed. If the active-list call fails, nothing new is marked pending, because absence from a failed fetch means nothing.
- **Atomic commit.** The coordinator fetches everything for every device first, and only then feeds the tracker. If any call ends the update early (reauth, rate limit), the tracker is left untouched, so the next successful update reports the same events and nothing is lost.
- **`delayed`** = the event is reported more than 15 min after it happened (`created_at` for started/status, the end time for ended). That covers restart replays and NOM outages alike.
- **Order.** NOM lists history newest first, but an update's events fire **oldest first**, so each event entity ends up showing its newest event. The sort key is device time (`status_start`; for an end, `status_end`, but never earlier than its own start), then started-before-ended, then `created_at` and id.

**Persistence.** A per-entry `Store` (`.storage/noria.<entry_id>.alarms`) holds the tracker state: watermark, tracked records, open/pending flags. Saving is debounced, and the store is removed with the entry.

**Events.** Two `EventEntity`s per device, without a device class:
- `alarm`, with event types = alarm kinds (`pump_failure`, `emergency_level`, `emergency_level_float_error`, `emergency_level_probe_error`, `probe_contamination`, `za_switch`, `pump_damage_risk`, `unknown_alarm`). Attributes: `phase` (`started` or `ended`), `code` (`human_number`), `alarm_id`, `pump_number`, `started`, `ended`, `duration_s`, `delayed`.
- `status`, with event types = status kinds (`power_connected`, `manual_pumping`, `alarm_counters_reset`, `cleaning`, `nb_iot_reregistration`, plus spec-derived ones not yet seen, and `unknown_status`). Attributes: `code`, `alarm_id`, `pump_number`, `occurred`, `delayed`.
- Unknown codes fire `unknown_*` with the code in the attributes and are logged once.

**Restart behaviour.**
- First setup (no store): fetch the window and mark everything as seen **without firing**, so no flood of old events. Alarms that are open at that point are tracked, so their `ended` still fires later.
- Restart with a store: unseen records created up to **6 h** before startup fire with `delayed: true`; older ones are tracked silently. Records that were open before the restart are reconciled through the active list and lookups, so an alarm that ended during the outage fires `ended` with `delayed: true`.
- `EventEntity` restores its last state on its own.
- **Delivery of the first refresh's events.** These are delivered by `coordinator.async_update_listeners()` after the platforms are set up, not from `async_added_to_hass`. HA ignores state writes while an entity is still being added, so only the last of several replayed events would survive.

**Binary sensors** (device class PROBLEM) reflect active alarms only: on while the active list contains that kind, and per pump on 2-pump controllers. If the active-alarms call fails, these entities become **unavailable** rather than reporting "no problem". Unknown active alarm codes still count in the `active_alarms` sensor.

## 4. Readings and pumps

- **Deduplication key `(device_id, pump_number, timestamp)`.** Duplicate rows with the same timestamp exist. A 2-pump controller may also send one row per pump with the same timestamp, which a timestamp-only key would wrongly drop.
- **Latest per pump.** Fetch `limit = max(10, 4 × pump_count)` newest readings and keep the newest row per `pump_number`. A pump missing from that page keeps its previous value, and on the first refresh paging continues for up to 48 h of history until every pump in `device_type.pump_count` has a reading. `limit=1` is never enough on multi-pump devices.
- Sort by `timestamp` (parsed with `dt_util.parse_datetime`), never by `id`.
- **Unverified:** the 2-pump layout. The account has only a 1-pump device, so tests must cover a synthetic 2-pump fixture, flagged as synthetic.

## 5. Counters and statistics

HA's recorder (`homeassistant/components/sensor/recorder.py::reset_detected`) treats a `TOTAL_INCREASING` drop as follows:
- A drop of **less than 10%** only logs a "not strictly increasing" warning.
- A drop of **10% or more** counts as a **meter reset**: the next value is added to the long-term sum in full.

| Value | Observed | State class | Why |
|---|---|---|---|
| `total_time` (pumping time, s) | dips of 12–51 s after restarts | **`TOTAL`**, no `last_reset` | The sum follows the actual deltas, so dips net out with no warnings and no false resets. Recorded as s, suggested display in h |
| `switch_count` (cycles) | never decreased in 8,899 readings | **`TOTAL_INCREASING`** | A drop would mean a genuine reset (unit swap), and HA's reset handling is right for that |
| `phase_alarm_count`, `alarm_count` | can be reset (status 903); dropped 9→6 once without 903 | **none** | Diagnostic counters with no long-term statistics, so a drop can't inflate a sum |
| `time_diff` (pumping in last interval, s) | can be negative after a restart | **`MEASUREMENT`**, disabled by default | Negative values are clamped to 0, since negative pumping makes no sense |
| Pump times (s) | float, per window | `MEASUREMENT` | |

Tests use the `time_diff = -42` reading in `readings_tlakan.json` to check that pumping time keeps a sane state.

## 6. Pagination

A single client helper, `_paginate(path, params, page_size=50)` with `MAX_PAGES = 20`, is used for `/devices`, `/alarms/active` and `/alarms` history.
- It returns once a page has fewer than `page_size` items.
- **More than `MAX_PAGES` pages raises `NoriaResponseError`; it never returns a partial list.** These lists are treated as complete: the active list decides "open now", a history fetch advances the alarm watermark, and the device list defines the devices. A truncated list would hide problems or lose events. The error goes through the normal failure paths (§7): the device list fails the whole update, active alarms make the alarm entities unavailable, and history leaves the watermark where it was.
- Readings are not affected: the newest rows come first, so the per-pump latest stays correct even when paging stops at the 48 h lookback.
- `/devices/count` is used only in the config flow.

## 7. Failure handling

| Condition | Detection | Behaviour |
|---|---|---|
| Wrong or expired credentials | HTTP 401 on any call, or 403 on `/devices` | `ConfigEntryAuthFailed` → reauth flow (the config flow maps both to `invalid_auth`) |
| Endpoint not allowed for the account | HTTP 403 on a per-device call (the spec documents 403 per endpoint) | `NoriaForbiddenError`: that part fails like any optional call, **no reauth**. Starting reauth would loop, because the same correct password validates again |
| Devices list fails | timeout, connection error, 5xx, bad JSON | `UpdateFailed`; every entity unavailable; the coordinator logs once when the service goes away and once when it's back |
| Malformed item | a required field missing or of the wrong type | `NoriaResponseError` for that call (never an uncaught `KeyError`/`TypeError`) |
| Optional per-device call fails (readings, history, pump times, downlinks) | same errors, scoped to the call | keep that part's previous data and the rest of the update; log once per device and endpoint until it recovers; entities keep their last value. Readings and downlinks are re-fetched on every update until one fetch for the current uplink succeeds. `last_message_at` shows staleness |
| Active alarms fail | same | alarm binary sensors and `active_alarms` for that device go **unavailable**; other entities stay |
| Capability not offered | 204 or 404 on a capability endpoint (e.g. QI) | mark the capability unsupported for the device, stop calling it until reload, create no entities for it |
| No data | `200 []`, or 204 on list endpoints | a valid empty result |
| Rate limited | 429 on **any** call | `UpdateFailed(retry_after=Retry-After)` for the whole update: stop issuing calls and back off. The tracker is not committed (§3) |
| Unknown enum or code | new `alarm_type` or `alarm_number` | handled as `unknown_*`, logged once, shown in diagnostics |
| Redirect (3xx) on a read | any 3xx | not followed; treated as a temporary error for that call and logged. A redirect usually means a login page or a changed URL |

**Retries.** Read calls are retried only through the coordinator's next scheduled update, with no extra retry loop in the client. Command requests, once they exist (§9), are **never retried automatically**, whatever the error, because a timeout doesn't prove the command wasn't delivered.

Diagnostics (`diagnostics.py`) include, redacted: the last raw device list, the last readings per pump, active alarms, the watermark state, and per-endpoint failure counters.

## 8. Open questions (read-only checks for the api-explorer agent)

1. ~~What the `from`/`to` filter on `/alarms` applies to.~~ **Answered (2026-10-02):** `created_at`, inclusive; naive times are read as Prague local time; long alarms spanning the window are not returned. Built into §3.
2. What an **active** alarm looks like: are `status_end`/`status_range` null while it's active? Still open: nothing was active in either pass. The tracker treats active-list membership as authoritative, whatever `status_end` holds.
3. HTTP status and body for wrong credentials (401 or 403? JSON?). **Not to be tested on the production account**: repeated failed logins may lock it. Ask Noria, or use the test server with the user's approval.
4. Readings layout on a 2-pump controller (needs such a device, or the test server).
5. What the ZA switch (108) means, the unit of `prob_sens`, and the firmware version format. Partly answered: 108 is closely tied to pump failure (103), but its meaning, the `prob_sens` unit and the firmware format are still unknown.
6. ~~What the downlink history looks like.~~ **Answered (2026-10-02):** see live-findings "Downlink history". `sent` marks hand-over at the next uplink; no device confirmation status followed either horn command.
7. ~~Who created the horn-off downlink (2026-10-01 21:05:20)?~~ **Answered (2026-10-02):** the user, testing horn on and off in the website.

## 9. Future capabilities: device controls (not implemented)

**Status: recorded only.** Nothing here is to be implemented, probed or executed until the open points below are resolved and the user approves each step.

### What is known

- The **Noria website** (`https://www.noriaonline.cz`, the web app, not the documented REST API at `nom.noriatechnology.cz/api`) has three per-device command routes:
  - `/devices/{id}/downlink_pump`
  - `/devices/{id}/downlink_horn_on`
  - `/devices/{id}/downlink_horn_off`
- The user clicked **horn on** manually in the website. The browser sent a **GET**, which returned **HTTP 302** and created a row in the device's downlink history: `Houkačka zapnout`, data `9101`, created 2026-10-01 20:24:48 +02:00. The horn-off row (`9100`) at 21:05:20 was also the user's manual test. So these GETs are commands. Opening, prefetching, crawling or retrying such a URL can operate a physical device.
- The REST spec (`nom_api.yaml`) has **no** horn or pump command endpoints. Its only downlink-related paths are the read-only history, `GET /devices/{id}/downlink_events[/count|/{event_id}]`. The `downlink_event` schema has `event_type`, `data`, `sent`, `sent_at`, `note`, `created_at` and `updated_at`.
- The only documented REST write that reaches a device, `PUT /devices/{id}/params/{param_id}`, can't express these commands either. Its `param_type` enum is meter and alarm configuration only: `no_flow`, `back_flow`, `high_flow`, `permanent_leakage`, `sync_reading`, `sync_history`, `init_sync`, `seq_alarm`, `freeze_alarm`, `pump_times`, `daily_consumption`, `aff_offset`, `flx_lpulse`, `flx_wh_pulse`. This TLAKAN has only `seq_alarm` and `pump_times`.
- `/app_tokens` (mobile-app push tokens) suggests a Noria mobile app exists. If it offers horn/pump buttons, it calls some interface that isn't in the published spec.
- **Related device statuses, inferred from the spec** (the status code = 900 + the `stN` bit, a pattern confirmed for 901, 902, 903, 909 and 913):
  - st7, remote horn deactivation (907);
  - st8, remote horn activation (908);
  - st11, pump cannot be switched on remotely (911);
  - st12, downlink OK (912).

  None of these has ever appeared in the 4,336-row alarm history, not even after the horn commands of 2026-10-01.

### Command states: queued, sent, physically confirmed

These three states are different things and must never be merged, in code, in entity state or in UI wording.

| State | Meaning | Evidence | Observed? |
|---|---|---|---|
| **queued** | NOM has accepted the command and created a downlink row, but hasn't handed it to the device | `downlink_events` row with `sent: false` | not yet; expected between creation and the next uplink |
| **sent** | NOM handed the command to the device in its next uplink receive window | `sent: true`, with `sent_at` equal to the next uplink's receive time | yes (horn on 2 min, horn off 22 min after creation) |
| **physically confirmed** | the device reports that it actually executed the command | a device-originated signal, e.g. status 907/908 (remote horn off/on) or 912 (downlink OK), or a reading only the action explains | **never**: no such signal has been seen |

Rules:
- `sent` is **not** proof of execution. A command can be sent and silently ignored, refused (st11) or lost.
- The integration never reports a device's physical state (e.g. "horn is on") from a command or its downlink row. That state may only come from device-originated data.
- The read-only models already encode this. `DownlinkEvent.state` is `queued` or `sent` and is never derived as `confirmed`; `CommandState.CONFIRMED` is reserved for a future device signal. Diagnostics show the downlink history with these states.
- UI and log wording follow the states: "queued", "sent to the device", and "confirmed by the device" only when a device signal exists.

### Unknown (must be resolved first)

1. **Website authentication**: session cookie, CSRF, separate credentials? Whether the REST Basic-auth account works there at all is unconfirmed.
2. **Supported REST equivalents**: ask Noria (info@noriatechnology.cz) whether the REST API offers or will offer these commands, and on what terms. Driving the website from HA would be scraping an undocumented interface, so it is not acceptable without Noria's confirmation.
3. **Semantics**:
   - what `downlink_pump` does (start the pump? drain to minimum level? for how long?) and when the device refuses it (st11);
   - whether horn on/off is latched or timed.
4. **Delivery and confirmation**: see "Command states" below. Still open: whether a device signal for physical execution exists at all, and whether pending commands queue up or replace each other.
5. **Limits and safety**: daily message caps, whether commands cost anything, and whether pumping at low level can damage the pump. Alarm 111 "pump damage risk" already exists.

### Rules until then

- **Never probe** the website command routes, whatever the HTTP method. `nom.py` blocks them unconditionally, the `api-explorer` agent is forbidden to touch them, and nobody fetches noriaonline.cz URLs from tooling, WebFetch or the browser tools.
- **Never retry a command automatically**, in tooling or in the integration.
- Reading `GET /devices/{id}/downlink_events` is allowed. It's the safe way to learn the history format, including the row from the manual horn-on test.

### If it is ever implemented

- Only through an interface Noria supports and documents, verified on the test server (`nomtest`) first.
- HA `button` entities (`horn_on`, `horn_off`, `pump`) with clear names, created only for devices with `has_downlink: true`.
- One press sends exactly one request: no retry, no redirect following. A failure raises a translated `HomeAssistantError` telling the user the command **may or may not** have been sent.
- After a press, refresh the coordinator. Optionally expose the newest downlink event (sent / pending) and the related statuses (907/908/911/912) as diagnostics or events.
- The user approves every live command executed during development.

# ha-noria

Home Assistant custom integration, distributed through HACS, for **Noria Online Monitoring (NOM)** by Noria Technology (CZ): remote monitoring of pressure-sewer pumping stations (TLAKAN) and water/power meters (WMETER, QI, EMETER) over low-power radio (LTE/NB-IoT, wM-Bus, Sigfox). Owned by @janfryauf.

**Status (2026-10-02)**: v0.1 read-only integration implemented, unit-tested, and running against the live account in the dev HA, where all sensors report values. Public repo at https://github.com/janfryauf/ha-noria (MIT, branch `main`); CI covers tests, hassfest and HACS validation. Not released yet: the next step is a few days of live running, then v0.1.0 via `/ha-release`. The account's only device is a **TLAKAN SMART for one pump** (`TLK P4-NBr`, `multi_tlakan`, LTE). Real data is captured as sanitized fixtures in `tests/fixtures/`.

## Code map

| File | Role |
|---|---|
| `api.py` | HA-free async client: read allowlist, no redirects, no retries, pagination, error classes |
| `models.py` | HA-free dataclasses; alarm kinds per `(alarm_type, alarm_number)`; `CommandState` (queued / sent / confirmed, never derived as confirmed) |
| `alarms.py` | HA-free `AlarmTracker`: reconciles active list + created_at history window + `/alarms/{id}` lookups into exactly-once `started`/`ended`/`occurred` events, persisted in a `Store` |
| `coordinator.py` | 5-min update cycle, per-part failure handling (`_optional`), tracker persistence |
| `sensor.py`, `binary_sensor.py`, `event.py` | entities (per-pump where the data is per pump) |
| `diagnostics.py` | redacted dump, downlink rows with delivery state |
| `brand/icon.png`, `brand/icon@2x.png` | integration icon, served by HA from the integration folder. Generated from `assets/icon.svg` (your own design, paths only: no font needed). Regenerate with: `uv run --no-project --with resvg-py python -c "import resvg_py,pathlib as p; s=p.Path('assets/icon.svg').read_text(); [p.Path(f'custom_components/noria/brand/{n}').write_bytes(bytes(resvg_py.svg_to_bytes(svg_string=s,width=w,height=w))) for n,w in (('icon.png',256),('icon@2x.png',512))]"` |

## Design decisions

- Domain `noria`, code in `custom_components/noria/`, `iot_class: cloud_polling`, `integration_type: hub`, config flow with username/password (HTTP Basic), plus reauth and reconfigure.
- API client in `custom_components/noria/api.py`: async on aiohttp, takes HA's shared session, no `homeassistant` imports, so it can become a PyPI library later if this ever goes to HA core.
- **[docs/design.md](docs/design.md) is the spec for v0.1**: API coverage, update cycle, alarms and events, pump handling, state classes, pagination, failure handling, open questions. Follow it, and update it when a decision changes.
- In short: TLAKAN only, read-only, one coordinator per account polling every 5 min. Active alarms drive the PROBLEM binary sensors; alarm history (overlapping fetch, dedupe by id, persisted watermark) drives `alarm`/`status` event entities. Readings are tracked per pump, deduped on `(device_id, pump_number, timestamp)`. `total_time` uses `TOTAL`, `switch_count` uses `TOTAL_INCREASING`, and the alarm counters get no state class.
- Match alarms on `(alarm_type, alarm_number)`; numbers repeat across types. Alarm names come from translation keys per code, because NOM's texts are Czech only.
- One HA device per NOM device. Other device families get added only once real fixtures exist for them.
- Known v0.1 limitation: entities are created only at setup, so a device added to the account later needs a reload (documented in README). Adding them automatically is the Gold-scale `dynamic-devices` rule, the planned next improvement.
- Entity mapping comes from **real fixtures** in `tests/fixtures/` (sanitized), not from the spec alone. The NOM docs say they are "still in progress".
- **Read-only.** Device controls (pump, horn on/off) exist on the website as GET routes with side effects, and have no confirmed REST equivalent. They are **future capabilities** (docs/design.md §9): don't implement or execute them. The client calls only allowlisted read endpoints, never follows redirects, and never retries anything that could be a command.
- Quality target: HA integration quality scale **Silver** for v0.1, Gold after.

## NOM API essentials

- `https://nom.noriatechnology.cz/api` (test: `https://nomtest.noriatechnology.cz/api`); HTTP Basic auth; list endpoints require `limit` and `offset`; `204` = no data.
- Discovery `GET /devices`. Readings `GET /devices/{id}/readings` (newest first; take the newest row per pump, since `limit=1` isn't enough on 2-pump controllers). Alarms `GET /devices/{id}/alarms/active`, plus history `GET /devices/{id}/alarms?from=` (filters on `created_at`) and `GET /devices/{id}/alarms/{alarm_id}`.
- In the HACS workspace, the `noria-api` skill has the full spec, field meanings per device family, a read-only probe CLI (`.claude/skills/noria-api/scripts/nom.py`), and `live-findings.md` with what the real API returns where it differs from the spec.

## Conventions

- Python 3.14, HA 2026.x APIs: `entry.runtime_data`, `type NoriaConfigEntry = ConfigEntry[NoriaCoordinator]`, entity descriptions, `AddConfigEntryEntitiesCallback`, `translation_key` names, `icons.json`.
- `ruff check` + `ruff format` (config in `pyproject.toml`), type hints everywhere, module and class docstrings.
- Tests with `pytest-homeassistant-custom-component`; mock `NoriaClient`, not HTTP, except in `test_api.py` (`aioclient_mock`). Config flow at 100% coverage. Reconciliation scenarios go in `tests/test_alarms.py` against the `FakeNom` model of the real endpoints. Multi-pump behaviour is only tested with synthetic data, so label it as such.
- `.env` holds real credentials (`API_USER`, `API_PASSWORD`). It's gitignored; never print it or commit it.

## Commands (from the workspace root)

```bash
scripts/check.sh ha-noria                 # ruff + pytest + manifest + hassfest
scripts/run_ha.sh                         # dev HA at :8123 with noria linked in
cd ha-noria && ../.venv/bin/pytest --cov --cov-report=term-missing
```

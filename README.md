# Noria Online Monitoring for Home Assistant

Custom integration (installable through HACS) for [Noria Online Monitoring](https://noriatechnology.cz) (NOM). It monitors **TLAKAN** pressure-sewer pump station controllers.

**Read-only.** The integration only reads data from the NOM REST API. It never sends commands (pump, horn) to your devices.

## What you get

For each TLAKAN controller, a device with:

- **Sensors:**
  - pumping time (total, in hours) and pump cycles;
  - active alarms;
  - average and reference pump time;
  - last message and subscription expiration;
  - diagnostic counters for pump failures and emergency levels.
- **Problem binary sensors:** any problem, pump failure, pump damage risk, emergency level (plain, float error and probe contamination), probe contamination and ZA switch. They're on while NOM lists the alarm as active.
- **Event entities:**
  - **Alarm** fires when an alarm starts and when it ends, including alarms that come and go between two polls.
  - **Status** fires for device statuses such as power connected, cleaning or NB-IoT re-registration.

  Both carry `code`, `pump_number`, timestamps, and a `delayed` flag for events that happened while Home Assistant was offline.

On controllers with two pumps, per-pump values appear as "Pump 1 …" and "Pump 2 …".

Data is polled every 5 minutes. TLAKAN controllers report about hourly, and alarms are sent immediately.

## Installation

1. In HACS, add this repository as a custom repository (type *Integration*).
2. Install **Noria Online Monitoring** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Noria Online Monitoring** and enter your NOM API username and password.

## Removal

Delete the integration under **Settings → Devices & services**, then remove it in HACS.

## Troubleshooting

- Download diagnostics from the integration's menu. Personal data (names, addresses, serial numbers, e-mails) is redacted.
- Debug logging: add `custom_components.noria: debug` under `logger:`.
- Alarm texts from NOM are Czech only. The integration shows translated names based on the alarm code instead.

## Known limitations

- Only TLAKAN controllers are supported for now. Water meters (WMETER, QI) and power meters are detected but skipped.
- A controller added to your NOM account later doesn't get entities until you reload the integration (⋮ → Reload on the integration page).
- Event entities show "Unknown" until their first event after setup. Existing alarm history is not replayed when you first add the integration.
- Device controls are not available. NOM's documented API doesn't offer them, and the website routes that do aren't a supported interface.

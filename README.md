# Hive Local Thermostat (Forked from RETIRED 2.0.8) - v2.1

Local Hive Thermostat MQTT integration for Home Assistant

---

## 🙏 A huge thank you to the original developer

A massive thank you to [andrew-codechimp](https://github.com/andrew-codechimp) for creating and maintaining the original [HA-Hive-Local-Thermostat](https://github.com/andrew-codechimp/HA-Hive-Local-Thermostat) integration. This fork would not exist without their excellent work. All credit for the core integration goes to them.

---

## ⚠️ This integration was forked with some patches that I need for my SLT2C system

## Instructions

To use this integration your Hive thermostat receiver must be added to [Zigbee2MQTT](https://www.zigbee2mqtt.io/supported-devices/#v=Hive) and an MQTT broker and the MQTT integration within Home Assistant must be correctly configured. It is important to follow the pairing steps within the Zigbee2MQTT documentation, the thermostat remote control must be paired directly to the thermostat receiver.

Zigbee2MQTT will expose the native sensors but Hive requires specific message structures to be sent for setting modes and a combination of sensor values to determine the modes, this integration creates controls and sensors that correctly interface with the native Hive values/methods.

SLR1x, SLR2x, SLT6b and OTR1 thermostats are supported, this has been tested with an SLR2d, SLR2c, SLR1c and OTR1. If you have thoroughly tested a different model please let me know and I'll add it to the list of confirmed devices. As long as you have one of these receivers this integration will work with either the Hive mini or regular controller.

Once you have the thermostat receiver added to Zigbee2MQTT, add a device via this integration and specify a friendly name, the Zigbee2MQTT topic which should look something like this `zigbee2mqtt/HiveReceiver` (note this is case sensitive).

The new device created will have new sensors/controls available that will accurately show/send status changes.

You can optionally hide/disable the native Hive device created by Zigbee2MQTT within HomeAssistant.

The integration supports native boost and native schedules (auto). With schedules you can switch on/off schedule mode, and read/write the schedule's actual contents (times and temperatures) via the `get_weekly_schedule`/`set_weekly_schedule` actions described below. You can of course still ignore the native schedule entirely and set up automations within Home Assistant instead to control when heating/water is on or off and set a temperature for heating.

The numeric entities allow you to set defaults for boost times, heating boost temperature and also frost protection. Frost protection should be set to match what you have set on the Hive thermostat for an accurate display.

Actions are provided to natively boost the Heating `hive_local_thermostat.boost_heating` and Water `hive_local_thermostat.boost_water` (SLR2 only), these can optionally take a duration and temperature (heating only), these actions allow you to make custom buttons/scripts/automations to add additional control over the default boost buttons.

There are also matching actions to cancel the native boost for Heating `hive_local_thermostat.cancel_boost_heating` and Water `hive_local_thermostat.cancel_boost_water` (SLR2 only), these actions will return the heating/water back to the state they were before the boost.

### Weekly schedule (SLR2)

The SLR2 answers the standard Zigbee thermostat cluster's Get/Set Weekly Schedule commands, so this fork exposes that directly rather than only letting you toggle Auto mode:

- `hive_local_thermostat.get_weekly_schedule` (`zone`: `heat` or `water`, defaults to `heat`) requests the device report its schedule. The reply arrives asynchronously - sometimes as several messages, one per group of days that share an identical programme - and is merged into the `sensor.*_weekly_schedule_heat`/`sensor.*_weekly_schedule_water` diagnostic entity's `schedule` attribute as it comes in. Nothing is populated until you call this at least once; it isn't polled automatically.
- `hive_local_thermostat.set_weekly_schedule` (`zone`, `days`: list of weekday names, `transitions`: list of transition objects, up to 6 in chronological order) writes a programme to the device for the given day(s). Each transition is `{time: "HH:MM", ...}`:
  - **heat**: `temperature: <°C>` for a real target, or `state: "off"` for a setback period.
  - **water**: `state: "on"` or `state: "off"` (it's an on/off relay, no temperature).

  This is the same shape the schedule sensor renders, so a day you read back via `get_weekly_schedule` can be pasted straight into `set_weekly_schedule`.

This talks directly to the device over MQTT rather than through this integration's usual coordinator state, so it bypasses the hold/mode logic used elsewhere - test with a single day/transition first and confirm it round-trips (`set_weekly_schedule` then `get_weekly_schedule`) before relying on it.

#### Editing the schedule from the UI (no YAML)

If you'd rather not call the action by hand, the integration's own **Configure** dialog has a guided editor:

Settings → Devices & Services → **Hive Local Thermostat** → the ⚙ **Configure** button → **Manage schedules**.

The **Manage schedules** menu holds **Edit heating schedule**, **Clear heating schedule**, and (SLR2) the matching water actions. (The top-level menu also has **General settings** — MQTT topic / model / schedule-mode options — as before.)

**Editing** is two steps:

1. **Who** — choose *Apply to* (a preset: **Every day**, **Weekdays**, **Weekend**, or **Custom days**), and optionally **Load times from** an existing day to pre-fill the next screen. Loading a day and applying it to others is how you **copy one day onto several** (e.g. load Monday, apply to Weekdays, submit unchanged).
2. **What** — up to six rows of time (24-hour `HH:MM`) plus, for heating, a temperature or an *Off / setback* tick; for water a simple *On* tick. A row is used only if it has a time.

Each submit **overwrites the whole day** on the device for the days you chose, so enter the full day's programme. For a different weekend, do it in two passes (Weekdays, then Weekend).

**Clearing** (Clear heating / Clear water) overwrites the chosen days with a single all-day-off period — a quick way to wipe a zone's schedule for some or all days.

#### Viewing the schedule

The `sensor.*_weekly_schedule_heat` / `_water` diagnostic entities carry three attributes:

- `schedule_text` - each day as one readable line, e.g. `06:30 21°C · 09:00 off · 17:00 20°C · 22:00 off`. This is the one to look at.
- `schedule` - the same data structured per transition (`{time, state, temperature}`), for templating.
- `raw_schedule` - the untouched device values, for feeding back into `set_weekly_schedule` if you prefer raw.

A Markdown card gives you a readable week at a glance (replace the entity id with yours):

```yaml
type: markdown
content: >-
  {% set s = state_attr('sensor.hivereceiver_weekly_schedule_heat', 'schedule_text') %}
  {% if s %}{% for day, line in s.items() %}
  **{{ day | capitalize }}:** {{ line }}
  {% endfor %}{% else %}No schedule read yet - call `get_weekly_schedule`.{% endif %}
```

Example - to remove an unwanted early-morning heating block on weekdays (leaving a single comfortable evening period), overwrite those days:

```yaml
action: hive_local_thermostat.set_weekly_schedule
data:
  config_entry_id: <your entry id>
  zone: heat
  days: [monday, tuesday, wednesday, thursday, friday]
  transitions:
    - {time: "00:00", state: "off"}
    - {time: "17:00", temperature: 20}
    - {time: "22:00", state: "off"}
```

![Hive Screenshot](https://raw.githubusercontent.com/spants/HA-Hive-Local-Thermostat/main/images/screenshot.png "Hive Controls")

This project is not endorsed by, directly affiliated with, maintained, authorized, or sponsored by Hive.

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=spants&repository=HA-Hive-Local-Thermostat&category=Integration)

Restart Home Assistant

### Manual Installation

<details>
<summary>Show detailed instructions</summary>

Installation via HACS is recommended, but a manual setup is supported.

1. Manually copy custom_components/hive_local_thermostat folder from latest release to custom_components folder in your config folder.
1. Restart Home Assistant.
1. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Hive Local Thermostat"

</details>

## Climate Control Operation

> **Important:** You must use the climate entity created by **this integration**, not the one automatically created by Zigbee2MQTT. If you use the wrong entity your commands will appear to do nothing — no MQTT messages will be sent to the device.
>
> - The correct entity is found under **Settings → Devices & Services → Hive Local Thermostat → your device**. Its entity ID will look like `climate.hive_receiver`.
> - The Zigbee2MQTT discovery entity will have a name ending in `_heat` (e.g. `climate.hivereceiver_heat`) and does **not** have the preset selector for None and Boost.
>
> To avoid confusion, disable the Zigbee2MQTT discovery entity for your Hive receiver. In the Z2M web UI open the device, go to the **Settings** tab and turn off the **Home Assistant** toggle. Alternatively add `homeassistant: false` to the device entry in your Z2M `configuration.yaml`.

The climate control provides 3 modes

- Off - turns the Hive off, it will only ever heat if the temperature is below the Hive thermostats preset frost protection temperature. No schedules you have on the Hive thermostat will activate. **My SLT2c had a problem with setting the system to off on the original code. 
This fork operates in this way: If OFF is chosen, the current temperature is stored and the system is set to HEAT at 7 degs. When HEAT is called, the original temperature is set. If the temperature is changed in the UI while the system is OFF, the integration stores this new temperature but still pretends to be off. When HEAT is called, it will restore to the new temperature**
   
- Heat - turns the Hive on and will heat to the temperature specified and maintain that temperature.
- Auto - turns the Hive to its inbuilt schedule mode, the schedules created on the Hive thermostat will be active, you can adjust the temperature either within this integration or via the Hive thermostat and it will override the temperature until the next scheduled temperature.

The presets provide the facility to boost the heating. Selecting Boost will trigger your predefined boost temperature and duration specified in this integrations device configuration section, selecting None will cancel any active boost.

## FAQ's

- What are running states

  Running states are whether your boiler is actually active, you can see in the above screenshot that my thermostat is on Auto (schedule on the Hive thermostat), the current temperature is above my target temperature therefore the running state is idle. This will change to heating when the current temperature goes below my target and the boiler is triggered to heat.

- My boost remaining sensors are not counting down

  By default Zigbee2MQTT will not send frequent updates to track boosts by the minute, they will update but this is typically every 10 minutes.

  To change this within Zigbee2MQTT go into the receiver's reporting settings and change the following;

  Min Rep Change of any tempSetpointHoldDuration to 1  
  
  If you have heat and water you will have two values under different Endpoints, with heat only you will have one.

- I have changed thermostat settings within Zigbee2MQTT but it does not work properly.

  Zigbee2MQTT's UI exposes the current state of each value in a generic way, it does not have the logic to correctly send/interpret the values required to control the Hive thermostat, the documentation for each thermostat within Zigbee2MQTT explains the messages required to change things, this integration provides a wrapper around all this complexity for you but there is nothing stopping you from creating your own MQTT messages to do this.

- Can this be used with ZHA?

  No, this integration requires sending/receiving messages via MQTT to the Hive thermostat, ZHA does not work like this.

- How do I get beta versions with HACS
  - Within Home Assistant go to Settings -> Integrations -> HACS
  - Select Services
  - Select Hive Local Thermostat
  - In the Diagnostics panel select the +1 entity not shown
  - Select Pre-release
  - Select the cog icon
  - Select Enable
  - Select Update and wait for the entity to be enabled
  - Turn on the Pre-release toggle
  - HACS will now show updates available for pre-releases if there are any

[commits-shield]: https://img.shields.io/github/commit-activity/y/spants/HA-Hive-Local-Thermostat.svg?style=for-the-badge
[commits]: https://github.com/spants/HA-Hive-Local-Thermostat/commits/main
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge
[exampleimg]: example.png
[license-shield]: https://img.shields.io/github/license/spants/HA-Hive-Local-Thermostat.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/spants/HA-Hive-Local-Thermostat.svg?style=for-the-badge
[releases]: https://github.com/spants/HA-Hive-Local-Thermostat/releases
[download-latest-shield]: https://img.shields.io/github/downloads/spants/HA-Hive-Local-Thermostat/latest/total?style=for-the-badge
[hacs-installs-shield]: https://img.shields.io/endpoint.svg?url=https%3A%2F%2Flauwbier.nl%2Fhacs%2Fhive_local_thermostat&style=for-the-badge

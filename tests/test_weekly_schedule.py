"""Unit tests for the weekly-schedule feature.

These cover the pure logic that turns friendly schedule input into the exact
MQTT payloads Zigbee2MQTT expects, the read-merge, the sensor rendering, and
the options-flow helpers - no Home Assistant runtime is set up, entities and
the coordinator are exercised directly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from custom_components.hive_local_thermostat import config_flow as cf, const
from custom_components.hive_local_thermostat.coordinator import HiveCoordinator
from custom_components.hive_local_thermostat.sensor import HiveWeeklyScheduleSensor
from custom_components.hive_local_thermostat.services import _resolve_setpoint

ALL_DAYS = list(const.VALID_SCHEDULE_DAYS)


def _bare_coordinator(model: str) -> HiveCoordinator:
    """A coordinator instance with just enough wired to publish, no HA setup."""
    coordinator = object.__new__(HiveCoordinator)
    coordinator.model = model
    return coordinator


def _capture_set(coordinator: HiveCoordinator) -> list[dict[str, Any]]:
    """Patch _async_publish_set to record the (parsed) payloads it would send."""
    sent: list[dict[str, Any]] = []

    async def _fake(payload: str) -> None:
        sent.append(json.loads(payload))

    coordinator._async_publish_set = _fake  # type: ignore[method-assign]
    return sent


# --- coordinator: write translation to Z2M's converter shape -----------------


@pytest.mark.asyncio
async def test_set_weekly_schedule_water_payload() -> None:
    """Water write maps to dayofweek/transitionTime/heatSetpoint on the water key."""
    coordinator = _bare_coordinator(const.MODEL_SLR2)
    sent = _capture_set(coordinator)

    await coordinator.async_set_weekly_schedule(
        const.ZONE_WATER,
        ALL_DAYS,
        [
            {"time": 0, "heating_setpoint": 0},
            {"time": 1020, "heating_setpoint": 99},
            {"time": 1080, "heating_setpoint": 0},
        ],
    )

    assert sent == [
        {
            "weekly_schedule_water": {
                "dayofweek": ALL_DAYS,
                "transitions": [
                    {"transitionTime": "00:00", "heatSetpoint": 0},
                    {"transitionTime": "17:00", "heatSetpoint": 99},
                    {"transitionTime": "18:00", "heatSetpoint": 0},
                ],
            }
        }
    ]


@pytest.mark.asyncio
async def test_set_weekly_schedule_heat_payload() -> None:
    """Heat write (the path never tested on hardware) uses the same translation."""
    coordinator = _bare_coordinator(const.MODEL_SLR2)
    sent = _capture_set(coordinator)

    await coordinator.async_set_weekly_schedule(
        const.ZONE_HEAT,
        ["monday"],
        [
            {"time": 390, "heating_setpoint": 20.0},
            {"time": 540, "heating_setpoint": const.WEEKLY_SCHEDULE_OFF_SETPOINT},
        ],
    )

    assert sent[0] == {
        "weekly_schedule_heat": {
            "dayofweek": ["monday"],
            "transitions": [
                {"transitionTime": "06:30", "heatSetpoint": 20.0},
                {"transitionTime": "09:00", "heatSetpoint": 1},
            ],
        }
    }


@pytest.mark.asyncio
async def test_set_weekly_schedule_single_endpoint_key() -> None:
    """SLR1/OTR1 have one endpoint, so the key has no zone suffix."""
    coordinator = _bare_coordinator(const.MODEL_SLR1)
    sent = _capture_set(coordinator)

    await coordinator.async_set_weekly_schedule(
        const.ZONE_HEAT, ["monday"], [{"time": 0, "heating_setpoint": 18.0}]
    )

    assert "weekly_schedule" in sent[0]


@pytest.mark.asyncio
async def test_set_water_on_non_slr2_is_rejected() -> None:
    """Water isn't available on heating-only models - nothing is published."""
    coordinator = _bare_coordinator(const.MODEL_SLR1)
    sent = _capture_set(coordinator)

    await coordinator.async_set_weekly_schedule(
        const.ZONE_WATER, ["monday"], [{"time": 0, "heating_setpoint": 0}]
    )

    assert sent == []


@pytest.mark.asyncio
async def test_clear_weekly_schedule_payload_and_cache() -> None:
    """Clear sends the native command, resets the cache, and re-reads."""
    coordinator = _bare_coordinator(const.MODEL_SLR2)
    coordinator.weekly_schedule_water = {"monday": [{"time": 1020, "heating_setpoint": 99}]}
    sent = _capture_set(coordinator)
    calls: list[str] = []

    async def _fake_get(zone: str) -> None:
        calls.append(zone)

    coordinator.async_get_weekly_schedule = _fake_get  # type: ignore[method-assign]
    coordinator._save_weekly_schedule = lambda: None  # type: ignore[method-assign]
    coordinator.async_update_listeners = lambda: None  # type: ignore[method-assign]

    await coordinator.async_clear_weekly_schedule(const.ZONE_WATER)

    assert sent == [{"clear_weekly_schedule_water": ""}]
    assert coordinator.weekly_schedule_water == {}
    assert calls == [const.ZONE_WATER]


# --- coordinator: read merge accumulation ------------------------------------


def test_merge_weekly_schedule_copies_per_day() -> None:
    """Days in one response must not share a mutable transitions list."""
    merged = HiveCoordinator._merge_weekly_schedule(
        None,
        {"days": ["monday", "tuesday"], "transitions": [{"time": 0, "heating_setpoint": 20}]},
    )

    merged["monday"].append({"time": 600, "heating_setpoint": 18})

    assert len(merged["monday"]) == 2
    assert len(merged["tuesday"]) == 1  # sibling untouched


def test_merge_weekly_schedule_accumulates_across_responses() -> None:
    """A later response for other days extends rather than replaces the store."""
    first = HiveCoordinator._merge_weekly_schedule(
        None, {"days": ["monday"], "transitions": [{"time": 0, "heating_setpoint": 20}]}
    )
    second = HiveCoordinator._merge_weekly_schedule(
        first, {"days": ["tuesday"], "transitions": [{"time": 0, "heating_setpoint": 18}]}
    )

    assert set(second) == {"monday", "tuesday"}


# --- sensor: state summary + rendering ---------------------------------------


def _water_sensor() -> HiveWeeklyScheduleSensor:
    sensor = object.__new__(HiveWeeklyScheduleSensor)
    sensor._zone = const.ZONE_WATER
    return sensor


def test_native_summary_uniform_week() -> None:
    text = {day: "00:00 off · 17:00 on · 18:00 off" for day in ALL_DAYS}
    assert HiveWeeklyScheduleSensor._native_summary(text) == "00:00 off · 17:00 on · 18:00 off"


def test_native_summary_partial_uniform() -> None:
    text = {day: "17:00 on · 18:00 off" for day in const.VALID_SCHEDULE_DAYS[:5]}
    assert HiveWeeklyScheduleSensor._native_summary(text) == "17:00 on · 18:00 off (5/7 days)"


def test_native_summary_varies() -> None:
    text = {day: "17:00 on · 18:00 off" for day in ALL_DAYS}
    text["saturday"] = "09:00 on · 11:00 off"
    assert HiveWeeklyScheduleSensor._native_summary(text) == "7/7 days, varies"


def test_native_summary_too_long_falls_back() -> None:
    text = {day: "x" * 300 for day in ALL_DAYS}
    assert HiveWeeklyScheduleSensor._native_summary(text) == "7/7 days, varies"


def test_format_schedule_orders_days_and_sorts_transitions() -> None:
    sensor = _water_sensor()
    raw = {
        "tuesday": [{"time": 1080, "heating_setpoint": 0}, {"time": 1020, "heating_setpoint": 99}],
        "monday": [{"time": 1020, "heating_setpoint": 99}],
    }
    formatted = sensor._format_schedule(raw)
    assert list(formatted) == ["monday", "tuesday"]  # Mon before Tue
    assert [t["time"] for t in formatted["tuesday"]] == ["17:00", "18:00"]  # sorted


def test_format_transition_water_and_heat() -> None:
    water = _water_sensor()
    assert water._format_transition(const.WATER_SCHEDULE_ON_SETPOINT) == {"state": "on"}
    assert water._format_transition(const.WATER_SCHEDULE_OFF_SETPOINT) == {"state": "off"}

    heat = object.__new__(HiveWeeklyScheduleSensor)
    heat._zone = const.ZONE_HEAT
    assert heat._format_transition(const.WEEKLY_SCHEDULE_OFF_SETPOINT) == {"state": "off"}
    assert heat._format_transition(20.0) == {"state": "on", "temperature": 20.0}


# --- services: friendly setpoint resolution ----------------------------------


def test_resolve_setpoint_water_on_off() -> None:
    assert _resolve_setpoint({"state": "on"}, const.ZONE_WATER) == float(
        const.WATER_SCHEDULE_ON_SETPOINT
    )
    assert _resolve_setpoint({"state": "off"}, const.ZONE_WATER) == float(
        const.WATER_SCHEDULE_OFF_SETPOINT
    )


def test_resolve_setpoint_heat_temperature_and_off() -> None:
    assert _resolve_setpoint({"temperature": 21}, const.ZONE_HEAT) == 21.0
    assert _resolve_setpoint({"state": "off"}, const.ZONE_HEAT) == float(
        const.WEEKLY_SCHEDULE_OFF_SETPOINT
    )


@pytest.mark.parametrize(
    ("item", "zone"),
    [
        ({"state": "maybe"}, const.ZONE_WATER),  # unrecognised water state
        ({"state": "on"}, const.ZONE_HEAT),  # heat "on" needs a temperature
        ({}, const.ZONE_HEAT),  # nothing given
    ],
)
def test_resolve_setpoint_invalid(item: dict[str, Any], zone: str) -> None:
    with pytest.raises((ValueError, KeyError, TypeError)):
        _resolve_setpoint(item, zone)


# --- config_flow: day resolution, transitions, prefill, menu -----------------


def test_resolve_days_presets() -> None:
    assert cf._resolve_days({"days_preset": cf.PRESET_EVERY_DAY}) == ALL_DAYS
    assert cf._resolve_days({"days_preset": cf.PRESET_WEEKDAYS}) == list(cf.WEEKDAY_DAYS)
    assert cf._resolve_days({"days_preset": cf.PRESET_WEEKEND}) == list(cf.WEEKEND_DAYS)
    assert cf._resolve_days({}) == ALL_DAYS  # default is every day


def test_resolve_days_custom() -> None:
    resolved = cf._resolve_days(
        {"days_preset": cf.PRESET_CUSTOM, "days": ["monday", "wednesday"]}
    )
    assert resolved == ["monday", "wednesday"]


def test_resolve_days_custom_empty_errors() -> None:
    with pytest.raises(cf.SchemaFlowError):
        cf._resolve_days({"days_preset": cf.PRESET_CUSTOM})


def test_build_transitions_water() -> None:
    result = cf._build_transitions(
        const.ZONE_WATER,
        {"time_1": "17:00", "on_1": True, "time_2": "18:00", "on_2": False},
    )
    assert result == [
        {"time": 1020, "heating_setpoint": const.WATER_SCHEDULE_ON_SETPOINT},
        {"time": 1080, "heating_setpoint": const.WATER_SCHEDULE_OFF_SETPOINT},
    ]


def test_build_transitions_heat_off_and_temp_sorted() -> None:
    result = cf._build_transitions(
        const.ZONE_HEAT,
        {
            "time_1": "09:00",
            "off_1": True,
            "time_2": "06:30",
            "temperature_2": 20.0,
            "off_2": False,
        },
    )
    # sorted by time: 06:30 first
    assert result[0] == {"time": 390, "heating_setpoint": 20.0}
    assert result[1] == {"time": 540, "heating_setpoint": const.WEEKLY_SCHEDULE_OFF_SETPOINT}


def test_build_transitions_none_errors() -> None:
    with pytest.raises(cf.SchemaFlowError):
        cf._build_transitions(const.ZONE_WATER, {})


@pytest.mark.parametrize("bad_time", ["25:00", "abc", "1700"])
def test_build_transitions_invalid_time(bad_time: str) -> None:
    with pytest.raises(cf.SchemaFlowError):
        cf._build_transitions(const.ZONE_WATER, {"time_1": bad_time, "on_1": True})


def test_prefill_from_transitions_water() -> None:
    prefill = cf._prefill_from_transitions(
        const.ZONE_WATER,
        [
            {"time": 1080, "heating_setpoint": 0},
            {"time": 390, "heating_setpoint": 99},
        ],
    )
    # sorted, so the 06:30 on lands in slot 1
    assert prefill["time_1"] == "06:30"
    assert prefill["on_1"] is True
    assert prefill["time_2"] == "18:00"
    assert prefill["on_2"] is False


def test_prefill_from_transitions_heat() -> None:
    prefill = cf._prefill_from_transitions(
        const.ZONE_HEAT,
        [
            {"time": 0, "heating_setpoint": const.WEEKLY_SCHEDULE_OFF_SETPOINT},
            {"time": 390, "heating_setpoint": 20.0},
        ],
    )
    assert prefill["off_1"] is True
    assert "temperature_1" not in prefill
    assert prefill["temperature_2"] == 20.0


def test_build_options_flow_model_gating() -> None:
    slr2 = cf.build_options_flow(const.MODEL_SLR2)
    slr1 = cf.build_options_flow(const.MODEL_SLR1)

    assert slr2["manage_schedules"].options == [
        "heat_schedule",
        "clear_heat",
        "water_schedule",
        "clear_water",
    ]
    assert slr1["manage_schedules"].options == ["heat_schedule", "clear_heat"]
    # both editors funnel into the shared rows step, which ends on the sent page
    assert slr2["heat_schedule"].next_step == "schedule_rows"
    assert slr2["schedule_rows"].next_step == "schedule_sent"
    assert slr2["schedule_sent"].next_step is None


def test_load_from_options() -> None:
    schedule = {"wednesday": [{"time": 1020, "heating_setpoint": 99}]}
    values = [o["value"] for o in cf._load_from_options(schedule)]
    assert values == [cf.LOAD_BLANK, "wednesday"]
    assert [o["value"] for o in cf._load_from_options(None)] == [cf.LOAD_BLANK]

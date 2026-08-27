"""Adds config flow for Hive Local Thermostat."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaConfigFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
    SchemaOptionsFlowHandler,
)

from . import const

if TYPE_CHECKING:
    from .common import HiveData
    from .coordinator import HiveCoordinator


def required(
    key: str, options: dict[str, Any], default: Any | None = None
) -> vol.Required:
    """Return vol.Required."""
    if isinstance(options, dict) and key in options:
        suggested_value = options[key]
    elif default is not None:
        suggested_value = default
    else:
        return vol.Required(key)
    return vol.Required(key, description={"suggested_value": suggested_value})


def optional(
    key: str, options: dict[str, Any], default: Any | None = None
) -> vol.Optional:
    """Return vol.Optional."""
    if isinstance(options, dict) and key in options:
        suggested_value = options[key]
    elif default is not None:
        suggested_value = default
    else:
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": suggested_value})


async def general_options_schema(
    handler: SchemaConfigFlowHandler | SchemaOptionsFlowHandler,
) -> vol.Schema:
    """Generate options schema."""
    return vol.Schema(
        {
            required(const.CONF_MQTT_TOPIC, handler.options): selector.TextSelector(),
            required(const.CONF_MODEL, handler.options): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=const.MODELS,
                    translation_key="model",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ),
            ),
            required(
                const.CONF_SHOW_HEAT_SCHEDULE_MODE, handler.options, default=True
            ): selector.BooleanSelector(
                selector.BooleanSelectorConfig(),
            ),
            required(
                const.CONF_SHOW_WATER_SCHEDULE_MODE, handler.options, default=True
            ): selector.BooleanSelector(
                selector.BooleanSelectorConfig(),
            ),
        }
    )


async def general_config_schema(
    handler: SchemaConfigFlowHandler | SchemaOptionsFlowHandler,
) -> vol.Schema:
    """Generate config schema."""
    return vol.Schema(
        {
            required(CONF_NAME, handler.options): selector.TextSelector(),
            required(const.CONF_MQTT_TOPIC, handler.options): selector.TextSelector(),
            required(const.CONF_MODEL, handler.options): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=const.MODELS,
                    translation_key="model",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ),
            ),
            required(
                const.CONF_SHOW_HEAT_SCHEDULE_MODE, handler.options, default=True
            ): selector.BooleanSelector(
                selector.BooleanSelectorConfig(),
            ),
            required(
                const.CONF_SHOW_WATER_SCHEDULE_MODE, handler.options, default=True
            ): selector.BooleanSelector(
                selector.BooleanSelectorConfig(),
            ),
        }
    )


SCHEDULE_DATA_DAYS = "days"
SCHEDULE_DATA_PRESET = "days_preset"
SCHEDULE_DATA_LOAD_FROM = "load_from"

# "Load times from" sentinel meaning "don't pre-fill, start with empty rows".
LOAD_BLANK = "blank"

# Day-preset choices for the "Apply to" control. Every day is the default so a
# user who ignores it still writes a full week (matches most real schedules).
PRESET_EVERY_DAY = "every_day"
PRESET_WEEKDAYS = "weekdays"
PRESET_WEEKEND = "weekend"
PRESET_CUSTOM = "custom"

WEEKDAY_DAYS = const.VALID_SCHEDULE_DAYS[:5]
WEEKEND_DAYS = const.VALID_SCHEDULE_DAYS[5:]

# flow_state keys carrying the setup step's choices through to the rows step.
FLOW_ZONE = "schedule_zone"
FLOW_DAYS = "schedule_days"
FLOW_PREFILL = "schedule_prefill"

# Schedule wizard error keys (resolved via translations/en.json options.error).
ERROR_NO_TRANSITIONS = "no_transitions"
ERROR_TEMPERATURE_REQUIRED = "temperature_required"
ERROR_INVALID_TIME = "invalid_time"
ERROR_NO_DAYS = "no_days"


def _day_options() -> list[selector.SelectOptionDict]:
    """Weekday options for the schedule wizard, labelled Monday..Sunday."""
    return [
        selector.SelectOptionDict(value=day, label=day.capitalize())
        for day in const.VALID_SCHEDULE_DAYS
    ]


def _preset_options() -> list[selector.SelectOptionDict]:
    """'Apply to' presets that expand to a fixed set of days."""
    return [
        selector.SelectOptionDict(value=PRESET_EVERY_DAY, label="Every day"),
        selector.SelectOptionDict(value=PRESET_WEEKDAYS, label="Weekdays (Mon–Fri)"),
        selector.SelectOptionDict(value=PRESET_WEEKEND, label="Weekend (Sat–Sun)"),
        selector.SelectOptionDict(value=PRESET_CUSTOM, label="Custom days…"),
    ]


def _minutes_to_hhmm(minutes: int) -> str:
    """Render minutes-since-midnight as a 24-hour HH:MM string."""
    hours, mins = divmod(int(minutes), 60)
    return f"{hours:02d}:{mins:02d}"


def _parse_time_to_minutes(value: str) -> int:
    """Convert an 'HH:MM' (24-hour) string to minutes since midnight.

    Uses HA's own time validator, which accepts HH:MM (and HH:MM:SS) and
    rejects anything that isn't a real time of day. Raises vol.Invalid on bad
    input, which the caller turns into a friendly form error.
    """
    parsed = cv.time(value.strip())
    return parsed.hour * 60 + parsed.minute


def _coordinator(handler: SchemaCommonFlowHandler) -> HiveCoordinator:
    """Fetch the running coordinator for the config entry being configured."""
    options_handler = cast(SchemaOptionsFlowHandler, handler.parent_handler)
    entry = options_handler.config_entry
    return cast("HiveData", entry.runtime_data).coordinator


def _zone_schedule(
    coordinator: HiveCoordinator, zone: str
) -> dict[str, list[dict[str, Any]]] | None:
    """Return the coordinator's last-known schedule for a zone (may be None)."""
    if zone == const.ZONE_WATER:
        return coordinator.weekly_schedule_water
    return coordinator.weekly_schedule_heat


def _resolve_days(user_input: dict[str, Any]) -> list[str]:
    """Expand the 'Apply to' preset (or custom multiselect) into concrete days.

    A preset always wins; the custom multiselect is consulted only for Custom,
    and an empty custom selection is an error rather than a silent fall-back to
    every day.
    """
    preset = user_input.get(SCHEDULE_DATA_PRESET, PRESET_EVERY_DAY)
    if preset == PRESET_EVERY_DAY:
        return list(const.VALID_SCHEDULE_DAYS)
    if preset == PRESET_WEEKDAYS:
        return list(WEEKDAY_DAYS)
    if preset == PRESET_WEEKEND:
        return list(WEEKEND_DAYS)
    days = user_input.get(SCHEDULE_DATA_DAYS) or []
    if not days:
        raise SchemaFlowError(ERROR_NO_DAYS)
    return cast(list[str], days)


def _suggest(key: str, suggested: dict[str, Any]) -> vol.Optional:
    """Return an Optional marker, pre-filled with a suggested value when we have one."""
    if key in suggested:
        return vol.Optional(key, description={"suggested_value": suggested[key]})
    return vol.Optional(key)


def _suggest_bool(key: str, suggested: dict[str, Any]) -> vol.Optional:
    """Return an Optional boolean marker (defaults to off), pre-filled when present."""
    if key in suggested:
        return vol.Optional(
            key, default=False, description={"suggested_value": suggested[key]}
        )
    return vol.Optional(key, default=False)


# --- "Apply to" (preset + custom days), shared by the editor and clear steps ---


def _apply_to_markers() -> dict[Any, Any]:
    """Return the preset dropdown + custom-day multiselect block."""
    return {
        vol.Required(
            SCHEDULE_DATA_PRESET, default=PRESET_EVERY_DAY
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_preset_options(),
                mode=selector.SelectSelectorMode.DROPDOWN,
            ),
        ),
        vol.Optional(SCHEDULE_DATA_DAYS): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_day_options(),
                multiple=True,
                mode=selector.SelectSelectorMode.LIST,
            ),
        ),
    }


# --- Step 1: setup (who to apply to + which day to load times from) ---


def _load_from_options(
    schedule: dict[str, list[dict[str, Any]]] | None,
) -> list[selector.SelectOptionDict]:
    """Blank plus every day that currently has a schedule to copy from."""
    options = [
        selector.SelectOptionDict(value=LOAD_BLANK, label="Blank (start empty)"),
    ]
    if schedule:
        options.extend(
            selector.SelectOptionDict(value=day, label=day.capitalize())
            for day in const.VALID_SCHEDULE_DAYS
            if day in schedule
        )
    return options


def _setup_schema(schedule: dict[str, list[dict[str, Any]]] | None) -> vol.Schema:
    """Build the setup form: apply-to preset/days plus an optional 'load times from' day."""
    schema = _apply_to_markers()
    schema[vol.Optional(SCHEDULE_DATA_LOAD_FROM, default=LOAD_BLANK)] = (
        selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_load_from_options(schedule),
                mode=selector.SelectSelectorMode.DROPDOWN,
            ),
        )
    )
    return vol.Schema(schema)


async def heat_setup_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Build the heating editor setup schema, load-from list from device state."""
    schedule = _zone_schedule(_coordinator(handler), const.ZONE_HEAT)
    return _setup_schema(schedule)


async def water_setup_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Build the water editor setup schema, load-from list from device state."""
    schedule = _zone_schedule(_coordinator(handler), const.ZONE_WATER)
    return _setup_schema(schedule)


def _prefill_from_transitions(
    zone: str, transitions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Turn a source day's transitions into suggested values for the row fields."""
    suggested: dict[str, Any] = {}
    ordered = sorted(transitions, key=lambda t: t["time"])
    for index, transition in enumerate(
        ordered[: const.MAX_SCHEDULE_TRANSITIONS], start=1
    ):
        suggested[f"time_{index}"] = _minutes_to_hhmm(transition["time"])
        setpoint = transition["heating_setpoint"]
        if zone == const.ZONE_WATER:
            suggested[f"on_{index}"] = setpoint == const.WATER_SCHEDULE_ON_SETPOINT
        elif setpoint == const.WEEKLY_SCHEDULE_OFF_SETPOINT:
            suggested[f"off_{index}"] = True
        else:
            suggested[f"temperature_{index}"] = float(setpoint)
    return suggested


async def _store_setup(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any], zone: str
) -> dict[str, Any]:
    """Resolve the setup choices and stash them for the rows step in flow_state."""
    days = _resolve_days(user_input)
    load_from = user_input.get(SCHEDULE_DATA_LOAD_FROM, LOAD_BLANK)
    prefill: dict[str, Any] = {}
    if load_from != LOAD_BLANK:
        schedule = _zone_schedule(_coordinator(handler), zone) or {}
        prefill = _prefill_from_transitions(zone, schedule.get(load_from) or [])
    handler.flow_state[FLOW_ZONE] = zone
    handler.flow_state[FLOW_DAYS] = days
    handler.flow_state[FLOW_PREFILL] = prefill
    return {}


async def validate_heat_setup(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store the heating editor setup, then advance to the rows step."""
    return await _store_setup(handler, user_input, const.ZONE_HEAT)


async def validate_water_setup(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store the water editor setup, then advance to the rows step."""
    return await _store_setup(handler, user_input, const.ZONE_WATER)


# --- Step 2: rows (the actual transitions, pre-filled if a day was loaded) ---


def _rows_schema(zone: str, suggested: dict[str, Any]) -> vol.Schema:
    """Up to six transition rows, pre-filled from `suggested` where present.

    A row is used only if its time is filled in; heat rows carry a temperature
    plus an off/setback toggle, water rows a single on/off toggle.
    """
    schema: dict[Any, Any] = {}
    for index in range(1, const.MAX_SCHEDULE_TRANSITIONS + 1):
        # A plain HH:MM text box rather than a TimeSelector: the device works
        # in whole minutes, and TimeSelector has no way to hide its seconds
        # field, which is just noise here.
        schema[_suggest(f"time_{index}", suggested)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT),
        )
        if zone == const.ZONE_WATER:
            schema[_suggest_bool(f"on_{index}", suggested)] = selector.BooleanSelector()
        else:
            schema[_suggest(f"temperature_{index}", suggested)] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=32,
                        step=0.5,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="°C",
                    ),
                )
            )
            schema[_suggest_bool(f"off_{index}", suggested)] = (
                selector.BooleanSelector()
            )
    return vol.Schema(schema)


async def schedule_rows_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Rows schema for whichever zone the setup step selected."""
    zone = handler.flow_state.get(FLOW_ZONE, const.ZONE_HEAT)
    prefill = handler.flow_state.get(FLOW_PREFILL, {})
    return _rows_schema(zone, prefill)


def _build_transitions(zone: str, user_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the wizard's per-row fields into device transitions, sorted by time.

    Raises SchemaFlowError (shown on the form) if nothing was entered or a heat
    row is left on with no temperature.
    """
    transitions: list[dict[str, Any]] = []
    for index in range(1, const.MAX_SCHEDULE_TRANSITIONS + 1):
        raw_time = user_input.get(f"time_{index}")
        if not raw_time:
            continue
        try:
            minutes = _parse_time_to_minutes(raw_time)
        except vol.Invalid as err:
            raise SchemaFlowError(ERROR_INVALID_TIME) from err

        if zone == const.ZONE_WATER:
            setpoint: float = (
                const.WATER_SCHEDULE_ON_SETPOINT
                if user_input.get(f"on_{index}")
                else const.WATER_SCHEDULE_OFF_SETPOINT
            )
        elif user_input.get(f"off_{index}"):
            setpoint = const.WEEKLY_SCHEDULE_OFF_SETPOINT
        else:
            temperature = user_input.get(f"temperature_{index}")
            if temperature is None:
                raise SchemaFlowError(ERROR_TEMPERATURE_REQUIRED)
            setpoint = float(temperature)

        transitions.append({"time": minutes, "heating_setpoint": setpoint})

    if not transitions:
        raise SchemaFlowError(ERROR_NO_TRANSITIONS)

    transitions.sort(key=lambda transition: transition["time"])
    return transitions


async def write_schedule_rows(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Write the edited rows to the days chosen back in the setup step.

    Returns an empty dict so none of the transient row fields are persisted
    into the config entry options - the device is the source of truth.
    """
    zone = cast(str, handler.flow_state[FLOW_ZONE])
    days = cast(list[str], handler.flow_state[FLOW_DAYS])
    transitions = _build_transitions(zone, user_input)
    coordinator = _coordinator(handler)
    await coordinator.async_set_weekly_schedule(zone, days, transitions)
    # Re-read straight away so the Weekly schedule sensor reflects what the
    # device actually stored - this is the user's confirmation the write landed.
    await coordinator.async_get_weekly_schedule(zone)
    return {}


# --- Clear: wipe the whole zone schedule via Z2M's native clear command ---


async def clear_schema(_handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Clear is a whole-zone action with nothing to configure - confirm only."""
    return vol.Schema({})


async def _apply_clear(
    handler: SchemaCommonFlowHandler, _user_input: dict[str, Any], zone: str
) -> dict[str, Any]:
    """Clear the entire weekly schedule for a zone (native ZCL clear)."""
    await _coordinator(handler).async_clear_weekly_schedule(zone)
    return {}


async def schedule_sent_schema(_handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Empty schema for the final 'schedule sent' confirmation page."""
    return vol.Schema({})


async def validate_clear_heat(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Clear the heating schedule for the chosen days."""
    return await _apply_clear(handler, user_input, const.ZONE_HEAT)


async def validate_clear_water(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Clear the water schedule for the chosen days."""
    return await _apply_clear(handler, user_input, const.ZONE_WATER)


CONFIG_FLOW: dict[str, SchemaFlowFormStep | SchemaFlowMenuStep] = {
    "user": SchemaFlowFormStep(general_config_schema),
}


def build_options_flow(
    model: str,
) -> dict[str, SchemaFlowFormStep | SchemaFlowMenuStep]:
    """Assemble the model-aware options flow.

    Top menu: General settings, plus a "Manage schedules" submenu holding the
    edit/clear actions. Water actions appear only on SLR2, so SLR1/OTR1
    (heating-only) never see steps they can't use. The editor is two steps
    (setup -> rows), both zones sharing the single 'schedule_rows' step via
    flow_state.
    """
    schedule_menu = ["heat_schedule", "clear_heat"]
    if model == const.MODEL_SLR2:
        schedule_menu += ["water_schedule", "clear_water"]

    flow: dict[str, SchemaFlowFormStep | SchemaFlowMenuStep] = {
        "init": SchemaFlowMenuStep(["general", "manage_schedules"]),
        "general": SchemaFlowFormStep(general_options_schema),
        "manage_schedules": SchemaFlowMenuStep(schedule_menu),
        "heat_schedule": SchemaFlowFormStep(
            heat_setup_schema,
            validate_user_input=validate_heat_setup,
            next_step="schedule_rows",
        ),
        "schedule_rows": SchemaFlowFormStep(
            schedule_rows_schema,
            validate_user_input=write_schedule_rows,
            next_step="schedule_sent",
        ),
        "clear_heat": SchemaFlowFormStep(
            clear_schema,
            validate_user_input=validate_clear_heat,
            next_step="schedule_sent",
        ),
        # Final confirmation page (empty form) shown after any write.
        "schedule_sent": SchemaFlowFormStep(schedule_sent_schema),
    }
    if model == const.MODEL_SLR2:
        flow["water_schedule"] = SchemaFlowFormStep(
            water_setup_schema,
            validate_user_input=validate_water_setup,
            next_step="schedule_rows",
        )
        flow["clear_water"] = SchemaFlowFormStep(
            clear_schema,
            validate_user_input=validate_clear_water,
            next_step="schedule_sent",
        )
    return flow


# A superset flow so SchemaConfigFlowHandler.__init_subclass__ generates an
# async_step_* method for every step; the per-entry (model-aware) flow is what
# actually drives the options flow, built in _async_get_options_flow below.
OPTIONS_FLOW: dict[str, SchemaFlowFormStep | SchemaFlowMenuStep] = build_options_flow(
    const.MODEL_SLR2
)


# mypy: ignore-errors
class ConfigFlowHandler(SchemaConfigFlowHandler, domain=const.DOMAIN):
    """Handle a config or options flow for Holdays."""

    config_flow = CONFIG_FLOW
    options_flow = OPTIONS_FLOW
    VERSION = const.CONFIG_VERSION

    @callback
    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Return config entry title.

        The options parameter contains config entry options, which is the union of user
        input from the config flow steps.
        """
        return cast(str, options["name"]) if "name" in options else ""


@staticmethod
@callback
def _async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
    """Return a model-aware options flow for this entry.

    SchemaConfigFlowHandler.__init_subclass__ auto-generates an
    async_get_options_flow bound to the static OPTIONS_FLOW; we replace it so
    the menu can hide the water schedule editor on heating-only models.
    """
    model = config_entry.options.get(const.CONF_MODEL, const.MODEL_SLR2)
    return SchemaOptionsFlowHandler(config_entry, build_options_flow(model))


ConfigFlowHandler.async_get_options_flow = _async_get_options_flow

"""Define services for the Hive Local Thermostat integration."""

import logging
from typing import cast

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .common import HiveData
from .const import (
    DOMAIN,
    MODEL_SLR2,
    VALID_SCHEDULE_DAYS,
    WATER_SCHEDULE_OFF_SETPOINT,
    WATER_SCHEDULE_ON_SETPOINT,
    WEEKLY_SCHEDULE_OFF_SETPOINT,
    ZONE_HEAT,
    ZONE_WATER,
)

SERVICE_HEATING_BOOST = "boost_heating"
SERVICE_WATER_BOOST = "boost_water"
SERVICE_HEATING_BOOST_CANCEL = "cancel_boost_heating"
SERVICE_WATER_BOOST_CANCEL = "cancel_boost_water"
SERVICE_GET_WEEKLY_SCHEDULE = "get_weekly_schedule"
SERVICE_SET_WEEKLY_SCHEDULE = "set_weekly_schedule"

SERVICE_DATA_HEATING_BOOST_MINUTES = "minutes_to_boost"
SERVICE_DATA_HEATING_BOOST_TEMPERATURE = "temperature_to_boost"
SERVICE_DATA_WATER_BOOST_MINUTES = "minutes_to_boost"
SERVICE_DATA_ZONE = "zone"
SERVICE_DATA_DAYS = "days"
SERVICE_DATA_TRANSITIONS = "transitions"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SERVICE_BASE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): str,
    }
)

SERVICE_HEATING_BOOST_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {
        vol.Optional(SERVICE_DATA_HEATING_BOOST_MINUTES): cv.positive_int,
        vol.Optional(SERVICE_DATA_HEATING_BOOST_TEMPERATURE): cv.positive_float,
    }
)

SERVICE_WATER_BOOST_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {
        vol.Optional(SERVICE_DATA_WATER_BOOST_MINUTES): cv.positive_int,
    }
)

SERVICE_GET_WEEKLY_SCHEDULE_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {
        vol.Optional(SERVICE_DATA_ZONE, default=ZONE_HEAT): vol.In(
            [ZONE_HEAT, ZONE_WATER]
        ),
    }
)

SERVICE_SET_WEEKLY_SCHEDULE_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {
        vol.Optional(SERVICE_DATA_ZONE, default=ZONE_HEAT): vol.In(
            [ZONE_HEAT, ZONE_WATER]
        ),
        vol.Required(SERVICE_DATA_DAYS): vol.All(
            cv.ensure_list, [vol.In(VALID_SCHEDULE_DAYS)]
        ),
        vol.Required(SERVICE_DATA_TRANSITIONS): vol.All(cv.ensure_list, [dict]),
    }
)


_LOGGER = logging.getLogger(__name__)


def async_get_entry(hass: HomeAssistant, config_entry_id: str) -> ConfigEntry:
    """Get the Hive Local Thermostat config entry."""
    if not (entry := hass.config_entries.async_get_entry(config_entry_id)):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="integration_not_found",
            translation_placeholders={"target": DOMAIN},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_loaded",
            translation_placeholders={"target": entry.title},
        )
    return entry


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the hive_local_thermostat integration."""

    hass.services.async_register(
        DOMAIN,
        SERVICE_HEATING_BOOST,
        _async_heating_boost,
        schema=SERVICE_HEATING_BOOST_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_HEATING_BOOST_CANCEL,
        _async_heating_boost_cancel,
        schema=SERVICE_BASE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WATER_BOOST,
        _async_water_boost,
        schema=SERVICE_WATER_BOOST_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WATER_BOOST_CANCEL,
        _async_water_boost_cancel,
        schema=SERVICE_BASE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_WEEKLY_SCHEDULE,
        _async_get_weekly_schedule,
        schema=SERVICE_GET_WEEKLY_SCHEDULE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_WEEKLY_SCHEDULE,
        _async_set_weekly_schedule,
        schema=SERVICE_SET_WEEKLY_SCHEDULE_SCHEMA,
    )


async def _async_heating_boost(call: ServiceCall) -> ServiceResponse:
    """Handle the service call."""
    entry = async_get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = cast(HiveData, entry.runtime_data).coordinator

    boost_minutes = cast(
        int,
        call.data.get(
            SERVICE_DATA_HEATING_BOOST_MINUTES,
            coordinator.heating_boost_duration,
        ),
    )

    boost_temperature = cast(
        float,
        call.data.get(
            SERVICE_DATA_HEATING_BOOST_TEMPERATURE,
            coordinator.heating_boost_temperature,
        ),
    )

    await coordinator.async_heating_boost(boost_minutes, boost_temperature)

    return None


async def _async_heating_boost_cancel(call: ServiceCall) -> ServiceResponse:
    """Handle the service call to cancel heating boost."""
    entry = async_get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = cast(HiveData, entry.runtime_data).coordinator

    await coordinator.async_heating_boost_cancel()

    return None


async def _async_water_boost(call: ServiceCall) -> ServiceResponse:
    """Handle the service call."""
    entry = async_get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = cast(HiveData, entry.runtime_data).coordinator

    boost_minutes = cast(
        int,
        call.data.get(
            SERVICE_DATA_WATER_BOOST_MINUTES,
            coordinator.water_boost_duration,
        ),
    )

    if coordinator.model != MODEL_SLR2:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="wrong_model",
        )

    await coordinator.async_water_boost(boost_minutes)

    return None


async def _async_water_boost_cancel(call: ServiceCall) -> ServiceResponse:
    """Handle the service call to cancel water boost."""
    entry = async_get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = cast(HiveData, entry.runtime_data).coordinator

    if coordinator.model != MODEL_SLR2:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="wrong_model",
        )

    await coordinator.async_water_boost_cancel()

    return None


async def _async_get_weekly_schedule(call: ServiceCall) -> ServiceResponse:
    """Handle the service call to request the device's native weekly schedule.

    This only triggers the read - the device answers asynchronously (often as
    several messages) and the result is merged into the coordinator's
    weekly_schedule_heat/weekly_schedule_water state as it arrives.
    """
    entry = async_get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = cast(HiveData, entry.runtime_data).coordinator

    zone = cast(str, call.data[SERVICE_DATA_ZONE])

    if zone == ZONE_WATER and coordinator.model != MODEL_SLR2:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="wrong_model",
        )

    await coordinator.async_get_weekly_schedule(zone)

    return None


async def _async_set_weekly_schedule(call: ServiceCall) -> ServiceResponse:
    """Handle the service call to write a native weekly schedule to the device."""
    entry = async_get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = cast(HiveData, entry.runtime_data).coordinator

    zone = cast(str, call.data[SERVICE_DATA_ZONE])

    if zone == ZONE_WATER and coordinator.model != MODEL_SLR2:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="wrong_model",
        )

    days = cast(list[str], call.data[SERVICE_DATA_DAYS])
    raw_transitions = cast(list[dict], call.data[SERVICE_DATA_TRANSITIONS])

    transitions = []
    for item in raw_transitions:
        try:
            time_str = str(item["time"])
            hours_str, minutes_str = time_str.split(":", 1)
            minutes_since_midnight = int(hours_str) * 60 + int(minutes_str)
            heating_setpoint = _resolve_setpoint(item, zone)
        except (KeyError, ValueError, TypeError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_transition",
                translation_placeholders={"transition": str(item)},
            ) from err

        transitions.append(
            {
                "time": minutes_since_midnight,
                "heating_setpoint": heating_setpoint,
            }
        )

    await coordinator.async_set_weekly_schedule(zone, days, transitions)

    return None


def _resolve_setpoint(item: dict, zone: str) -> float:
    """Turn one friendly transition into the device's raw heating_setpoint.

    Accepts the same shape the schedule sensor renders, so a day read back via
    get_weekly_schedule can be fed straight into set_weekly_schedule:
    - water is an on/off relay: {"state": "on"/"off"} maps to the device's
      99/0 sentinels (a raw {"temperature": 99} is still accepted as-is).
    - heat: {"temperature": <°C>} for a real target, or {"state": "off"} for
      an off/setback period (mapped to the device's setback sentinel).

    Raises KeyError/ValueError/TypeError on anything unrecognised so the caller
    reports it as an invalid transition.
    """
    state = item.get("state")
    normalised_state = str(state).strip().lower() if state is not None else None

    if zone == ZONE_WATER:
        if normalised_state in ("on", "true"):
            return float(WATER_SCHEDULE_ON_SETPOINT)
        if normalised_state in ("off", "false"):
            return float(WATER_SCHEDULE_OFF_SETPOINT)
        if normalised_state is not None:
            # Unrecognised water state (caller reports it as invalid_transition).
            raise ValueError
        # No friendly state given - fall back to a raw sentinel value.
        return float(item["temperature"])

    if normalised_state == "off":
        return float(WEEKLY_SCHEDULE_OFF_SETPOINT)
    if normalised_state == "on":
        # "on" alone is ambiguous for heat - it needs an actual target.
        raise ValueError
    return float(item["temperature"])
